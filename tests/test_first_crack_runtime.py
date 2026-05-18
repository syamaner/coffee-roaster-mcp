from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from coffee_roaster_mcp.artifacts import (
    ArtifactResolutionError,
    ResolvedArtifact,
    ResolvedDetectorArtifacts,
)
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorOutput,
    build_first_crack_detector_adapter,
)
from coffee_roaster_mcp.first_crack_runtime import FirstCrackSessionRuntime
from coffee_roaster_mcp.session import RoastSessionStore


class ClockHarness:
    def __init__(self) -> None:
        self.utc_value = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        self.monotonic_value = 500.0

    def utc_now(self) -> datetime:
        return self.utc_value

    def monotonic_now(self) -> float:
        return self.monotonic_value


class MockDetectorBackend:
    def __init__(self, outputs: Sequence[FirstCrackDetectorOutput]) -> None:
        self._outputs = list(outputs)
        self.windows: list[AudioWindow] = []

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        self.windows.append(window)
        return self._outputs.pop(0)


class FakeAudioPipeline:
    def __init__(
        self,
        windows: Sequence[AudioWindow] = (),
        *,
        latest_error: str | None = None,
    ) -> None:
        self._windows = list(windows)
        self.latest_error = latest_error
        self.started = False
        self.stopped = False
        self.stop_reasons: list[float] = []

    def start(self) -> AudioCaptureSnapshot:
        self.started = True
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        self.stopped = True
        self.stop_reasons.append(timeout_seconds)
        return self.snapshot()

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        if max_windows is None:
            drained = tuple(self._windows)
            self._windows.clear()
            return drained
        drained = tuple(self._windows[:max_windows])
        del self._windows[:max_windows]
        return drained

    def add_window(self, window: AudioWindow) -> None:
        self._windows.append(window)

    def snapshot(self) -> AudioCaptureSnapshot:
        return AudioCaptureSnapshot(
            running=self.started and not self.stopped,
            queued_window_count=len(self._windows),
            emitted_window_count=len(self._windows),
            dropped_window_count=0,
            latest_error=self.latest_error,
        )


def test_disabled_and_manual_modes_do_not_prepare_audio_or_detector() -> None:
    calls: list[str] = []
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    def audio_factory(config: AudioConfig) -> FakeAudioPipeline:
        calls.append(f"audio:{config.sample_rate}")
        return FakeAudioPipeline()

    def adapter_factory(config: FirstCrackConfig) -> FirstCrackDetectorAdapter:
        calls.append(f"adapter:{config.mode}")
        raise AssertionError("adapter factory should not be called")

    disabled_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="disabled")),
        audio_pipeline_factory=audio_factory,
        detector_adapter_factory=adapter_factory,
    )
    disabled = disabled_runtime.start_for_session(session)

    manual_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="manual")),
        audio_pipeline_factory=audio_factory,
        detector_adapter_factory=adapter_factory,
    )
    manual = manual_runtime.start_for_session(session)

    assert disabled.status == "disabled"
    assert manual.status == "manual"
    assert calls == []


def test_audio_runtime_processes_after_beans_added_and_records_once() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.94,
                detected_at_monotonic_seconds=506.0,
            ),
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.99,
                detected_at_monotonic_seconds=507.0,
            ),
        )
    )
    pipeline = FakeAudioPipeline(
        (
            _audio_window(sequence_number=1, started_at_monotonic_seconds=505.0),
            _audio_window(sequence_number=2, started_at_monotonic_seconds=506.0),
        )
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    start = runtime.start_for_session(session)
    pre_t0 = runtime.process_available_windows(session_store=store, session=session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 510.0
    detected = runtime.process_available_windows(session_store=store, session=session)
    duplicate = runtime.process_available_windows(session_store=store, session=session)

    assert start.status == "pending"
    assert pre_t0.queued_window_count == 2
    assert detected.status == "detected"
    assert duplicate.status == "detected"
    assert pipeline.stopped is True
    assert [window.sequence_number for window in backend.windows] == [1]
    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
    ]
    assert session.first_crack_monotonic_seconds == 6.0


def test_audio_runtime_can_discard_queued_pre_t0_windows() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.94,
                detected_at_monotonic_seconds=506.0,
            ),
        )
    )
    pipeline = FakeAudioPipeline(
        (
            _audio_window(sequence_number=1, started_at_monotonic_seconds=501.0),
            _audio_window(sequence_number=2, started_at_monotonic_seconds=502.0),
        )
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    discarded = runtime.discard_queued_windows_for_session(
        session.id,
        reason="automatic T0 boundary",
    )
    pipeline.add_window(_audio_window(sequence_number=3, started_at_monotonic_seconds=506.0))
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 510.0
    detected = runtime.process_available_windows(session_store=store, session=session)

    assert discarded.queued_window_count == 0
    assert discarded.reason == "automatic T0 boundary"
    assert detected.status == "detected"
    assert [window.sequence_number for window in backend.windows] == [3]
    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
    ]


def test_audio_runtime_keeps_pending_status_when_detector_does_not_confirm() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=False),))
    pipeline = FakeAudioPipeline((_audio_window(sequence_number=3),))
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    snapshot = runtime.process_available_windows(session_store=store, session=session)

    assert snapshot.status == "pending"
    assert snapshot.processed_window_count == 1
    assert pipeline.stopped is False
    assert [event.kind for event in session.event_timeline] == ["beans_added"]


def test_audio_runtime_reports_unavailable_artifact_errors_without_crashing() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    def adapter_factory(config: FirstCrackConfig) -> FirstCrackDetectorAdapter:
        raise ArtifactResolutionError("missing onnx/int8/model_quantized.onnx")

    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: FakeAudioPipeline(),
        detector_adapter_factory=adapter_factory,
    )

    snapshot = runtime.start_for_session(session)

    assert snapshot.status == "unavailable"
    assert "missing onnx/int8/model_quantized.onnx" in (snapshot.reason or "")
    assert snapshot.active is False


def test_audio_runtime_reports_capture_and_detector_faults() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    capture_session = store.start_session()
    store.record_event(capture_session, "beans_added")
    capture_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: FakeAudioPipeline(latest_error="device lost"),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
    )

    capture_runtime.start_for_session(capture_session)
    capture_fault = capture_runtime.process_available_windows(
        session_store=store,
        session=capture_session,
    )

    second_store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    second_session = second_store.start_session()
    second_store.record_event(second_session, "beans_added")
    detector_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: FakeAudioPipeline((_audio_window(),)),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
    )

    detector_runtime.start_for_session(second_session)
    detector_fault = detector_runtime.process_available_windows(
        session_store=second_store,
        session=second_session,
    )

    assert capture_fault.status == "faulted"
    assert capture_fault.reason == "Audio capture failed: device lost"
    assert detector_fault.status == "faulted"
    assert "First-crack detection failed" in (detector_fault.reason or "")


def test_audio_runtime_stops_on_terminal_session_transition() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    pipeline = FakeAudioPipeline((_audio_window(),))
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend((FirstCrackDetectorOutput(confirmed=False),)),
        ),
    )

    runtime.start_for_session(session)
    snapshot = runtime.stop_for_session(session.id, reason="beans dropped")

    assert pipeline.stopped is True
    assert snapshot.status == "pending"
    assert snapshot.active is False
    assert (
        snapshot.reason == "Audio first-crack detection stopped before confirmation: beans dropped."
    )


def _audio_window(
    *,
    sequence_number: int = 0,
    started_at_monotonic_seconds: float = 505.0,
) -> AudioWindow:
    return AudioWindow(
        sequence_number=sequence_number,
        input_device="fake-mic",
        sample_rate=16_000,
        started_at_monotonic_seconds=started_at_monotonic_seconds,
        duration_seconds=1.0,
        samples=(0.0,) * 16_000,
    )


def _resolved_detector_artifacts() -> ResolvedDetectorArtifacts:
    return ResolvedDetectorArtifacts(
        onnx_model=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="v0.1.0",
            filename="onnx/int8/model_quantized.onnx",
            local_path=Path("/tmp/model_quantized.onnx"),
        ),
        feature_extractor_config=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="v0.1.0",
            filename="onnx/int8/preprocessor_config.json",
            local_path=Path("/tmp/preprocessor_config.json"),
        ),
    )
