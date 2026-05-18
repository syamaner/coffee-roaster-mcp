"""Session-owned first-crack detector runtime orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Literal, Protocol

from coffee_roaster_mcp.artifacts import ArtifactResolutionError
from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSnapshot,
    AudioWindow,
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
        self._audio_pipeline_factory = audio_pipeline_factory or _build_audio_pipeline
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

    def start_for_session(self, session: RoastSession) -> FirstCrackRuntimeSnapshot:
        """Start or prepare first-crack detection for a roast session."""
        with self._lock:
            self._stop_locked(reason="new roast session")
            self._active_session_id = session.id
            self._processed_window_count = 0

            if self._config.first_crack.mode != "audio":
                self._status = _initial_status(self._config.first_crack)
                self._reason = _initial_reason(self._config.first_crack)
                return self.snapshot()

            self._status = "pending"
            self._reason = "Audio first-crack detection is prepared for this session."
            try:
                adapter = self._detector_adapter_factory(self._config.first_crack)
                pipeline = self._audio_pipeline_factory(self._config.audio)
                pipeline.start()
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
            if capture_snapshot.latest_error is not None:
                self._mark_faulted_locked(f"Audio capture failed: {capture_snapshot.latest_error}")
                return self.snapshot()

            try:
                for window in pipeline.drain_windows():
                    self._processed_window_count += 1
                    result = integrate_first_crack_window_with_session(
                        config=self._config.first_crack,
                        adapter=adapter,
                        session_store=session_store,
                        session=session,
                        window=window,
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
            if self._pipeline is not None:
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
                pipeline.stop(timeout_seconds=self._stop_timeout_seconds)
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


def _build_audio_pipeline(config: AudioConfig) -> AudioCapturePipeline:
    return build_audio_capture_pipeline(config)


def _build_released_detector_adapter(config: FirstCrackConfig) -> FirstCrackDetectorAdapter:
    return build_released_onnx_first_crack_detector_adapter(config)


def _initial_status(config: FirstCrackConfig) -> FirstCrackRuntimeState:
    if config.mode == "disabled":
        return "disabled"
    if config.mode == "manual":
        if not config.allow_manual_override:
            return "unavailable"
        return "manual"
    return "pending"


def _initial_reason(config: FirstCrackConfig) -> str:
    if config.mode == "disabled":
        return "Automatic first-crack detection is disabled by configuration."
    if config.mode == "manual":
        if not config.allow_manual_override:
            return "Manual first-crack mode is configured, but manual override is disabled."
        return "Waiting for explicit mark_first_crack override."
    return "Audio first-crack detection has not started."
