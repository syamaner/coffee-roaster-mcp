from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioWindow
from coffee_roaster_mcp.config import FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorOutput,
    build_first_crack_detector_adapter,
    integrate_first_crack_window_with_session,
)
from coffee_roaster_mcp.session import RoastSessionStore, compute_roast_metrics


class ClockHarness:
    """Deterministic wall-clock and monotonic clock supplier for integration tests."""

    def __init__(self) -> None:
        self.utc_value = datetime(2026, 5, 17, 14, 0, tzinfo=UTC)
        self.monotonic_value = 500.0

    def utc_now(self) -> datetime:
        return self.utc_value

    def monotonic_now(self) -> float:
        return self.monotonic_value


class MockDetectorBackend:
    """Detector backend with deterministic queued outputs."""

    def __init__(self, outputs: tuple[FirstCrackDetectorOutput, ...]) -> None:
        self._outputs = list(outputs)
        self.windows: list[AudioWindow] = []

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        self.windows.append(window)
        return self._outputs.pop(0)


def test_integrator_ignores_detector_output_outside_audio_mode() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),))
    config = FirstCrackConfig(mode="manual")
    adapter = build_first_crack_detector_adapter(config, _resolved_detector_artifacts(), backend)

    result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(),
    )

    assert result is None
    assert backend.windows == []
    assert [event.kind for event in session.event_timeline] == ["beans_added"]
    assert session.phase == "roasting"


def test_integrator_ignores_unconfirmed_detector_output() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=False),))
    config = FirstCrackConfig(mode="audio")
    adapter = build_first_crack_detector_adapter(config, _resolved_detector_artifacts(), backend)

    result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(sequence_number=4),
    )

    assert result is None
    assert [window.sequence_number for window in backend.windows] == [4]
    assert [event.kind for event in session.event_timeline] == ["beans_added"]
    assert session.first_crack_at_utc is None


def test_confirmed_detector_output_records_first_crack_event_once() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.91,
                detected_at_monotonic_seconds=537.25,
            ),
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.99,
                detected_at_monotonic_seconds=538.0,
            ),
        )
    )
    config = FirstCrackConfig(mode="audio", revision="v0.1.0")
    adapter = build_first_crack_detector_adapter(config, _resolved_detector_artifacts(), backend)

    clock.utc_value = datetime(2026, 5, 17, 14, 6, tzinfo=UTC)
    clock.monotonic_value = 540.0
    first_result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(sequence_number=11, started_at_monotonic_seconds=536.5),
    )
    second_result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(sequence_number=12, started_at_monotonic_seconds=537.5),
    )

    assert first_result is not None
    assert second_result is not None
    assert first_result.event == second_result.event
    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
    ]
    assert session.phase == "development"
    assert session.first_crack_at_utc == datetime(
        2026,
        5,
        17,
        14,
        0,
        37,
        250000,
        tzinfo=UTC,
    )
    assert session.first_crack_monotonic_seconds == 37.25
    assert first_result.event.monotonic_seconds == 37.25
    assert first_result.event.payload == {
        "source": "first_crack_detector",
        "detected_at_monotonic_seconds": 537.25,
        "precision": "int8",
        "revision": "v0.1.0",
        "repo_id": "syamaner/coffee-first-crack-detection",
        "onnx_model_filename": "onnx/int8/model_quantized.onnx",
        "feature_extractor_filename": "onnx/int8/preprocessor_config.json",
        "window_sequence_number": 11,
        "confidence": 0.91,
    }
    assert len(first_result.session.event_timeline) == 2
    assert len(second_result.session.event_timeline) == 2
    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)
    assert metrics.development_time_seconds == 2.75


def test_automatic_detection_does_not_require_manual_override_permission() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    clock.monotonic_value = 502.0
    config = FirstCrackConfig(mode="audio", allow_manual_override=False)
    adapter = build_first_crack_detector_adapter(
        config,
        _resolved_detector_artifacts(),
        MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),)),
    )

    result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(),
    )

    assert result is not None
    assert result.event.kind == "first_crack_detected"
    assert session.phase == "development"


def test_adapter_default_timestamp_records_when_window_end_is_slightly_ahead() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    clock.monotonic_value = 501.25
    config = FirstCrackConfig(mode="audio")
    adapter = build_first_crack_detector_adapter(
        config,
        _resolved_detector_artifacts(),
        MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),)),
    )

    result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(
            started_at_monotonic_seconds=501.0,
            duration_seconds=1.0,
        ),
    )

    assert result is not None
    assert result.event.kind == "first_crack_detected"
    assert result.event.monotonic_seconds == 1.25
    assert result.event.payload["detected_at_monotonic_seconds"] == 502.0
    assert session.first_crack_monotonic_seconds == 1.25


def test_manual_first_crack_event_takes_precedence_over_later_detector_confirmation() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    manual_event = store.record_event(session, "first_crack_detected")
    config = FirstCrackConfig(mode="audio")
    adapter = build_first_crack_detector_adapter(
        config,
        _resolved_detector_artifacts(),
        MockDetectorBackend(
            (
                FirstCrackDetectorOutput(
                    confirmed=True,
                    confidence=0.7,
                    detected_at_monotonic_seconds=502.0,
                ),
            )
        ),
    )

    result = integrate_first_crack_window_with_session(
        config=config,
        adapter=adapter,
        session_store=store,
        session=session,
        window=_audio_window(),
    )

    assert result is not None
    assert result.event == manual_event
    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
    ]
    assert session.event_timeline[-1].payload == {}


def _audio_window(
    *,
    sequence_number: int = 3,
    started_at_monotonic_seconds: float = 500.5,
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


def _resolved_detector_artifacts() -> ResolvedDetectorArtifacts:
    return ResolvedDetectorArtifacts(
        onnx_model=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision=None,
            filename="onnx/int8/model_quantized.onnx",
            local_path=Path("/tmp/model.onnx"),
        ),
        feature_extractor_config=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision=None,
            filename="onnx/int8/preprocessor_config.json",
            local_path=Path("/tmp/preprocessor_config.json"),
        ),
    )
