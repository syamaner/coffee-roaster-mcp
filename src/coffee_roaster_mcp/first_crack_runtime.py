"""Session-owned first-crack detector runtime orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Literal, Protocol

from coffee_roaster_mcp.artifacts import ArtifactResolutionError
from coffee_roaster_mcp.audio import (
    AdditionalRecordingDevice,
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSnapshot,
    AudioWindow,
    MultiDeviceRoastRecorder,
    RoastAudioRecorder,
    RoastRecorder,
    build_audio_capture_pipeline,
    device_label_to_filename,
)
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorError,
    build_released_onnx_first_crack_detector_adapter,
    integrate_first_crack_window_with_session,
)
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore, SessionLifecycleError

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
        #: Recorder for the session currently being started (#176). Set while the
        #: default pipeline factory runs so the teed WAV is wired into the real
        #: capture pipeline; injected test factories ignore it.
        self._pending_recorder: RoastRecorder | None = None

    def _build_default_audio_pipeline(self, config: AudioConfig) -> AudioCapturePipeline:
        """Build the real capture pipeline, teeing the pending session recorder."""
        return build_audio_capture_pipeline(config, recorder=self._pending_recorder)

    def start_for_session(self, session: RoastSession) -> FirstCrackRuntimeSnapshot:
        """Start or prepare first-crack detection for a roast session."""
        with self._lock:
            self._stop_locked(reason="new roast session")
            self._active_session_id = session.id
            self._processed_window_count = 0
            self._last_capture_snapshot = None

            if self._config.first_crack.mode != "audio":
                self._status = _initial_status(self._config.first_crack)
                self._reason = _initial_reason(self._config.first_crack)
                return self.snapshot()

            self._status = "pending"
            self._reason = "Audio first-crack detection is prepared for this session."
            recorder = build_session_recorder(self._config, session)
            self._pending_recorder = recorder
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
                        self._stop_locked(reason="first crack detected")
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

            return FirstCrackRuntimeSnapshot(
                status=status,
                active_session_id=self._active_session_id,
                active=self._pipeline is not None,
                reason=reason,
                audio_running=False if capture_snapshot is None else capture_snapshot.running,
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
            )

    def _can_process_locked(self, session: RoastSession) -> bool:
        if self._config.first_crack.mode != "audio":
            return False
        if self._active_session_id != session.id:
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


def build_session_recorder(
    config: AppConfig,
    session: RoastSession,
) -> RoastRecorder | None:
    """Build a per-roast audio recorder for one session, or `None` when disabled.

    Recording is wired when both `recording.enabled` and `recording.autocapture`
    are true, so capture begins with the roast and needs no MCP command. Output
    lands under `export_location/<session_id>/`.

    Dispatch on `recording.devices`:

    - Unset, empty, or a single device: a single-stream
      :class:`RoastAudioRecorder` that tees the detector's existing mono stream
      into one WAV (the original behaviour).
    - Two or more devices: a :class:`MultiDeviceRoastRecorder` — the FIRST device
      is the detector's device (teed, no second open) and each ADDITIONAL device
      is captured as its own independent stream into its own WAV (option A). WAV
      filenames are derived from the device labels.

    Args:
        config: Application configuration.
        session: Live roast session that owns the recording. Its milestone
            timestamps are read at close to compute recording-relative offsets.

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
    sample_rate = recording.sample_rate or config.audio.sample_rate
    devices = recording.devices or ()

    def milestones() -> dict[str, float | None]:
        return recording_relative_milestones(session)

    if len(devices) >= 2:
        detector_label = devices[0]
        detector_wav = session_dir / f"roast.{device_label_to_filename(detector_label)}.wav"
        additional = [
            AdditionalRecordingDevice(
                device_label=label,
                wav_path=session_dir / f"roast.{device_label_to_filename(label)}.wav",
                sample_rate=sample_rate,
            )
            for label in devices[1:]
        ]
        return MultiDeviceRoastRecorder(
            detector_wav_path=detector_wav,
            detector_device_label=detector_label,
            sidecar_path=sidecar_path,
            sample_rate=sample_rate,
            session_id=session.id,
            additional_devices=additional,
            milestones_provider=milestones,
        )

    # Single-stream: tee the detector only. A lone configured device labels the
    # WAV; otherwise the default roast.wav name is kept.
    detector_label = devices[0] if devices else None
    return RoastAudioRecorder(
        wav_path=session_dir / "roast.wav",
        sidecar_path=sidecar_path,
        sample_rate=sample_rate,
        session_id=session.id,
        device_label=detector_label,
        milestones_provider=milestones,
    )


def recording_relative_milestones(session: RoastSession) -> dict[str, float | None]:
    """Return roast milestones in recording-relative seconds for the sidecar.

    The session stores milestones as elapsed seconds from its own start, while
    the recorder's clock starts when capture begins. Both share the process
    monotonic clock, so a milestone's recording-relative offset is its absolute
    monotonic time (`session.monotonic_start + elapsed`) minus the recording
    start. Recording starts very slightly after the session, so the offset is
    effectively the session-elapsed value; it is returned verbatim because the
    sub-tick skew is below the detector window and the recorder owns the
    absolute recording-start timestamp separately in the sidecar.

    Args:
        session: Live roast session.

    Returns:
        Mapping of milestone name to recording-relative seconds, or `None` when
        the milestone has not fired.
    """
    return {
        "beans_added": session.beans_added_monotonic_seconds,
        "first_crack": session.first_crack_monotonic_seconds,
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
    return AudioCaptureSnapshot(
        running=False,
        queued_window_count=snapshot.queued_window_count,
        emitted_window_count=snapshot.emitted_window_count,
        dropped_window_count=snapshot.dropped_window_count,
        latest_error=snapshot.latest_error,
    )


def _initial_reason(config: FirstCrackConfig) -> str:
    if config.mode == "disabled":
        return "Automatic first-crack detection is disabled by configuration."
    if config.mode == "manual":
        if not config.allow_manual_override:
            return "Manual first-crack mode is configured, but manual override is disabled."
        return "Waiting for explicit mark_first_crack override."
    return "Audio first-crack detection has not started."
