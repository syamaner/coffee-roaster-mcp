from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioWindow
from coffee_roaster_mcp.config import FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorError,
    FirstCrackDetectorOutput,
    build_first_crack_detector_adapter,
)


class MockDetectorBackend:
    def __init__(self, outputs: tuple[FirstCrackDetectorOutput, ...]) -> None:
        self._outputs = list(outputs)
        self.windows: list[AudioWindow] = []

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        self.windows.append(window)
        return self._outputs.pop(0)


class BadConfirmationBackend:
    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        return cast(
            FirstCrackDetectorOutput,
            SimpleNamespace(
                confirmed=1,
                confidence=None,
                detected_at_monotonic_seconds=None,
            ),
        )


def test_detector_adapter_ignores_unconfirmed_detector_output() -> None:
    window = _audio_window()
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=False),))
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(revision="v0.1.0"),
        _resolved_detector_artifacts(),
        backend,
    )

    event = adapter.process_window(window)

    assert event is None
    assert backend.windows == [window]


def test_detector_adapter_maps_confirmed_output_to_first_crack_event() -> None:
    window = _audio_window(sequence_number=7, started_at_monotonic_seconds=120.0)
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.87,
                detected_at_monotonic_seconds=120.42,
            ),
        )
    )
    config = FirstCrackConfig(
        repo_id="syamaner/custom-first-crack",
        revision="v0.2.0",
        precision="fp32",
    )
    artifacts = _resolved_detector_artifacts(
        repo_id="syamaner/custom-first-crack",
        revision="v0.2.0",
        onnx_filename="onnx/fp32/model.onnx",
        feature_extractor_filename="onnx/fp32/preprocessor_config.json",
    )
    adapter = FirstCrackDetectorAdapter(
        config=config,
        artifacts=artifacts,
        backend=backend,
    )

    event = adapter.process_window(window)

    assert event is not None
    assert event.kind == "first_crack_detected"
    assert event.detected_at_monotonic_seconds == 120.42
    assert event.precision == "fp32"
    assert event.revision == "v0.2.0"
    assert event.confidence == 0.87
    assert event.repo_id == "syamaner/custom-first-crack"
    assert event.onnx_model_filename == "onnx/fp32/model.onnx"
    assert event.feature_extractor_filename == "onnx/fp32/preprocessor_config.json"
    assert event.window_sequence_number == 7
    assert event.payload() == {
        "source": "first_crack_detector",
        "detected_at_monotonic_seconds": 120.42,
        "precision": "fp32",
        "revision": "v0.2.0",
        "repo_id": "syamaner/custom-first-crack",
        "onnx_model_filename": "onnx/fp32/model.onnx",
        "feature_extractor_filename": "onnx/fp32/preprocessor_config.json",
        "window_sequence_number": 7,
        "confidence": 0.87,
    }


def test_detector_adapter_uses_window_end_timestamp_when_output_has_no_timestamp() -> None:
    window = _audio_window(started_at_monotonic_seconds=55.25, duration_seconds=1.0)
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),))
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(),
        _resolved_detector_artifacts(),
        backend,
    )

    event = adapter.process_window(window)

    assert event is not None
    assert event.detected_at_monotonic_seconds == 56.25
    assert event.confidence is None
    assert "confidence" not in event.payload()


@pytest.mark.parametrize("confidence", (-0.01, 1.01, float("nan")))
def test_detector_adapter_rejects_invalid_confidence(confidence: float) -> None:
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(),
        _resolved_detector_artifacts(),
        MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True, confidence=confidence),)),
    )

    with pytest.raises(FirstCrackDetectorError, match="confidence"):
        adapter.process_window(_audio_window())


def test_detector_adapter_rejects_non_finite_detection_timestamp() -> None:
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(),
        _resolved_detector_artifacts(),
        MockDetectorBackend(
            (
                FirstCrackDetectorOutput(
                    confirmed=True,
                    detected_at_monotonic_seconds=float("inf"),
                ),
            )
        ),
    )

    with pytest.raises(FirstCrackDetectorError, match="detected_at_monotonic_seconds"):
        adapter.process_window(_audio_window())


def test_detector_adapter_rejects_non_boolean_confirmation() -> None:
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(),
        _resolved_detector_artifacts(),
        BadConfirmationBackend(),
    )

    with pytest.raises(FirstCrackDetectorError, match="confirmed"):
        adapter.process_window(_audio_window())


def _audio_window(
    *,
    sequence_number: int = 3,
    started_at_monotonic_seconds: float = 10.0,
    duration_seconds: float = 1.0,
) -> AudioWindow:
    return AudioWindow(
        sequence_number=sequence_number,
        input_device="mock-audio",
        sample_rate=4,
        started_at_monotonic_seconds=started_at_monotonic_seconds,
        duration_seconds=duration_seconds,
        samples=(0.1, 0.2, 0.3, 0.4),
    )


def _resolved_detector_artifacts(
    *,
    repo_id: str = "syamaner/coffee-first-crack-detection",
    revision: str | None = None,
    onnx_filename: str = "onnx/int8/model_quantized.onnx",
    feature_extractor_filename: str = "onnx/int8/preprocessor_config.json",
) -> ResolvedDetectorArtifacts:
    return ResolvedDetectorArtifacts(
        onnx_model=ResolvedArtifact(
            repo_id=repo_id,
            revision=revision,
            filename=onnx_filename,
            local_path=Path("/tmp/model.onnx"),
        ),
        feature_extractor_config=ResolvedArtifact(
            repo_id=repo_id,
            revision=revision,
            filename=feature_extractor_filename,
            local_path=Path("/tmp/preprocessor_config.json"),
        ),
    )
