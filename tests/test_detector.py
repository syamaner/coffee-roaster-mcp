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
    FirstCrackWindowObservation,
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
    # FC is backdated to the confirming-window onset (window start 120.0),
    # not the audio-detection timestamp 120.42 (#168). The raw confirmation
    # timestamp stays available as confirmed_at_monotonic_seconds.
    assert event.detected_at_monotonic_seconds == 120.0
    assert event.confirmed_at_monotonic_seconds == 120.42
    assert event.precision == "fp32"
    assert event.revision == "v0.2.0"
    assert event.confidence == 0.87
    assert event.repo_id == "syamaner/custom-first-crack"
    assert event.onnx_model_filename == "onnx/fp32/model.onnx"
    assert event.feature_extractor_filename == "onnx/fp32/preprocessor_config.json"
    assert event.window_sequence_number == 7
    assert event.confirmed_by_window_sequence_number == 7
    assert event.positive_window_count == 1
    assert event.confidence_threshold == 0.9
    assert event.min_positive_windows == 1
    assert event.confirmation_window_seconds == 20.0
    assert event.detected_at_inferred is False
    assert event.payload() == {
        "source": "first_crack_detector",
        "detected_at_monotonic_seconds": 120.0,
        "confirmed_at_monotonic_seconds": 120.42,
        "precision": "fp32",
        "revision": "v0.2.0",
        "repo_id": "syamaner/custom-first-crack",
        "onnx_model_filename": "onnx/fp32/model.onnx",
        "feature_extractor_filename": "onnx/fp32/preprocessor_config.json",
        "window_sequence_number": 7,
        "confirmed_by_window_sequence_number": 7,
        "positive_window_count": 1,
        "confidence_threshold": 0.9,
        "min_positive_windows": 1,
        "confirmation_window_seconds": 20.0,
        "confidence": 0.87,
    }


def test_detector_adapter_backdates_inferred_timestamp_to_window_onset() -> None:
    window = _audio_window(started_at_monotonic_seconds=55.25, duration_seconds=1.0)
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),))
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(),
        _resolved_detector_artifacts(),
        backend,
    )

    event = adapter.process_window(window)

    assert event is not None
    # With no explicit detector timestamp, FC is backdated to the window onset
    # (55.25) rather than the inferred window end (56.25) — recovering the
    # window-duration slice of detector lag (#168). The window-end stays
    # available as the raw confirmation timestamp.
    assert event.detected_at_monotonic_seconds == 55.25
    assert event.confirmed_at_monotonic_seconds == 56.25
    assert event.confidence is None
    assert event.detected_at_inferred is True
    assert "confidence" not in event.payload()


def test_detector_adapter_confirms_from_recent_positive_windows() -> None:
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=True, confidence=0.61),
            FirstCrackDetectorOutput(confirmed=False, confidence=0.10),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.82),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.91),
        )
    )
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(
            confidence_threshold=0.6,
            min_positive_windows=3,
            confirmation_window_seconds=20.0,
        ),
        _resolved_detector_artifacts(),
        backend,
    )

    first = adapter.process_window(
        _audio_window(sequence_number=10, started_at_monotonic_seconds=100.0)
    )
    second = adapter.process_window(
        _audio_window(sequence_number=11, started_at_monotonic_seconds=103.0)
    )
    third = adapter.process_window(
        _audio_window(sequence_number=12, started_at_monotonic_seconds=106.0)
    )
    confirmed = adapter.process_window(
        _audio_window(sequence_number=13, started_at_monotonic_seconds=109.0)
    )

    assert first is None
    assert second is None
    assert third is None
    assert confirmed is not None
    # Backdated to the onset of the earliest positive window (seq 10 starts at
    # 100.0), not its window end (101.0) and not the confirming window (#168).
    assert confirmed.detected_at_monotonic_seconds == 100.0
    # Raw confirmation = the confirming window's (seq 13) end timestamp.
    assert confirmed.confirmed_at_monotonic_seconds == 110.0
    assert confirmed.window_sequence_number == 10
    assert confirmed.confirmed_by_window_sequence_number == 13
    assert confirmed.positive_window_count == 3
    assert confirmed.confidence == 0.61


def test_process_window_observed_captures_confidence_for_non_confirming_window() -> None:
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=False, confidence=0.55),))
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(confidence_threshold=0.6),
        _resolved_detector_artifacts(),
        backend,
    )

    observation = adapter.process_window_observed(
        _audio_window(sequence_number=21, started_at_monotonic_seconds=100.0)
    )

    assert isinstance(observation, FirstCrackWindowObservation)
    assert observation.window_sequence_number == 21
    # Confidence is captured even though the window never crossed the threshold,
    # so a miss is diagnosable (#175).
    assert observation.confidence == 0.55
    assert observation.positive_window_count == 0
    assert observation.confirmed is False
    assert observation.fc_status == "listening"
    assert observation.event is None


def test_process_window_observed_reports_candidate_before_confirmation() -> None:
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=True, confidence=0.71),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.83),
        )
    )
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(
            confidence_threshold=0.6,
            min_positive_windows=2,
            confirmation_window_seconds=20.0,
        ),
        _resolved_detector_artifacts(),
        backend,
    )

    candidate = adapter.process_window_observed(
        _audio_window(sequence_number=30, started_at_monotonic_seconds=100.0)
    )
    confirmed = adapter.process_window_observed(
        _audio_window(sequence_number=31, started_at_monotonic_seconds=103.0)
    )

    assert candidate.confidence == 0.71
    assert candidate.positive_window_count == 1
    assert candidate.confirmed is False
    assert candidate.fc_status == "candidate"
    assert candidate.event is None

    assert confirmed.confidence == 0.83
    assert confirmed.positive_window_count == 2
    assert confirmed.confirmed is True
    assert confirmed.fc_status == "confirmed"
    assert confirmed.event is not None
    assert confirmed.event.kind == "first_crack_detected"


def test_process_window_delegates_to_observed() -> None:
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True, confidence=0.95),))
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(confidence_threshold=0.6, min_positive_windows=1),
        _resolved_detector_artifacts(),
        backend,
    )

    event = adapter.process_window(
        _audio_window(sequence_number=40, started_at_monotonic_seconds=100.0)
    )

    assert event is not None
    assert event.kind == "first_crack_detected"
    assert event.confidence == 0.95


def test_detector_adapter_prunes_old_positive_windows_before_confirmation() -> None:
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=True, confidence=0.91),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.92),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.93),
        )
    )
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(min_positive_windows=2, confirmation_window_seconds=5.0),
        _resolved_detector_artifacts(),
        backend,
    )

    first = adapter.process_window(
        _audio_window(sequence_number=1, started_at_monotonic_seconds=100.0)
    )
    second = adapter.process_window(
        _audio_window(sequence_number=2, started_at_monotonic_seconds=106.0)
    )
    confirmed = adapter.process_window(
        _audio_window(sequence_number=3, started_at_monotonic_seconds=109.0)
    )

    assert first is None
    assert second is None
    assert confirmed is not None
    # Window 1 (end 101.0) is pruned outside the 5.0s confirmation span; the
    # earliest surviving positive is window 2, backdated to its onset 106.0
    # (not its end 107.0). Raw confirmation = window 3 end 110.0 (#168).
    assert confirmed.detected_at_monotonic_seconds == 106.0
    assert confirmed.confirmed_at_monotonic_seconds == 110.0
    assert confirmed.window_sequence_number == 2
    assert confirmed.confirmed_by_window_sequence_number == 3


def test_detector_adapter_backdates_to_first_confirming_crack_window() -> None:
    """FC reports the onset of the FIRST confirming crack window, recovering lag.

    Mirrors the roast-3 finding (#168, memory fc-detector-lag): the detector
    needs a window of cracks before it fires, so the confirmation tick lags the
    first audible crack. Backdating to the earliest positive window's onset
    recovers that gap deterministically. Here three positive windows span
    200.0 -> 208.0; the reported FC is the first window's onset (200.0), while
    the raw confirmation lands at the third window's end (209.0) — a ~9 s lag.
    """
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=True, confidence=0.95),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.96),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.97),
        )
    )
    adapter = build_first_crack_detector_adapter(
        FirstCrackConfig(min_positive_windows=3, confirmation_window_seconds=20.0),
        _resolved_detector_artifacts(),
        backend,
    )

    assert (
        adapter.process_window(_audio_window(sequence_number=1, started_at_monotonic_seconds=200.0))
        is None
    )
    assert (
        adapter.process_window(_audio_window(sequence_number=2, started_at_monotonic_seconds=204.0))
        is None
    )
    confirmed = adapter.process_window(
        _audio_window(sequence_number=3, started_at_monotonic_seconds=208.0)
    )

    assert confirmed is not None
    # Reported FC = onset of the first confirming crack window (seq 1).
    assert confirmed.detected_at_monotonic_seconds == 200.0
    assert confirmed.window_sequence_number == 1
    # Raw confirmation = the third window's (inferred) end timestamp.
    assert confirmed.confirmed_at_monotonic_seconds == 209.0
    assert confirmed.confirmed_by_window_sequence_number == 3
    # The backdating recovers the full detector-confirmation lag.
    assert confirmed.confirmed_at_monotonic_seconds - confirmed.detected_at_monotonic_seconds == 9.0
    assert confirmed.payload()["detected_at_monotonic_seconds"] == 200.0
    assert confirmed.payload()["confirmed_at_monotonic_seconds"] == 209.0


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


def test_released_onnx_backend_uses_configured_confidence_threshold(tmp_path: Path) -> None:
    backend = build_released_onnx_first_crack_detector_backend(
        FirstCrackConfig(mode="audio", confidence_threshold=0.6),
        _resolved_detector_artifacts_with_files(tmp_path),
        session_factory=lambda _path, _threads: FakeOnnxSession(((0.0, 0.5),)),
        feature_extractor_factory=lambda _path: FakeFeatureExtractor(),
    )

    output = backend.detect(_audio_window(sample_rate=16_000))

    assert output.confirmed is True
    assert output.confidence is not None
    assert abs(output.confidence - 0.622459) < 0.000001


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
