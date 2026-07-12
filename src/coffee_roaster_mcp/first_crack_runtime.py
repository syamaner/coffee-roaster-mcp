"""Session-owned first-crack detector runtime orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol

from coffee_roaster_mcp.artifacts import ArtifactResolutionError
from coffee_roaster_mcp.audio import (
    AdditionalRecordingDevice,
    AnnotationSessionSpec,
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSnapshot,
    AudioWindow,
    MultiDeviceRoastRecorder,
    RoastAudioRecorder,
    RoastRecorder,
    build_audio_capture_pipeline,
)
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorError,
    build_released_onnx_first_crack_detector_adapter,
    integrate_first_crack_window_with_session,
)
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore, SessionLifecycleError

_LOGGER = logging.getLogger(__name__)

FirstCrackRuntimeState = Literal[
    "disabled",
    "manual",
    "pending",
    "detected",
    "faulted",
    "unavailable",
]


class FirstCrackAudioPipeline(Protocol):
    """Audio pipeline operations required by the first-crack runtime."""

    def start(self) -> AudioCaptureSnapshot:
        """Start capture and return capture status."""
        ...

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        """Stop capture and return capture status."""
        ...

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        """Return queued detector windows without blocking."""
        ...

    def snapshot(self) -> AudioCaptureSnapshot:
        """Return current capture status."""
        ...


FirstCrackAudioPipelineFactory = Callable[[AudioConfig], FirstCrackAudioPipeline]
FirstCrackDetectorAdapterFactory = Callable[[FirstCrackConfig], FirstCrackDetectorAdapter]


@dataclass(frozen=True)
class FirstCrackRuntimeSnapshot:
    """MCP-visible first-crack runtime status.

    Attributes:
        status: Current runtime state.
        active_session_id: Session id that owns the runtime, if any.
        active: Whether the runtime currently owns an active capture pipeline.
        reason: Human-readable status detail.
        audio_running: Whether the audio capture worker is alive.
        queued_window_count: Detector windows waiting to be processed.
        emitted_window_count: Windows emitted by the capture pipeline.
        dropped_window_count: Windows dropped by the capture queue.
        processed_window_count: Windows processed by this runtime.
        mic_peak_dbfs: Live rolling peak level of the captured mic stream in dBFS
            (#178), or ``None`` when no audio capture is running. ``-inf`` for
            silence. Lets a mis-gained / dead mic be caught under real conditions.
        mic_rms_dbfs: Live rolling RMS level of the captured mic stream in dBFS
            (#178), or ``None`` when no audio capture is running.
        overflow_count_last_minute: Microphone input-overflow events (#190) in
            the trailing 60 seconds, or ``0`` when no audio capture is running
            or the input does not report overflows. Surfaces sustained
            degradation (e.g. under CPU contention) as an operator-visible
            diagnostic instead of stderr-only warning logs.
        estimated_lost_audio_ms_last_minute: Estimated milliseconds of audio at
            risk from overflow events in the trailing 60 seconds. See
            :class:`~coffee_roaster_mcp.audio.OverflowSnapshot` for the
            estimation method (derived from the actual inter-read gap).
        total_overflow_count: Lifetime overflow event count for the current
            capture run, for a whole-roast severity view alongside the rolling
            per-minute figures.
    """

    status: FirstCrackRuntimeState
    active_session_id: str | None
    active: bool
    reason: str | None = None
    audio_running: bool = False
    queued_window_count: int = 0
    emitted_window_count: int = 0
    dropped_window_count: int = 0
    processed_window_count: int = 0
    mic_peak_dbfs: float | None = None
    mic_rms_dbfs: float | None = None
    overflow_count_last_minute: int = 0
    estimated_lost_audio_ms_last_minute: float = 0.0
    total_overflow_count: int = 0


class FirstCrackSessionRuntime:
    """Own first-crack audio and detector processing for roast sessions."""

    def __init__(
        self,
        *,
        config: AppConfig,
        audio_pipeline_factory: FirstCrackAudioPipelineFactory | None = None,
        detector_adapter_factory: FirstCrackDetectorAdapterFactory | None = None,
        stop_timeout_seconds: float = 1.0,
    ) -> None:
        """Initialize a session-owned first-crack runtime.

        Args:
            config: Application configuration.
            audio_pipeline_factory: Optional test double for audio capture.
            detector_adapter_factory: Optional test double for detector adapter construction.
            stop_timeout_seconds: Maximum seconds to wait for capture shutdown.
        """
        self._config = config
        self._audio_pipeline_factory = audio_pipeline_factory or self._build_default_audio_pipeline
        self._detector_adapter_factory = (
            detector_adapter_factory or _build_released_detector_adapter
        )
        self._stop_timeout_seconds = stop_timeout_seconds
        self._lock = RLock()
        self._active_session_id: str | None = None
        self._pipeline: FirstCrackAudioPipeline | None = None
        self._adapter: FirstCrackDetectorAdapter | None = None
        self._status: FirstCrackRuntimeState = _initial_status(config.first_crack)
        self._reason: str | None = _initial_reason(config.first_crack)
        self._processed_window_count = 0
        self._last_capture_snapshot: AudioCaptureSnapshot | None = None
        #: Whether inference/draining has been stopped for this session while the
        #: capture worker + recorder keep running (#181). Set at first-crack
        #: detection so the runtime stops draining/detecting but the recording
        #: spans charge→session stop (not charge→FC). The pipeline is finalised
        #: only at ``stop_for_session`` (the real roast end).
        self._inference_stopped = False
        #: Recorder for the session currently being started (#176). Set while the
        #: default pipeline factory runs so the teed WAV is wired into the real
        #: capture pipeline; injected test factories ignore it.
        self._pending_recorder: RoastRecorder | None = None
        #: Annotation-pipeline metadata for the next/current recording (#176). Set
        #: by ``set_recording_metadata`` before the roast; consumed when the
        #: recorder is built at session start. None falls back to session_id / 0.
        self._recording_metadata: RecordingMetadata | None = None
        #: Whether the recorder for the active session was already built (#176). If
        #: ``set_recording_metadata`` arrives after this, the WAV names are already
        #: fixed and the late metadata is rejected with a clear warning.
        self._recorder_built_for_session = False

    def set_recording_metadata(self, *, origin: str, roast_num: int) -> RecordingMetadata:
        """Store annotation-pipeline metadata for the next roast's recording.

        Call this BEFORE ``start_roast_session``. The recorder reads it when it
        creates the WAVs, naming them ``mic{N}-{origin}-roast{N}.wav`` and writing
        a ``{origin}-roast{N}-session.json`` for the coffee-first-crack-detection
        annotation pipeline. If never called, recording falls back to the session
        id as origin and roast number ``0`` so capture never breaks.

        Ordering matters: the recorder is built at session start, so metadata set
        AFTER the roast has started cannot rename the already-open WAVs. Calling
        it late is therefore non-silent — it logs a clear warning and the late
        metadata is NOT applied (renaming nothing while relabelling the session
        JSON would point the JSON at the wrong WAV files). The stored value is
        still returned so the agent can detect the mismatch.

        Args:
            origin: Bean origin slug (e.g. ``"brazil"``).
            roast_num: 1-based roast number.

        Returns:
            The stored :class:`RecordingMetadata`.

        Raises:
            ValueError: If origin is blank or roast_num is negative.
        """
        normalized_origin = origin.strip()
        if not normalized_origin:
            raise ValueError("origin must not be blank.")
        if roast_num < 0:
            raise ValueError("roast_num must be >= 0.")
        with self._lock:
            if self._recorder_built_for_session:
                _LOGGER.warning(
                    "set_recording_metadata(origin=%r, roast_num=%d) arrived AFTER the "
                    "roast recorder was built; the WAV names are already fixed for the "
                    "active session and this metadata will NOT be applied. Call "
                    "set_recording_metadata BEFORE start_roast_session.",
                    normalized_origin,
                    roast_num,
                )
            self._recording_metadata = RecordingMetadata(
                origin=normalized_origin,
                roast_num=roast_num,
            )
            return self._recording_metadata

    def _build_default_audio_pipeline(self, config: AudioConfig) -> AudioCapturePipeline:
        """Build the real capture pipeline, teeing the pending session recorder."""
        return build_audio_capture_pipeline(config, recorder=self._pending_recorder)

    def start_for_session(self, session: RoastSession) -> FirstCrackRuntimeSnapshot:
        """Start or prepare first-crack detection for a roast session."""
        with self._lock:
            self._stop_locked(reason="new roast session")
            self._active_session_id = session.id
            self._processed_window_count = 0
            self._inference_stopped = False
            self._last_capture_snapshot = None

            if self._config.first_crack.mode != "audio":
                self._status = _initial_status(self._config.first_crack)
                self._reason = _initial_reason(self._config.first_crack)
                return self.snapshot()

            self._status = "pending"
            self._reason = "Audio first-crack detection is prepared for this session."
            recorder = build_session_recorder(
                self._config,
                session,
                metadata=self._recording_metadata,
            )
            self._pending_recorder = recorder
            # Once the recorder exists the WAV names are fixed; a later
            # set_recording_metadata for this session is rejected with a warning.
            self._recorder_built_for_session = recorder is not None
            try:
                adapter = self._detector_adapter_factory(self._config.first_crack)
                pipeline = self._audio_pipeline_factory(self._config.audio)
                self._last_capture_snapshot = pipeline.start()
            except ArtifactResolutionError as exc:
                self._status = "unavailable"
                self._reason = f"First-crack detector artifacts are unavailable: {exc}"
                self._adapter = None
                self._pipeline = None
                return self.snapshot()
            except (AudioCaptureError, FirstCrackDetectorError) as exc:
                self._status = "unavailable"
                self._reason = f"Audio first-crack detection is unavailable: {exc}"
                self._adapter = None
                self._pipeline = None
                return self.snapshot()
            except Exception as exc:  # noqa: BLE001 - dependency backends vary.
                self._status = "unavailable"
                self._reason = (
                    "Audio first-crack detection could not be prepared: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._adapter = None
                self._pipeline = None
                return self.snapshot()
            finally:
                # The recorder is consumed by the pipeline factory; clear the
                # session-scoped handle so it never leaks into the next start.
                self._pending_recorder = None

            self._adapter = adapter
            self._pipeline = pipeline
            return self.snapshot()

    def process_available_windows(
        self,
        *,
        session_store: RoastSessionStore,
        session: RoastSession,
    ) -> FirstCrackRuntimeSnapshot:
        """Process queued detector windows for the owning active roast session."""
        with self._lock:
            if not self._can_process_locked(session):
                return self.snapshot()

            pipeline = self._pipeline
            adapter = self._adapter
            if pipeline is None or adapter is None:
                return self.snapshot()

            capture_snapshot = pipeline.snapshot()
            self._last_capture_snapshot = capture_snapshot
            if capture_snapshot.latest_error is not None:
                self._mark_faulted_locked(f"Audio capture failed: {capture_snapshot.latest_error}")
                return self.snapshot()

            drain_limit = 1 if _uses_detector_paced_wav_replay(self._config.audio) else None
            try:
                for window in pipeline.drain_windows(max_windows=drain_limit):
                    self._processed_window_count += 1
                    result = integrate_first_crack_window_with_session(
                        config=self._config.first_crack,
                        adapter=adapter,
                        session_store=session_store,
                        session=session,
                        window=window,
                        allow_future_timeline=_uses_detector_paced_wav_replay(self._config.audio),
                    )
                    if result is not None:
                        self._status = "detected"
                        self._reason = "First crack was recorded by audio detection."
                        # #181: stop ONLY inference/draining at first crack — keep
                        # the capture worker + recorder running so the recording
                        # spans charge→session stop (not charge→FC). Nothing drains
                        # the bounded window queue after this, so it fills and drops
                        # harmlessly while the capture worker keeps teeing to the
                        # WAVs. The pipeline finalises at stop_for_session.
                        self._inference_stopped = True
                        break
            except (AudioCaptureError, FirstCrackDetectorError, SessionLifecycleError) as exc:
                self._mark_faulted_locked(f"First-crack detection failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - detector backends vary.
                self._mark_faulted_locked(
                    f"First-crack detection failed: {type(exc).__name__}: {exc}"
                )

            return self.snapshot()

    def stop_for_session(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> FirstCrackRuntimeSnapshot:
        """Stop first-crack detection if it belongs to the supplied session."""
        with self._lock:
            if self._active_session_id != session_id:
                return self.snapshot()
            self._stop_locked(reason=reason)
            return self.snapshot()

    def discard_queued_windows_for_session(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> FirstCrackRuntimeSnapshot:
        """Drop queued detector windows that were captured before a runtime boundary."""
        with self._lock:
            if self._active_session_id != session_id:
                return self.snapshot()
            if self._pipeline is not None and not _uses_detector_paced_wav_replay(
                self._config.audio
            ):
                self._pipeline.drain_windows()
            if self._status == "pending":
                self._reason = reason
            return self.snapshot()

    def shutdown(self) -> FirstCrackRuntimeSnapshot:
        """Stop any active detector runtime for process shutdown."""
        with self._lock:
            self._stop_locked(reason="process shutdown")
            return self.snapshot()

    def snapshot(self) -> FirstCrackRuntimeSnapshot:
        """Return an MCP-visible runtime snapshot."""
        with self._lock:
            capture_snapshot = self._pipeline.snapshot() if self._pipeline is not None else None
            if capture_snapshot is not None:
                self._last_capture_snapshot = capture_snapshot
            elif self._last_capture_snapshot is not None:
                capture_snapshot = _stopped_capture_snapshot(self._last_capture_snapshot)
            status = self._status
            reason = self._reason
            if (
                status == "pending"
                and capture_snapshot is not None
                and capture_snapshot.latest_error is not None
            ):
                status = "faulted"
                reason = f"Audio capture failed: {capture_snapshot.latest_error}"

            audio_running = capture_snapshot is not None and capture_snapshot.running
            # The mic levels are a LIVE signal: report them only while audio
            # capture is running. After stop_for_session the stopped capture
            # snapshot still carries the last-measured levels, but they are stale,
            # so gate on ``audio_running`` — ``None`` reliably means "no live
            # signal", matching the field's documented semantics (#178).
            mic_peak_dbfs = (
                capture_snapshot.peak_dbfs if audio_running and capture_snapshot else None
            )
            mic_rms_dbfs = capture_snapshot.rms_dbfs if audio_running and capture_snapshot else None
            return FirstCrackRuntimeSnapshot(
                status=status,
                active_session_id=self._active_session_id,
                active=self._pipeline is not None,
                reason=reason,
                audio_running=audio_running,
                queued_window_count=0
                if capture_snapshot is None
                else capture_snapshot.queued_window_count,
                emitted_window_count=0
                if capture_snapshot is None
                else capture_snapshot.emitted_window_count,
                dropped_window_count=0
                if capture_snapshot is None
                else capture_snapshot.dropped_window_count,
                processed_window_count=self._processed_window_count,
                mic_peak_dbfs=mic_peak_dbfs,
                mic_rms_dbfs=mic_rms_dbfs,
                overflow_count_last_minute=0
                if capture_snapshot is None
                else capture_snapshot.overflow_count_last_minute,
                estimated_lost_audio_ms_last_minute=0.0
                if capture_snapshot is None
                else capture_snapshot.estimated_lost_audio_ms_last_minute,
                total_overflow_count=0
                if capture_snapshot is None
                else capture_snapshot.total_overflow_count,
            )

    def _can_process_locked(self, session: RoastSession) -> bool:
        if self._config.first_crack.mode != "audio":
            return False
        if self._active_session_id != session.id:
            return False
        # Inference is stopped at first crack (#181) while capture keeps running;
        # never drain/detect again until a fresh session resets the flag.
        if self._inference_stopped:
            return False
        if self._status != "pending":
            return False
        return session.active and session.phase == "roasting"

    def _mark_faulted_locked(self, reason: str) -> None:
        self._status = "faulted"
        self._reason = reason
        self._stop_locked(reason="runtime fault")

    def _stop_locked(self, *, reason: str) -> None:
        pipeline = self._pipeline
        if pipeline is not None:
            try:
                self._last_capture_snapshot = _stopped_capture_snapshot(
                    pipeline.stop(timeout_seconds=self._stop_timeout_seconds)
                )
            except Exception as exc:  # noqa: BLE001 - shutdown should be best effort.
                self._status = "faulted"
                self._reason = f"Audio capture stop failed: {type(exc).__name__}: {exc}"
            finally:
                self._pipeline = None
                self._adapter = None
        # The session is over: the next roast may set fresh recording metadata,
        # and inference may run again from scratch (#181).
        self._recorder_built_for_session = False
        self._inference_stopped = False
        if self._status == "pending":
            self._reason = f"Audio first-crack detection stopped before confirmation: {reason}."


def build_first_crack_session_runtime(
    config: AppConfig,
    *,
    audio_pipeline_factory: FirstCrackAudioPipelineFactory | None = None,
    detector_adapter_factory: FirstCrackDetectorAdapterFactory | None = None,
) -> FirstCrackSessionRuntime:
    """Build the configured session-owned first-crack runtime."""
    return FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=audio_pipeline_factory,
        detector_adapter_factory=detector_adapter_factory,
    )


#: Default per-roast capture root when `recording.export_location` is unset.
#: This lives under the configured log dir, which is gitignored, so large WAVs
#: are never committed (#176 privacy/storage).
_DEFAULT_RECORDING_SUBDIR = "captures"


@dataclass(frozen=True)
class RecordingMetadata:
    """Annotation-pipeline metadata for a recording session (#176).

    Set by the ``set_recording_metadata`` MCP tool before a roast so the captured
    WAVs are named and described for the coffee-first-crack-detection annotation
    pipeline.

    Attributes:
        origin: Bean origin slug (e.g. ``"brazil"``).
        roast_num: 1-based roast number.
    """

    origin: str
    roast_num: int


def _normalize_origin_slug(origin: str) -> str:
    """Coerce an origin into the ``[a-z0-9-]+`` slug the FC pipeline expects.

    Lower-cases, replaces every run of disallowed characters with a hyphen, and
    trims leading/trailing hyphens. An empty result falls back to ``"roast"`` so
    a filename is always producible.
    """
    chars = [char.lower() if char.isalnum() else "-" for char in origin]
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "roast"


def build_session_recorder(
    config: AppConfig,
    session: RoastSession,
    *,
    metadata: RecordingMetadata | None = None,
) -> RoastRecorder | None:
    """Build a per-roast audio recorder for one session, or `None` when disabled.

    Recording is wired when both `recording.enabled` and `recording.autocapture`
    are true, so capture begins with the roast and needs no MCP command. Output
    lands under `export_location/<session_id>/`.

    Annotation-pipeline naming (#176): WAVs are named
    ``mic{N}-{origin}-roast{N}.wav`` (N = 1-based device order, mic1 = the
    detector/teed device) and a ``{origin}-roast{N}-session.json`` is written
    alongside the recording sidecar, so the output plugs straight into
    coffee-first-crack-detection's ``propagate_annotations`` / ``chunk_audio``.
    When `metadata` is omitted the fallback origin is the session id and the
    roast number is ``0``, so capture never breaks.

    Dispatch on `recording.devices`:

    - Unset, empty, or a single device: a single-stream
      :class:`RoastAudioRecorder` that tees the detector's existing mono stream
      into one WAV (mic1).
    - Two or more devices: a :class:`MultiDeviceRoastRecorder` — the FIRST device
      is the detector's device (teed, no second open, mic1) and each ADDITIONAL
      device is captured as its own independent stream into its own WAV (option
      A, mic2..N).

    Args:
        config: Application configuration.
        session: Live roast session that owns the recording. Its milestone
            timestamps are read at close to compute recording-relative offsets.
        metadata: Optional annotation-pipeline metadata (origin + roast number).
            Falls back to ``origin=session.id`` and ``roast_num=0`` when omitted.

    Returns:
        A configured recorder, or `None` when recording is disabled or capture
        is not autostarted for this roast.
    """
    recording = config.recording
    if not recording.enabled or not recording.autocapture:
        return None
    export_location = recording.export_location or (
        config.logging.log_dir / _DEFAULT_RECORDING_SUBDIR
    )
    session_dir = export_location / session.id
    sidecar_path = session_dir / "roast.recording.json"
    # The teed mic1 stream IS the FC detector's stream, so its WAV header must use
    # the detector's TRUE capture rate (audio.sample_rate), not recording.sample_rate
    # (#176 hardware bug 1: a 16 kHz teed stream mislabelled at 44.1 kHz played
    # ~2.75x too fast). Only the independently-opened additional streams use
    # recording.sample_rate (defaulting to the detector rate when unset).
    detector_sample_rate = config.audio.sample_rate
    additional_sample_rate = recording.sample_rate or config.audio.sample_rate
    devices = recording.devices or ()

    origin = _normalize_origin_slug(metadata.origin if metadata is not None else session.id)
    roast_num = metadata.roast_num if metadata is not None else 0
    annotation_path = session_dir / f"{origin}-roast{roast_num}-session.json"

    def _mic_wav(mic_num: int) -> Path:
        return session_dir / f"mic{mic_num}-{origin}-roast{roast_num}.wav"

    # The milestones closure needs the recorder's start instant to rebase the
    # session-elapsed milestone times onto the recording clock, but the recorder
    # is built below. Capture it in a one-slot holder the closure reads at close.
    recorder_holder: list[RoastRecorder] = []

    def milestones() -> dict[str, float | None]:
        recorder = recorder_holder[0] if recorder_holder else None
        recording_started_monotonic = (
            recorder.started_monotonic_seconds if recorder is not None else None
        )
        return recording_relative_milestones(
            session,
            recording_started_monotonic_seconds=recording_started_monotonic,
        )

    # Per-device labels for the annotation session JSON, in device order. The
    # detector is mic1; additional devices use their configured label, else mic{N}.
    mic_labels = tuple(
        devices[index] if index < len(devices) else f"mic{index + 1}"
        for index in range(max(1, len(devices)))
    )

    recorder: RoastRecorder
    if len(devices) >= 2:
        detector_label = devices[0]
        annotation_session = AnnotationSessionSpec(
            path=annotation_path,
            origin=origin,
            roast_num=roast_num,
            mic_labels=mic_labels,
        )
        additional = [
            AdditionalRecordingDevice(
                device_label=label,
                wav_path=_mic_wav(index + 2),
                sample_rate=additional_sample_rate,
            )
            for index, label in enumerate(devices[1:])
        ]
        recorder = MultiDeviceRoastRecorder(
            detector_wav_path=_mic_wav(1),
            detector_device_label=detector_label,
            sidecar_path=sidecar_path,
            sample_rate=detector_sample_rate,
            session_id=session.id,
            additional_devices=additional,
            milestones_provider=milestones,
            annotation_session=annotation_session,
        )
    else:
        # Single-stream: tee the detector only (mic1). A lone configured device
        # supplies the mic1 label; otherwise the default mic1 label is used.
        detector_label = devices[0] if devices else None
        annotation_session = AnnotationSessionSpec(
            path=annotation_path,
            origin=origin,
            roast_num=roast_num,
            mic_labels=(mic_labels[0],),
        )
        recorder = RoastAudioRecorder(
            wav_path=_mic_wav(1),
            sidecar_path=sidecar_path,
            sample_rate=detector_sample_rate,
            session_id=session.id,
            device_label=detector_label,
            milestones_provider=milestones,
            annotation_session=annotation_session,
        )
    recorder_holder.append(recorder)
    return recorder


def recording_relative_milestones(
    session: RoastSession,
    *,
    recording_started_monotonic_seconds: float | None,
) -> dict[str, float | None]:
    """Return roast milestones in RECORDING-relative seconds for the sidecar.

    The session stores each milestone as elapsed seconds from the SESSION start
    (`session.monotonic_start`). The recorder starts slightly later, at its own
    `started_monotonic_seconds` (an absolute monotonic instant). To express a
    milestone relative to the RECORDING start — what the operator needs to align
    the WAV to T0 / first crack for offline annotation — subtract the recording
    start, converted into the same session-elapsed domain:

        recording_start_session_elapsed =
            recording_started_monotonic_seconds - session.monotonic_start
        milestone_recording_relative =
            milestone_session_elapsed - recording_start_session_elapsed

    Both the milestone and the recording start are exact in the shared monotonic
    clock, so the subtraction is exact (no sub-tick fudge). When the recording
    start is unknown (recorder never began), the raw session-elapsed value is
    returned unchanged.

    Args:
        session: Live roast session.
        recording_started_monotonic_seconds: The recorder's absolute monotonic
            start instant, or `None` if recording never started.

    Returns:
        Mapping of milestone name to recording-relative seconds, or `None` when
        the milestone has not fired.
    """
    if recording_started_monotonic_seconds is None:
        offset = 0.0
    else:
        offset = recording_started_monotonic_seconds - session.monotonic_start

    def _rebase(value: float | None) -> float | None:
        if value is None:
            return None
        return round(value - offset, 6)

    return {
        "beans_added": _rebase(session.beans_added_monotonic_seconds),
        "first_crack": _rebase(session.first_crack_monotonic_seconds),
    }


def _build_released_detector_adapter(config: FirstCrackConfig) -> FirstCrackDetectorAdapter:
    return build_released_onnx_first_crack_detector_adapter(config)


def _uses_detector_paced_wav_replay(config: AudioConfig) -> bool:
    return config.source == "wav" and config.replay_mode == "detector_paced"


def _initial_status(config: FirstCrackConfig) -> FirstCrackRuntimeState:
    if config.mode == "disabled":
        return "disabled"
    if config.mode == "manual":
        if not config.allow_manual_override:
            return "unavailable"
        return "manual"
    return "pending"


def _stopped_capture_snapshot(snapshot: AudioCaptureSnapshot) -> AudioCaptureSnapshot:
    # A stopped capture carries no LIVE mic levels: the meter's last values are
    # stale once capture stops, so drop them (the runtime snapshot also gates the
    # levels on ``audio_running``, so a stale value would never surface anyway).
    #
    # Overflow stats (#190 review finding) are the OPPOSITE case — they are
    # exactly what an operator wants to review right after drop/stop, not a
    # live-only signal to discard. Carry them through unchanged so fc_status
    # doesn't silently zero out the roast's overflow history the moment
    # capture stops, which is precisely when it matters most.
    return AudioCaptureSnapshot(
        running=False,
        queued_window_count=snapshot.queued_window_count,
        emitted_window_count=snapshot.emitted_window_count,
        dropped_window_count=snapshot.dropped_window_count,
        latest_error=snapshot.latest_error,
        peak_dbfs=None,
        rms_dbfs=None,
        overflow_count_last_minute=snapshot.overflow_count_last_minute,
        estimated_lost_audio_ms_last_minute=snapshot.estimated_lost_audio_ms_last_minute,
        total_overflow_count=snapshot.total_overflow_count,
    )


def _initial_reason(config: FirstCrackConfig) -> str:
    if config.mode == "disabled":
        return "Automatic first-crack detection is disabled by configuration."
    if config.mode == "manual":
        if not config.allow_manual_override:
            return "Manual first-crack mode is configured, but manual override is disabled."
        return "Waiting for explicit mark_first_crack override."
    return "Audio first-crack detection has not started."
