"""Detector adapter boundary for first-crack audio windows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from coffee_roaster_mcp.artifacts import ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioWindow
from coffee_roaster_mcp.config import FirstCrackConfig, ModelPrecision
from coffee_roaster_mcp.session import RoastEvent, RoastSession, RoastSessionStore

FirstCrackDetectorEventKind = Literal["first_crack_detected"]


class FirstCrackDetectorError(RuntimeError):
    """Raised when first-crack detector output cannot be adapted."""


class FirstCrackDetectorBackend(Protocol):
    """Inference backend for first-crack detector windows."""

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        """Run detection for one audio window."""
        ...


@dataclass(frozen=True)
class FirstCrackDetectorOutput:
    """Raw detector decision for one audio window.

    Attributes:
        confirmed: Whether the detector confirmed first crack for the window.
        confidence: Optional model confidence for confirmed detections.
        detected_at_monotonic_seconds: Optional monotonic timestamp for the
            detection. When omitted, the adapter uses the end of the audio window.
    """

    confirmed: bool
    confidence: float | None = None
    detected_at_monotonic_seconds: float | None = None


@dataclass(frozen=True)
class FirstCrackDetectionEvent:
    """Confirmed first-crack detector event candidate.

    The detector adapter returns this as metadata only. Session integration is
    handled by the explicit E4-S9 integration helper so timeline writes stay
    visible and preserve the `RoastSessionStore` mutation boundary.

    Attributes:
        kind: Event kind intended for the session timeline.
        detected_at_monotonic_seconds: Monotonic timestamp for first crack.
        precision: Configured ONNX model precision.
        revision: Configured model repository revision.
        confidence: Optional detector confidence.
        repo_id: Configured model repository id.
        onnx_model_filename: Repository-relative ONNX model artifact.
        feature_extractor_filename: Repository-relative feature extractor artifact.
        window_sequence_number: Source audio window sequence number.
    """

    kind: FirstCrackDetectorEventKind
    detected_at_monotonic_seconds: float
    precision: ModelPrecision
    revision: str | None
    confidence: float | None
    repo_id: str
    onnx_model_filename: str
    feature_extractor_filename: str
    window_sequence_number: int

    def payload(self) -> dict[str, str | int | float | None]:
        """Return session-event payload metadata for this confirmed detection."""
        payload: dict[str, str | int | float | None] = {
            "source": "first_crack_detector",
            "detected_at_monotonic_seconds": self.detected_at_monotonic_seconds,
            "precision": self.precision,
            "revision": self.revision,
            "repo_id": self.repo_id,
            "onnx_model_filename": self.onnx_model_filename,
            "feature_extractor_filename": self.feature_extractor_filename,
            "window_sequence_number": self.window_sequence_number,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload


@dataclass(frozen=True)
class FirstCrackDetectorAdapter:
    """Adapt detector backend outputs into confirmed first-crack event candidates."""

    config: FirstCrackConfig
    artifacts: ResolvedDetectorArtifacts
    backend: FirstCrackDetectorBackend

    def process_window(self, window: AudioWindow) -> FirstCrackDetectionEvent | None:
        """Process one audio window and return a confirmed event candidate if present."""
        output = self.backend.detect(window)
        if type(output.confirmed) is not bool:
            raise FirstCrackDetectorError("detector output confirmed must be a boolean.")
        if not output.confirmed:
            return None

        confidence = _validate_optional_confidence(output.confidence)
        detected_at_monotonic_seconds = _detection_timestamp(window, output)
        return FirstCrackDetectionEvent(
            kind="first_crack_detected",
            detected_at_monotonic_seconds=detected_at_monotonic_seconds,
            precision=self.config.precision,
            revision=self.config.revision,
            confidence=confidence,
            repo_id=self.config.repo_id,
            onnx_model_filename=self.artifacts.onnx_model.filename,
            feature_extractor_filename=self.artifacts.feature_extractor_config.filename,
            window_sequence_number=window.sequence_number,
        )


@dataclass(frozen=True)
class FirstCrackTimelineIntegrationResult:
    """Result of applying one confirmed detector event to the session timeline.

    Attributes:
        event: Authoritative session event. Repeated confirmations return the
            original first-crack event instead of adding another timeline row.
        session: Snapshot of the authoritative session after integration.
    """

    event: RoastEvent
    session: RoastSession


def build_first_crack_detector_adapter(
    config: FirstCrackConfig,
    artifacts: ResolvedDetectorArtifacts,
    backend: FirstCrackDetectorBackend,
) -> FirstCrackDetectorAdapter:
    """Build the first-crack detector adapter from resolved detector dependencies."""
    return FirstCrackDetectorAdapter(
        config=config,
        artifacts=artifacts,
        backend=backend,
    )


def integrate_first_crack_window_with_session(
    *,
    config: FirstCrackConfig,
    adapter: FirstCrackDetectorAdapter,
    session_store: RoastSessionStore,
    session: RoastSession,
    window: AudioWindow,
) -> FirstCrackTimelineIntegrationResult | None:
    """Process one detector window and write one first-crack event if confirmed.

    The integration is intentionally gated to `first_crack.mode: audio`; manual
    and disabled modes leave detector output disconnected from the authoritative
    timeline. Session mutation stays behind `RoastSessionStore`, so duplicate
    detector confirmations return the original singleton `first_crack_detected`
    event without appending another row.

    Args:
        config: First-crack runtime configuration.
        adapter: Detector adapter that turns backend output into event metadata.
        session_store: Authoritative one-session mutation boundary.
        session: Active roast session to update.
        window: Audio window to process.

    Returns:
        Timeline integration result for confirmed output, or `None` when the
        mode is not audio or the detector did not confirm first crack.
    """
    if config.mode != "audio":
        return None

    detection_event = adapter.process_window(window)
    if detection_event is None:
        return None

    event, snapshot = session_store.record_first_crack_detection_snapshot(
        session,
        detected_at_monotonic_seconds=detection_event.detected_at_monotonic_seconds,
        payload=detection_event.payload(),
    )
    return FirstCrackTimelineIntegrationResult(event=event, session=snapshot)


def _detection_timestamp(
    window: AudioWindow,
    output: FirstCrackDetectorOutput,
) -> float:
    if output.detected_at_monotonic_seconds is None:
        detected_at = window.started_at_monotonic_seconds + window.duration_seconds
    else:
        detected_at = float(output.detected_at_monotonic_seconds)
    if not math.isfinite(detected_at):
        raise FirstCrackDetectorError(
            "detector output detected_at_monotonic_seconds must be finite."
        )
    return round(detected_at, 6)


def _validate_optional_confidence(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    normalized = float(confidence)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise FirstCrackDetectorError("detector output confidence must be between 0 and 1.")
    return normalized
