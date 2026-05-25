"""Detector adapter boundary for first-crack audio windows."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MethodType
from typing import Any, Literal, Protocol, cast

from coffee_roaster_mcp.artifacts import (
    HuggingFaceDownloader,
    ResolvedDetectorArtifacts,
    resolve_first_crack_detector_artifacts,
)
from coffee_roaster_mcp.audio import AudioWindow
from coffee_roaster_mcp.config import FirstCrackConfig, ModelPrecision
from coffee_roaster_mcp.session import RoastEvent, RoastSession, RoastSessionStore

FirstCrackDetectorEventKind = Literal["first_crack_detected"]
DEFAULT_FIRST_CRACK_CONFIDENCE_THRESHOLD = 0.9


class FirstCrackDetectorError(RuntimeError):
    """Raised when first-crack detector output cannot be adapted."""


class FirstCrackDetectorBackend(Protocol):
    """Inference backend for first-crack detector windows."""

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        """Run detection for one audio window."""
        ...


class OnnxInputInfo(Protocol):
    """Input metadata exposed by an ONNX Runtime inference session."""

    name: str


class OnnxInferenceSession(Protocol):
    """Narrow ONNX Runtime session protocol used by the detector backend."""

    def get_inputs(self) -> Sequence[OnnxInputInfo]:
        """Return model input metadata."""
        ...

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, object],
    ) -> Sequence[object]:
        """Run one ONNX inference call."""
        ...


class OnnxFeatureExtractor(Protocol):
    """Callable feature extractor loaded from a resolved preprocessor config."""

    def __call__(
        self,
        raw_speech: Sequence[Sequence[float]],
        *,
        sampling_rate: int,
        return_tensors: str,
    ) -> Mapping[str, object]:
        """Convert raw mono audio into model input tensors."""
        ...


OnnxSessionFactory = Callable[[Path, int], OnnxInferenceSession]
OnnxFeatureExtractorFactory = Callable[[Path], OnnxFeatureExtractor]


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
        detected_at_inferred: Whether the detection timestamp was inferred from
            the source window end because the backend omitted an explicit timestamp.
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
    detected_at_inferred: bool

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
            detected_at_inferred=output.detected_at_monotonic_seconds is None,
        )


@dataclass(frozen=True)
class OnnxPreprocessorConfig:
    """Validated detector preprocessor configuration metadata."""

    sampling_rate: int | None


@dataclass(frozen=True)
class OnnxFirstCrackDetectorBackend:
    """ONNX Runtime backend for released first-crack detector artifacts."""

    session: OnnxInferenceSession
    feature_extractor: OnnxFeatureExtractor
    preprocessor_config: OnnxPreprocessorConfig
    confidence_threshold: float = DEFAULT_FIRST_CRACK_CONFIDENCE_THRESHOLD

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        """Run ONNX inference for one audio window."""
        if (
            self.preprocessor_config.sampling_rate is not None
            and window.sample_rate != self.preprocessor_config.sampling_rate
        ):
            raise FirstCrackDetectorError(
                "Audio window sample_rate "
                f"{window.sample_rate} does not match first-crack preprocessor "
                f"sampling_rate {self.preprocessor_config.sampling_rate}."
            )

        inputs = self.feature_extractor(
            [list(window.samples)],
            sampling_rate=window.sample_rate,
            return_tensors="np",
        )
        model_input = inputs.get("input_values")
        if model_input is None:
            raise FirstCrackDetectorError(
                "First-crack feature extractor output must include 'input_values'."
            )

        input_infos = self.session.get_inputs()
        if not input_infos:
            raise FirstCrackDetectorError("First-crack ONNX model exposes no inputs.")

        outputs = self.session.run(None, {input_infos[0].name: model_input})
        confidence = _first_crack_confidence(outputs)
        return FirstCrackDetectorOutput(
            confirmed=confidence >= self.confidence_threshold,
            confidence=confidence,
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


def build_released_onnx_first_crack_detector_backend(
    config: FirstCrackConfig,
    artifacts: ResolvedDetectorArtifacts,
    *,
    session_factory: OnnxSessionFactory | None = None,
    feature_extractor_factory: OnnxFeatureExtractorFactory | None = None,
) -> OnnxFirstCrackDetectorBackend:
    """Build the released-artifact ONNX first-crack detector backend.

    Args:
        config: First-crack runtime configuration. The released ONNX backend is
            only valid for `first_crack.mode: audio`.
        artifacts: Resolved ONNX model and precision-specific preprocessor
            config artifacts.
        session_factory: Optional test double for ONNX Runtime session creation.
        feature_extractor_factory: Optional test double for local feature
            extractor loading.

    Returns:
        ONNX detector backend ready to process audio windows.

    Raises:
        FirstCrackDetectorError: If mode, artifacts, preprocessor config, or
            runtime dependencies are invalid.
    """
    if config.mode != "audio":
        raise FirstCrackDetectorError(
            "Released ONNX first-crack detector backend requires first_crack.mode 'audio'."
        )
    if config.onnx_threads <= 0:
        raise FirstCrackDetectorError("first_crack.onnx_threads must be greater than 0.")

    preprocessor_config = _load_onnx_preprocessor_config(
        artifacts.feature_extractor_config.local_path
    )
    create_session = session_factory or _build_onnx_runtime_session
    create_feature_extractor = feature_extractor_factory or _build_ast_feature_extractor
    return OnnxFirstCrackDetectorBackend(
        session=create_session(artifacts.onnx_model.local_path, config.onnx_threads),
        feature_extractor=create_feature_extractor(artifacts.feature_extractor_config.local_path),
        preprocessor_config=preprocessor_config,
    )


def build_released_onnx_first_crack_detector_adapter(
    config: FirstCrackConfig,
    *,
    downloader: HuggingFaceDownloader | None = None,
    session_factory: OnnxSessionFactory | None = None,
    feature_extractor_factory: OnnxFeatureExtractorFactory | None = None,
) -> FirstCrackDetectorAdapter:
    """Resolve released detector artifacts and build the ONNX detector adapter.

    Args:
        config: First-crack runtime configuration.
        downloader: Optional test double for Hugging Face artifact resolution.
        session_factory: Optional test double for ONNX Runtime session creation.
        feature_extractor_factory: Optional test double for local feature
            extractor loading.

    Returns:
        Detector adapter backed by the released-artifact ONNX runtime backend.
    """
    artifacts = resolve_first_crack_detector_artifacts(config, downloader=downloader)
    backend = build_released_onnx_first_crack_detector_backend(
        config,
        artifacts,
        session_factory=session_factory,
        feature_extractor_factory=feature_extractor_factory,
    )
    return build_first_crack_detector_adapter(config, artifacts, backend)


def integrate_first_crack_window_with_session(
    *,
    config: FirstCrackConfig,
    adapter: FirstCrackDetectorAdapter,
    session_store: RoastSessionStore,
    session: RoastSession,
    window: AudioWindow,
    max_future_seconds: float | None = None,
    allow_future_timeline: bool = False,
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
        max_future_seconds: Optional override for future detector timestamps.
            When omitted, inferred window-end timestamps may be up to one
            detector window ahead of the integration clock.
        allow_future_timeline: Whether to preserve detector timestamps that are
            ahead of wall-clock elapsed time. This is only intended for
            detector-paced replay of recorded source audio.

    Returns:
        Timeline integration result for confirmed output, or `None` when the
        mode is not audio or the detector did not confirm first crack.
    """
    if config.mode != "audio":
        return None
    if not session.active or session.phase != "roasting":
        return None

    detection_event = adapter.process_window(window)
    if detection_event is None:
        return None

    future_tolerance = _future_tolerance_for_detection(
        detection_event=detection_event,
        window=window,
        max_future_seconds=max_future_seconds,
        allow_future_timeline=allow_future_timeline,
    )
    event, snapshot = session_store.record_first_crack_detection_snapshot(
        session,
        detected_at_monotonic_seconds=detection_event.detected_at_monotonic_seconds,
        max_future_seconds=future_tolerance,
        payload=detection_event.payload(),
    )
    return FirstCrackTimelineIntegrationResult(event=event, session=snapshot)


def _future_tolerance_for_detection(
    *,
    detection_event: FirstCrackDetectionEvent,
    window: AudioWindow,
    max_future_seconds: float | None,
    allow_future_timeline: bool,
) -> float | None:
    if allow_future_timeline:
        return None
    if max_future_seconds is not None:
        return max_future_seconds
    if detection_event.detected_at_inferred:
        return window.duration_seconds
    return 0.0


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


def _load_onnx_preprocessor_config(path: Path) -> OnnxPreprocessorConfig:
    try:
        raw_config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FirstCrackDetectorError(
            f"Could not read first-crack preprocessor config {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FirstCrackDetectorError(
            f"Could not parse first-crack preprocessor config {path}: {exc}"
        ) from exc

    if not isinstance(raw_config, Mapping):
        raise FirstCrackDetectorError("First-crack preprocessor config must be a JSON object.")
    raw_mapping = cast(Mapping[str, object], raw_config)
    sampling_rate = raw_mapping.get("sampling_rate")
    if sampling_rate is None:
        return OnnxPreprocessorConfig(sampling_rate=None)
    if isinstance(sampling_rate, bool) or not isinstance(sampling_rate, int) or sampling_rate <= 0:
        raise FirstCrackDetectorError(
            "First-crack preprocessor config sampling_rate must be a positive integer."
        )
    return OnnxPreprocessorConfig(sampling_rate=sampling_rate)


def _build_onnx_runtime_session(model_path: Path, onnx_threads: int) -> OnnxInferenceSession:
    try:
        runtime_module = import_module("onnxruntime")
    except ImportError as exc:
        raise FirstCrackDetectorError(
            "first_crack.mode 'audio' requires the onnxruntime package to run released "
            "ONNX artifacts."
        ) from exc

    try:
        session_options = runtime_module.SessionOptions()
        session_options.intra_op_num_threads = onnx_threads
        session_options.inter_op_num_threads = 1
        session = runtime_module.InferenceSession(
            str(model_path),
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        raise FirstCrackDetectorError(
            f"Could not initialize first-crack ONNX Runtime session for {model_path}: {exc}"
        ) from exc
    return cast(OnnxInferenceSession, session)


def _build_ast_feature_extractor(preprocessor_config_path: Path) -> OnnxFeatureExtractor:
    try:
        transformers_module = import_module("transformers")
        extractor_type = transformers_module.ASTFeatureExtractor
    except (AttributeError, ImportError) as exc:
        raise FirstCrackDetectorError(
            "first_crack.mode 'audio' requires transformers.ASTFeatureExtractor to load "
            "the released preprocessor config."
        ) from exc

    try:
        extractor = extractor_type.from_pretrained(str(preprocessor_config_path.parent))
    except Exception as exc:
        raise FirstCrackDetectorError(
            "Could not initialize first-crack AST feature extractor from "
            f"{preprocessor_config_path}: {exc}"
        ) from exc
    _patch_ast_feature_extractor_for_numpy_only_runtime(extractor)
    return cast(OnnxFeatureExtractor, extractor)


def _patch_ast_feature_extractor_for_numpy_only_runtime(extractor: Any) -> None:
    extractor_module = import_module(extractor.__class__.__module__)
    if "torch" in extractor_module.__dict__:
        return

    try:
        numpy_module = import_module("numpy")
        spectrogram = extractor_module.spectrogram
    except (AttributeError, ImportError) as exc:
        raise FirstCrackDetectorError(
            "Could not configure AST feature extraction for ONNX-only runtime."
        ) from exc

    def extract_fbank_features(self: Any, waveform: Any, max_length: int) -> Any:
        squeezed = numpy_module.squeeze(waveform)
        fbank = spectrogram(
            squeezed,
            self.window,
            frame_length=400,
            hop_length=160,
            fft_length=512,
            power=2.0,
            center=False,
            preemphasis=0.97,
            mel_filters=self.mel_filters,
            log_mel="log",
            mel_floor=1.192092955078125e-07,
            remove_dc_offset=True,
        ).T

        frame_count = fbank.shape[0]
        difference = max_length - frame_count
        if difference > 0:
            fbank = numpy_module.pad(fbank, ((0, difference), (0, 0)), mode="constant")
        elif difference < 0:
            fbank = fbank[0:max_length, :]
        return fbank

    extractor._extract_fbank_features = MethodType(extract_fbank_features, extractor)


def _first_crack_confidence(outputs: Sequence[object]) -> float:
    if not outputs:
        raise FirstCrackDetectorError("First-crack ONNX model returned no outputs.")

    try:
        rows = _logit_rows(outputs[0])
    except Exception as exc:
        raise FirstCrackDetectorError(
            f"First-crack ONNX output could not be interpreted as logits: {exc}"
        ) from exc

    if not rows or not rows[0]:
        raise FirstCrackDetectorError("First-crack ONNX output logits must not be empty.")

    first_row = rows[0]
    if len(first_row) == 1:
        value = float(first_row[0])
        confidence = 1.0 / (1.0 + math.exp(-value))
    else:
        first_crack_index = 1
        max_logit = max(first_row)
        exp_logits = [math.exp(logit - max_logit) for logit in first_row]
        exp_sum = sum(exp_logits)
        confidence = exp_logits[first_crack_index] / exp_sum

    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise FirstCrackDetectorError(
            "First-crack ONNX output confidence must be finite and between 0 and 1."
        )
    return confidence


def _logit_rows(value: object) -> list[list[float]]:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()

    if isinstance(value, bool):
        raise TypeError("boolean logits are not supported")
    if isinstance(value, (int, float)):
        return [[float(value)]]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("logits must be numeric or a numeric sequence")

    raw_items = list(cast(Sequence[object], value))
    if not raw_items:
        return []
    if all(_is_number(item) for item in raw_items):
        return [[float(cast(int | float, item)) for item in raw_items]]

    rows: list[list[float]] = []
    for item in raw_items:
        rows.extend(_logit_rows(item))
    return rows


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))
