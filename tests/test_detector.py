from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import coffee_roaster_mcp.detector as detector_module
from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioWindow
from coffee_roaster_mcp.config import FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorError,
    FirstCrackDetectorOutput,
    OnnxFeatureExtractor,
    OnnxInferenceSession,
    OnnxInputInfo,
    build_first_crack_detector_adapter,
    build_released_onnx_first_crack_detector_adapter,
    build_released_onnx_first_crack_detector_backend,
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


class FakeInputInfo:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeOnnxSession:
    def __init__(self, logits: object = ((-3.0, 3.0),)) -> None:
        self.logits = logits
        self.input_feed: list[dict[str, object]] = []

    def get_inputs(self) -> tuple[OnnxInputInfo, ...]:
        return (FakeInputInfo("input_values"),)

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, object],
    ) -> tuple[object, ...]:
        self.input_feed.append(dict(input_feed))
        return (self.logits,)


class EmptyInputOnnxSession(FakeOnnxSession):
    def get_inputs(self) -> tuple[OnnxInputInfo, ...]:
        return ()


class FakeFeatureExtractor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        raw_speech: Sequence[Sequence[float]],
        *,
        sampling_rate: int,
        return_tensors: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "raw_speech": raw_speech,
                "sampling_rate": sampling_rate,
                "return_tensors": return_tensors,
            }
        )
        return {"input_values": (("features",),)}


class MissingInputFeatureExtractor(FakeFeatureExtractor):
    def __call__(
        self,
        raw_speech: Sequence[Sequence[float]],
        *,
        sampling_rate: int,
        return_tensors: str,
    ) -> dict[str, object]:
        super().__call__(raw_speech, sampling_rate=sampling_rate, return_tensors=return_tensors)
        return {"not_input_values": ()}


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
    assert event.detected_at_inferred is False
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
    assert event.detected_at_inferred is True
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


def test_released_onnx_backend_builds_from_resolved_artifacts(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(tmp_path)
    config = FirstCrackConfig(mode="audio", onnx_threads=4)
    session = FakeOnnxSession()
    extractor = FakeFeatureExtractor()
    session_calls: list[tuple[Path, int]] = []
    extractor_calls: list[Path] = []

    def session_factory(model_path: Path, onnx_threads: int) -> OnnxInferenceSession:
        session_calls.append((model_path, onnx_threads))
        return session

    def extractor_factory(preprocessor_config_path: Path) -> OnnxFeatureExtractor:
        extractor_calls.append(preprocessor_config_path)
        return extractor

    backend = build_released_onnx_first_crack_detector_backend(
        config,
        artifacts,
        session_factory=session_factory,
        feature_extractor_factory=extractor_factory,
    )

    output = backend.detect(_audio_window(sample_rate=16_000))

    assert output.confirmed is True
    assert output.confidence is not None
    assert abs(output.confidence - 0.997527) < 0.000001
    assert session_calls == [(artifacts.onnx_model.local_path, 4)]
    assert extractor_calls == [artifacts.feature_extractor_config.local_path]
    assert extractor.calls == [
        {
            "raw_speech": [[0.1, 0.2, 0.3, 0.4]],
            "sampling_rate": 16_000,
            "return_tensors": "np",
        }
    ]
    assert session.input_feed == [{"input_values": (("features",),)}]


def test_released_onnx_adapter_resolves_artifacts_before_backend(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(tmp_path)
    calls: list[dict[str, str | None]] = []

    def downloader(*, repo_id: str, filename: str, revision: str | None) -> str:
        calls.append({"repo_id": repo_id, "filename": filename, "revision": revision})
        if filename == "onnx/fp32/model.onnx":
            return str(artifacts.onnx_model.local_path)
        if filename == "onnx/fp32/preprocessor_config.json":
            return str(artifacts.feature_extractor_config.local_path)
        raise AssertionError(f"unexpected artifact {filename}")

    adapter = build_released_onnx_first_crack_detector_adapter(
        FirstCrackConfig(mode="audio", precision="fp32", revision="release-sha"),
        downloader=downloader,
        session_factory=lambda _path, _threads: FakeOnnxSession(),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    event = adapter.process_window(_audio_window(sample_rate=16_000))

    assert event is not None
    assert event.precision == "fp32"
    assert event.repo_id == "syamaner/coffee-first-crack-detection"
    assert event.revision == "release-sha"
    assert event.onnx_model_filename == "onnx/fp32/model.onnx"
    assert event.feature_extractor_filename == "onnx/fp32/preprocessor_config.json"
    assert event.confidence is not None
    assert calls == [
        {
            "repo_id": "syamaner/coffee-first-crack-detection",
            "filename": "onnx/fp32/model.onnx",
            "revision": "release-sha",
        },
        {
            "repo_id": "syamaner/coffee-first-crack-detection",
            "filename": "onnx/fp32/preprocessor_config.json",
            "revision": "release-sha",
        },
    ]


def test_released_onnx_backend_returns_unconfirmed_below_threshold(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(((4.0, -4.0),)),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    output = backend.detect(_audio_window(sample_rate=16_000))

    assert output.confirmed is False
    assert output.confidence is not None
    assert abs(output.confidence - 0.00033535) < 0.000001


def test_released_onnx_backend_rejects_non_audio_mode(tmp_path: Path) -> None:
    with pytest.raises(FirstCrackDetectorError, match="mode 'audio'"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="disabled"),
            _resolved_detector_artifacts_with_files(tmp_path),
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_rejects_invalid_onnx_threads(tmp_path: Path) -> None:
    with pytest.raises(FirstCrackDetectorError, match="onnx_threads"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio", onnx_threads=0),
            _resolved_detector_artifacts_with_files(tmp_path),
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_rejects_invalid_preprocessor_config(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(
        tmp_path,
        preprocessor_config='{"sampling_rate": false}',
    )

    with pytest.raises(FirstCrackDetectorError, match="sampling_rate"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            artifacts,
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_rejects_missing_preprocessor_config(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(tmp_path)
    artifacts.feature_extractor_config.local_path.unlink()

    with pytest.raises(FirstCrackDetectorError, match="Could not read"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            artifacts,
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_rejects_malformed_preprocessor_config(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(tmp_path, preprocessor_config="{")

    with pytest.raises(FirstCrackDetectorError, match="Could not parse"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            artifacts,
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_rejects_non_object_preprocessor_config(tmp_path: Path) -> None:
    artifacts = _resolved_detector_artifacts_with_files(tmp_path, preprocessor_config="[]")

    with pytest.raises(FirstCrackDetectorError, match="JSON object"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            artifacts,
            session_factory=lambda _path, _threads: FakeOnnxSession(),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_allows_preprocessor_without_sampling_rate(
    tmp_path: Path,
) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path, preprocessor_config="{}"),
        session_factory=lambda _path, _threads: FakeOnnxSession((10.0,)),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    output = backend.detect(_audio_window(sample_rate=8_000))

    assert output.confirmed is True
    assert output.confidence is not None
    assert abs(output.confidence - 0.9999546) < 0.000001


def test_released_onnx_backend_rejects_sample_rate_mismatch(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="sample_rate"):
        backend.detect(_audio_window(sample_rate=8_000))


def test_released_onnx_backend_rejects_missing_feature_extractor_input(
    tmp_path: Path,
) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(),
        feature_extractor_factory=lambda _path: MissingInputFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="input_values"):
        backend.detect(_audio_window(sample_rate=16_000))


def test_released_onnx_backend_rejects_model_without_inputs(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: EmptyInputOnnxSession(),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="no inputs"):
        backend.detect(_audio_window(sample_rate=16_000))


def test_released_onnx_backend_rejects_empty_model_outputs(tmp_path: Path) -> None:
    class EmptyOutputOnnxSession(FakeOnnxSession):
        def run(
            self,
            output_names: Sequence[str] | None,
            input_feed: Mapping[str, object],
        ) -> tuple[object, ...]:
            return ()

    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: EmptyOutputOnnxSession(),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="returned no outputs"):
        backend.detect(_audio_window(sample_rate=16_000))


def test_released_onnx_backend_rejects_empty_logits(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(()),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="logits must not be empty"):
        backend.detect(_audio_window(sample_rate=16_000))


def test_released_onnx_backend_rejects_non_numeric_logits(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession("first-crack"),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    with pytest.raises(FirstCrackDetectorError, match="could not be interpreted"):
        backend.detect(_audio_window(sample_rate=16_000))


def test_released_onnx_backend_supports_array_like_outputs(tmp_path: Path) -> None:
    class ArrayLikeLogits:
        def tolist(self) -> list[list[float]]:
            return [[-3.0, 3.0]]

    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio"),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(ArrayLikeLogits()),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    output = backend.detect(_audio_window(sample_rate=16_000))

    assert output.confirmed is True


def test_released_onnx_backend_fails_clearly_when_onnxruntime_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_onnxruntime(name: str, package: str | None = None) -> object:
        if name == "onnxruntime":
            raise ImportError(name)
        return __import__(name)

    monkeypatch.setattr(detector_module, "import_module", missing_onnxruntime)

    with pytest.raises(FirstCrackDetectorError, match="onnxruntime"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            _resolved_detector_artifacts_with_files(tmp_path),
            feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
        )


def test_released_onnx_backend_fails_clearly_when_transformers_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_transformers(name: str, package: str | None = None) -> object:
        if name == "transformers":
            raise ImportError(name)
        return __import__(name)

    monkeypatch.setattr(detector_module, "import_module", missing_transformers)

    with pytest.raises(FirstCrackDetectorError, match="ASTFeatureExtractor"):
        build_released_onnx_first_crack_detector_backend(
            FirstCrackConfig(mode="audio"),
            _resolved_detector_artifacts_with_files(tmp_path),
            session_factory=lambda _path, _threads: FakeOnnxSession(),
        )


def _audio_window(
    *,
    sequence_number: int = 3,
    started_at_monotonic_seconds: float = 10.0,
    duration_seconds: float = 1.0,
    sample_rate: int = 4,
) -> AudioWindow:
    return AudioWindow(
        sequence_number=sequence_number,
        input_device="mock-audio",
        sample_rate=sample_rate,
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


def _resolved_detector_artifacts_with_files(
    tmp_path: Path,
    *,
    preprocessor_config: str = '{"sampling_rate": 16000}',
) -> ResolvedDetectorArtifacts:
    model_path = tmp_path / "onnx" / "int8" / "model_quantized.onnx"
    preprocessor_path = tmp_path / "onnx" / "int8" / "preprocessor_config.json"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"fake-onnx")
    preprocessor_path.write_text(preprocessor_config, encoding="utf-8")
    return ResolvedDetectorArtifacts(
        onnx_model=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="release-sha",
            filename="onnx/int8/model_quantized.onnx",
            local_path=model_path,
        ),
        feature_extractor_config=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="release-sha",
            filename="onnx/int8/preprocessor_config.json",
            local_path=preprocessor_path,
        ),
    )
