"""Session-owned first-crack detector runtime orchestration."""

from __future__ import annotations

import dataclasses
import logging
import time
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

    def discard_pending_audio(self, *, timeout_seconds: float = 1.0) -> None:
        """Drop every window/chunk/sample buffered anywhere in the pipeline."""
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
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        """Initialize a session-owned first-crack runtime.

        Args:
            config: Application configuration.
            audio_pipeline_factory: Optional test double for audio capture.
            detector_adapter_factory: Optional test double for detector adapter construction.
            stop_timeout_seconds: Maximum seconds to wait for capture shutdown.
            monotonic_now: Optional monotonic clock supplier for tests. Used
                to decay the rolling overflow fields on the post-stop
                snapshot (coffee-roaster-mcp#193 review finding).
        """
        self._config = config
        self._audio_pipeline_factory = audio_pipeline_factory or self._build_default_audio_pipeline
        self._detector_adapter_factory = (
            detector_adapter_factory or _build_released_detector_adapter
        )
        self._stop_timeout_seconds = stop_timeout_seconds
        self._monotonic_now = monotonic_now or time.monotonic
        self._lock = RLock()
        self._active_session_id: str | None = None
        self._pipeline: FirstCrackAudioPipeline | None = None
        self._adapter: FirstCrackDetectorAdapter | None = None
        self._status: FirstCrackRuntimeState = _initial_status(config.first_crack)
        self._reason: str | None = _initial_reason(config.first_crack)
        self._processed_window_count = 0
        self._last_capture_snapshot: AudioCaptureSnapshot | None = None
        #: Wall-clock instant `_last_capture_snapshot` was captured from a
        #: LIVE poll (#193 review finding, round 2 — NOT the stop instant:
        #: an aggregate observed from a live poll seconds or minutes before
        #: the actual stop() call is already that much older than "now" the
        #: moment stop happens, and gating decay purely on time-since-STOP
        #: ignored that staleness). Used to decay the rolling "last minute"
        #: overflow fields on `_stopped_capture_snapshot` by the true AGE of
        #: the events that produced the aggregate, not by how long ago
        #: capture stopped — those fields are contractually a trailing-60-
        #: second rolling window (see `AudioCaptureSnapshot`'s docstring),
        #: which must not stay frozen at whatever was true when last polled.
        self._last_capture_snapshot_as_of_monotonic_seconds: float | None = None
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
        #: One-slot mutable box (coffee-roaster-mcp#191) the recorder's
        #: milestones closure reads at close. process_pending_windows_after_drop
        #: writes a recovered SESSION-ELAPSED first-crack timestamp here when a
        #: genuine pre-drop window is classified only after the drop — this
        #: overrides ONLY the sidecar's first_crack milestone, never
        #: session.first_crack_monotonic_seconds or the event timeline. Reset to
        #: a fresh empty box each session so a prior roast's recovery can never
        #: leak into the next one's sidecar.
        self._recovered_first_crack_holder: list[float | None] = []

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
            self._last_capture_snapshot_as_of_monotonic_seconds = None
            # Fresh empty box every session (#191): a prior roast's recovered
            # milestone must never leak into this one's sidecar.
            self._recovered_first_crack_holder = []

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
                recovered_first_crack_holder=self._recovered_first_crack_holder,
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

    def process_pending_windows_after_drop(
        self,
        *,
        session_store: RoastSessionStore,
        session: RoastSession,
    ) -> FirstCrackRuntimeSnapshot:
        """Classify queued PRE-drop windows after `beans_dropped` was recorded,
        recovering a straggler crack into the RECORDING's milestone only.

        coffee-roaster-mcp#191: a first-crack-confirming window can be captured
        but not yet drained by the poll cadence when an operator/agent-triggered
        `drop_beans` fires. Draining BEFORE the drop (the naive fix) runs
        detector inference synchronously ahead of the driver's drop command —
        delaying a safety-relevant hardware action, which is the wrong trade.
        This method is the safe alternative: call it AFTER `beans_dropped` is
        already recorded and the driver drop already issued, so inference never
        blocks the drop.

        Deliberately does NOT call `integrate_first_crack_window_with_session`
        or write `session.first_crack_monotonic_seconds` / a
        `first_crack_detected` timeline event. The session's own phase-ordered
        event transitions (`_ALLOWED_PHASES_BY_EVENT` in session.py) correctly
        refuse a first-crack event once phase is "dropped" — recording one
        there would put the CONTROL timeline out of causal order (nothing can
        act on a crack classified after the roast already ended) for zero
        control value. Instead, this writes the recovered timestamp into
        `_recovered_first_crack_holder`, which ONLY the recorder's sidecar
        milestone reads at close (see `build_session_recorder`). The session
        event log and the recording sidecar are different contracts: the
        session log is the causal control record, the sidecar milestone is
        annotation metadata for the offline dataset/Label Studio pipeline,
        where the crack's true position in the WAV is the entire point.

        The exemption is bound on the window's END time (started + duration),
        NOT its start time: the drop itself is acoustically crack-like (beans
        cascading into the cooling tray is a burst of sharp transients — the
        detector's known false-positive class). A window whose capture START
        predates the drop but whose tail STRADDLES it could contain drop
        clatter and confirm a phantom crack, which would poison the
        ANNOTATION dataset even worse than the control timeline — it becomes
        a training label. A genuinely lost pre-drop window — the case #191
        actually reports — is a COMPLETE window that finished capturing
        before the drop and was simply sitting undrained in the queue, so its
        end time is already ≤ the drop timestamp by construction; the
        end-time bound keeps that case while rejecting anything that overlaps
        the drop.

        Args:
            session_store: Authoritative one-session mutation boundary (used
                only for the phase-independent observability write, never the
                timeline).
            session: The session `beans_dropped` was just recorded on. Must have
                `beans_dropped_monotonic_seconds` set (the caller records the
                drop event before calling this).

        Returns:
            The resulting runtime snapshot.
        """
        with self._lock:
            if not self._can_process_after_drop_locked(session):
                return self.snapshot()
            # Detector-paced WAV replay drives windows one at a time on its own
            # deterministic clock for test/replay use, not a live drop race —
            # #191's post-drop drain is a live-roast (realtime capture) concern.
            if _uses_detector_paced_wav_replay(self._config.audio):
                return self.snapshot()

            pipeline = self._pipeline
            adapter = self._adapter
            if pipeline is None or adapter is None:  # pragma: no cover - defensive
                # _can_process_after_drop_locked already requires status ==
                # "pending", and every start_for_session path that leaves
                # pipeline/adapter None also sets status to "unavailable" —
                # so this is unreachable today; kept as a narrowing guard in
                # case that invariant ever changes.
                return self.snapshot()

            drop_monotonic_seconds = session.beans_dropped_monotonic_seconds
            if drop_monotonic_seconds is None:
                return self.snapshot()
            # AudioWindow timestamps are ABSOLUTE monotonic (the capture
            # pipeline's own clock); session milestones are SESSION-elapsed.
            # Rebase the drop cutoff into the absolute domain once, the same
            # transform the session store applies in reverse for FC detection
            # (session.py's _detected_elapsed_seconds).
            drop_cutoff_absolute = session.monotonic_start + drop_monotonic_seconds
            # coffee-roaster-mcp#192: bound the OTHER end too. A window whose
            # capture started before beans_added must never join the
            # adapter's confirmation-window candidates here either — the
            # live path (integrate_first_crack_window_with_session) applies
            # the same cutoff, but this drain loop calls the adapter
            # directly and would otherwise let a pre-charge candidate
            # (empty-drum rattle, charge pour) seed a recovered milestone
            # once combined with a genuine post-charge confirming window.
            beans_added_monotonic_seconds = session.beans_added_monotonic_seconds
            earliest_eligible_absolute = (
                None
                if beans_added_monotonic_seconds is None
                else session.monotonic_start + beans_added_monotonic_seconds
            )

            try:
                for window in pipeline.drain_windows():
                    window_end_absolute = (
                        window.started_at_monotonic_seconds + window.duration_seconds
                    )
                    if window_end_absolute >= drop_cutoff_absolute:
                        # Straddles, ends exactly at, or postdates the drop:
                        # its tail may contain drop-clatter transients (a known
                        # false-positive class) or genuine cooling-phase audio.
                        # The boundary tie is rejected too — err toward safety
                        # rather than assume a same-instant end truly missed
                        # the drop noise. Skip it — only a window that finished
                        # capturing strictly before the drop is eligible for
                        # the post-drop exemption.
                        continue
                    self._processed_window_count += 1
                    observation = adapter.process_window_observed(
                        window,
                        earliest_eligible_monotonic_seconds=earliest_eligible_absolute,
                    )
                    session_store.record_first_crack_window_observation(
                        session,
                        window_sequence_number=observation.window_sequence_number,
                        confidence=observation.confidence,
                        positive_window_count=observation.positive_window_count,
                        confirmed=observation.confirmed,
                        fc_status=observation.fc_status,
                    )
                    detection_event = observation.event
                    if detection_event is None:
                        continue
                    # Recovered SESSION-ELAPSED first-crack timestamp: the
                    # detector's backdated crack onset is absolute monotonic
                    # (same domain as AudioWindow), so rebase it the same way
                    # every other session milestone is stored.
                    recovered_session_elapsed = round(
                        detection_event.detected_at_monotonic_seconds - session.monotonic_start,
                        6,
                    )
                    self._recovered_first_crack_holder.clear()
                    self._recovered_first_crack_holder.append(recovered_session_elapsed)
                    recording_relative_seconds = round(
                        detection_event.detected_at_monotonic_seconds - drop_cutoff_absolute,
                        6,
                    )
                    _LOGGER.warning(
                        "First crack recovered from a PRE-DROP window classified only "
                        "AFTER beans_dropped (coffee-roaster-mcp#191): recovered "
                        "onset %.3fs relative to the drop (negative = before drop), "
                        "session-elapsed %.3fs. Recorded into the recording sidecar's "
                        "first_crack milestone ONLY — the session event log correctly "
                        "stays silent (a post-drop crack has no control value and "
                        "would break the timeline's causal order).",
                        recording_relative_seconds,
                        recovered_session_elapsed,
                    )
                    self._status = "detected"
                    self._reason = (
                        "First crack was recovered into the recording milestone from a "
                        "pre-drop window classified after the drop (#191); the session "
                        "event log intentionally has no first_crack_detected event for "
                        "this roast."
                    )
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
        """Drop queued detector windows that were captured before a runtime boundary.

        coffee-roaster-mcp#195 CI follow-up: draining only the emitted-window
        queue is not enough once window timestamps reflect true capture time
        (#190/#195) — audio captured before the boundary can still be
        sitting unprocessed in the reader thread's backlog or the
        processing thread's partial sample buffer when this is called (a
        processing-thread backlog is exactly what CPU contention on a small
        CI runner produces). Uses `discard_pending_audio()`, which clears
        every stage of the pipeline, not just `drain_windows()`'s queue.
        """
        with self._lock:
            if self._active_session_id != session_id:
                return self.snapshot()
            if self._pipeline is not None and not _uses_detector_paced_wav_replay(
                self._config.audio
            ):
                self._pipeline.discard_pending_audio()
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
                # coffee-roaster-mcp#193 review finding, round 2: stamp EVERY
                # live poll, not just the final stop. A live-polled aggregate
                # is already however old the polling cadence made it by the
                # time capture actually stops — gating decay purely on
                # time-since-STOP ignored that: an aggregate observed from a
                # sparse live poll minutes before stop would incorrectly be
                # treated as "fresh" for a further 60 seconds after stop.
                # Decay must be measured from when this aggregate was
                # actually TRUE (this poll), not from the stop instant.
                self._last_capture_snapshot_as_of_monotonic_seconds = self._monotonic_now()
            elif self._last_capture_snapshot is not None:
                capture_snapshot = _stopped_capture_snapshot(self._last_capture_snapshot)
                # coffee-roaster-mcp#193 review finding: overflow_count_last_minute
                # and estimated_lost_audio_ms_last_minute are contractually a
                # trailing-60-SECOND rolling window (see AudioCaptureSnapshot's
                # docstring) — carrying them through unchanged at stop (task #59,
                # so an operator reviewing right after drop/stop sees the roast's
                # overflow history) must not leave them frozen forever. Decay them
                # here, at READ time, by the AGE of the events that produced this
                # aggregate (time since the LAST LIVE POLL that observed it), not
                # by how long ago capture stopped — a poll seconds after that
                # live observation still sees the accurate figure; 60+ seconds
                # after it correctly sees it decayed to zero, same as a live
                # rolling window would. The lifetime total_overflow_count is
                # untouched — it is a whole-roast figure, not a rolling one, so
                # it stays frozen by design.
                as_of = self._last_capture_snapshot_as_of_monotonic_seconds
                if as_of is not None:
                    aggregate_age_seconds = self._monotonic_now() - as_of
                    if aggregate_age_seconds >= 60.0:
                        capture_snapshot = dataclasses.replace(
                            capture_snapshot,
                            overflow_count_last_minute=0,
                            estimated_lost_audio_ms_last_minute=0.0,
                        )
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

    def _can_process_after_drop_locked(self, session: RoastSession) -> bool:
        """Guard for :meth:`process_pending_windows_after_drop` (#191).

        Mirrors ``_can_process_locked`` except the phase check accepts
        ``"dropped"`` OR ``"cooling"`` instead of ``"roasting"`` — this method
        exists specifically to run AFTER that transition. Every real driver
        (mock and Hottop) records ``cooling_started`` in the SAME atomic call
        as ``beans_dropped`` whenever the driver reports cooling engaged
        (RoasterSessionStore.complete_reserved_driver_drop_snapshot), which is
        universal for Hottop's compound drop command (the drum keeps running
        into cooling, #163) — so by the time this method runs, the session has
        USUALLY already advanced straight to ``"cooling"``; ``"dropped"``
        alone is checked too only in case a driver ever reports
        cooling-not-engaged at drop time. ``session.active`` is not required:
        a session stays active through cooling, so gating on it here would be
        redundant, not protective.
        """
        if self._config.first_crack.mode != "audio":
            return False
        if self._active_session_id != session.id:
            return False
        if self._inference_stopped:
            return False
        if self._status != "pending":
            return False
        return session.phase in ("dropped", "cooling")

    def _mark_faulted_locked(self, reason: str) -> None:
        self._status = "faulted"
        self._reason = reason
        self._stop_locked(reason="runtime fault")

    def _stop_locked(self, *, reason: str) -> None:
        pipeline = self._pipeline
        if pipeline is not None:
            try:
                # pipeline.stop() returns a FINAL LIVE snapshot taken during
                # the stop call itself, so "now" genuinely is this
                # aggregate's true as-of instant (#193 review finding,
                # round 2) — same stamping discipline as every other live
                # poll in snapshot().
                self._last_capture_snapshot = _stopped_capture_snapshot(
                    pipeline.stop(timeout_seconds=self._stop_timeout_seconds)
                )
                self._last_capture_snapshot_as_of_monotonic_seconds = self._monotonic_now()
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
    recovered_first_crack_holder: list[float | None] | None = None,
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
        recovered_first_crack_holder: Optional one-slot mutable box
            (coffee-roaster-mcp#191) that, if set to a non-``None`` monotonic
            timestamp before the recorder closes, overrides the sidecar's
            ``first_crack`` milestone WITHOUT touching
            ``session.first_crack_monotonic_seconds`` or the session event
            timeline. Recovers a pre-drop crack that was classified only after
            `beans_dropped` (so it could never legally re-enter the causal,
            phase-ordered session log) into the RECORDING'S annotation
            metadata, which is a genuinely separate contract from the control
            timeline — see `FirstCrackSessionRuntime.process_pending_windows_after_drop`.

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
        first_crack_override = (
            recovered_first_crack_holder[0] if recovered_first_crack_holder else None
        )
        return recording_relative_milestones(
            session,
            recording_started_monotonic_seconds=recording_started_monotonic,
            first_crack_override_monotonic_seconds=first_crack_override,
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
    first_crack_override_monotonic_seconds: float | None = None,
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
        first_crack_override_monotonic_seconds: coffee-roaster-mcp#191 — a
            SESSION-ELAPSED first-crack timestamp recovered by the post-drop
            drain, used in place of `session.first_crack_monotonic_seconds`
            when set. The session's own field is deliberately left untouched
            (the control timeline never records a post-drop crack), so this is
            the only way the sidecar can carry a first_crack milestone that a
            straggler pre-drop window supplied. `None` (the default) falls
            back to the session's own value, unchanged from prior behavior.

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

    first_crack_session_elapsed = (
        session.first_crack_monotonic_seconds
        if first_crack_override_monotonic_seconds is None
        else first_crack_override_monotonic_seconds
    )
    return {
        "beans_added": _rebase(session.beans_added_monotonic_seconds),
        "first_crack": _rebase(first_crack_session_elapsed),
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
