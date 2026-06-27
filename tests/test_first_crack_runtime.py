from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
        running_after_stop: bool = False,
    ) -> None:
        self._windows = list(windows)
        self.latest_error = latest_error
        self.running_after_stop = running_after_stop
        self.started = False
        self.stopped = False
        self.stop_reasons: list[float] = []
        self.drain_limits: list[int | None] = []

    def start(self) -> AudioCaptureSnapshot:
        self.started = True
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        self.stopped = True
        self.stop_reasons.append(timeout_seconds)
        return self.snapshot()

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        self.drain_limits.append(max_windows)
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
            running=self.started and (not self.stopped or self.running_after_stop),
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
    # FC is backdated to the confirming-window onset (seq 1 starts at 505.0,
    # elapsed 5.0), not the detector timestamp 506.0 (#168).
    assert session.first_crack_monotonic_seconds == 5.0


def test_detector_paced_audio_runtime_drains_one_window_per_processing_tick() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=False),
            FirstCrackDetectorOutput(
                confirmed=True,
                confidence=0.94,
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
        config=AppConfig(
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
            audio=AudioConfig(
                source="wav",
                wav_path=Path("replay.wav"),
                replay_mode="detector_paced",
            ),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 510.0
    pending = runtime.process_available_windows(session_store=store, session=session)
    detected = runtime.process_available_windows(session_store=store, session=session)

    assert pending.status == "pending"
    assert detected.status == "detected"
    assert pipeline.drain_limits == [1, 1]
    assert [window.sequence_number for window in backend.windows] == [1, 2]
    # Inferred timestamp backdates to the confirming-window onset (seq 2 starts
    # at 506.0, elapsed 6.0), not its window end 507.0 (#168).
    assert session.first_crack_monotonic_seconds == 6.0


def test_audio_runtime_reports_stopped_after_pipeline_stop_returns_running_snapshot() -> None:
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
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=506.0),),
        running_after_stop=True,
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
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 510.0
    detected = runtime.process_available_windows(session_store=store, session=session)
    snapshot = runtime.snapshot()

    assert detected.active is False
    assert detected.audio_running is False
    assert snapshot.active is False
    assert snapshot.audio_running is False


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


def test_audio_runtime_records_earliest_positive_after_confirmation() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    clock.monotonic_value = 500.0
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=True, confidence=0.61),
            FirstCrackDetectorOutput(confirmed=False, confidence=0.2),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.82),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.91),
        )
    )
    pipeline = FakeAudioPipeline(
        (
            _audio_window(sequence_number=1, started_at_monotonic_seconds=503.0),
            _audio_window(sequence_number=2, started_at_monotonic_seconds=506.0),
            _audio_window(sequence_number=3, started_at_monotonic_seconds=509.0),
            _audio_window(sequence_number=4, started_at_monotonic_seconds=512.0),
        )
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(
                mode="audio",
                confidence_threshold=0.6,
                min_positive_windows=3,
                confirmation_window_seconds=20.0,
            )
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    clock.monotonic_value = 515.0
    detected = runtime.process_available_windows(session_store=store, session=session)

    assert detected.status == "detected"
    # Backdated to the onset of the earliest positive window (seq 1 starts at
    # 503.0, elapsed 3.0), not its window end 504.0 (#168).
    assert session.first_crack_monotonic_seconds == 3.0
    assert session.event_timeline[-1].payload["window_sequence_number"] == 1
    assert session.event_timeline[-1].payload["confirmed_by_window_sequence_number"] == 4
    assert session.event_timeline[-1].payload["positive_window_count"] == 3


def test_detector_paced_wav_replay_preserves_source_audio_timeline() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    backend = MockDetectorBackend((FirstCrackDetectorOutput(confirmed=True),))
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=4, started_at_monotonic_seconds=504.0),)
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(mode="audio"),
            audio=AudioConfig(source="wav", replay_mode="detector_paced"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    detected = runtime.process_available_windows(session_store=store, session=session)

    assert detected.status == "detected"
    # Backdated to the confirming-window onset (504.0, elapsed 4.0), not the
    # inferred window end 505.0 (#168).
    assert session.first_crack_monotonic_seconds == 4.0


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


def test_build_session_recorder_disabled_returns_none() -> None:
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    # Disabled entirely.
    assert (
        build_session_recorder(AppConfig(recording=RecordingConfig(enabled=False)), session) is None
    )
    # Enabled but not autocapture (v1 only wires autocapture).
    assert (
        build_session_recorder(
            AppConfig(recording=RecordingConfig(enabled=True, autocapture=False)),
            session,
        )
        is None
    )


def test_build_session_recorder_uses_export_location_and_session_id(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        audio=AudioConfig(sample_rate=22_050),
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path / "captures",
        ),
    )

    recorder = build_session_recorder(config, session)

    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path == tmp_path / "captures" / session.id / "roast.wav"
    assert recorder.sidecar_path == tmp_path / "captures" / session.id / "roast.recording.json"
    # sample_rate falls back to the detector audio.sample_rate.
    assert recorder.sample_rate == 22_050


def test_build_session_recorder_defaults_export_under_log_dir(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import LoggingConfig, RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        logging=LoggingConfig(log_dir=tmp_path / "logs"),
        recording=RecordingConfig(enabled=True, autocapture=True),
    )

    recorder = build_session_recorder(config, session)

    assert isinstance(recorder, RoastAudioRecorder)
    # With no export_location, captures land under the gitignored log dir.
    assert recorder.wav_path == tmp_path / "logs" / "captures" / session.id / "roast.wav"


def test_default_pipeline_factory_passes_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffee_roaster_mcp.first_crack_runtime as runtime_module
    from coffee_roaster_mcp.config import RecordingConfig

    captured: dict[str, object] = {}

    def fake_build_pipeline(config: AudioConfig, *, recorder: object = None) -> FakeAudioPipeline:
        captured["recorder"] = recorder
        return FakeAudioPipeline()

    monkeypatch.setattr(runtime_module, "build_audio_capture_pipeline", fake_build_pipeline)

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
            recording=RecordingConfig(enabled=True, autocapture=True),
        ),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
    )

    snapshot = runtime.start_for_session(session)

    # The default factory built the pipeline with the session recorder teed in.
    assert snapshot.status == "pending"
    assert captured["recorder"] is not None


def test_build_session_recorder_milestones_track_session(tmp_path: Path) -> None:
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import (
        build_session_recorder,
        recording_relative_milestones,
    )

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            sample_rate=16_000,
        ),
    )

    recorder = build_session_recorder(config, session)
    assert recorder is not None

    # Before any milestone, both are None (recording start unknown → raw values).
    assert recording_relative_milestones(session, recording_started_monotonic_seconds=None) == {
        "beans_added": None,
        "first_crack": None,
    }

    # The provider reads the LIVE session, so later milestones surface.
    session.beans_added_monotonic_seconds = 12.0
    session.first_crack_monotonic_seconds = 95.5
    assert recording_relative_milestones(session, recording_started_monotonic_seconds=None) == {
        "beans_added": 12.0,
        "first_crack": 95.5,
    }


def test_recording_relative_milestones_are_rebased_to_recording_start() -> None:
    """A milestone's sidecar value equals (milestone_session_elapsed - rec_start)."""
    from coffee_roaster_mcp.first_crack_runtime import recording_relative_milestones

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    # Session started at monotonic 500.0 (ClockHarness). Recording began 3.5 s
    # later, at absolute monotonic 503.5 — i.e. 3.5 s into the session.
    assert session.monotonic_start == 500.0
    recording_started = 503.5
    recording_start_session_elapsed = recording_started - session.monotonic_start

    # Milestones in SESSION-elapsed seconds (as the session stores them).
    session.beans_added_monotonic_seconds = 12.0
    session.first_crack_monotonic_seconds = 95.5

    milestones = recording_relative_milestones(
        session, recording_started_monotonic_seconds=recording_started
    )

    # Each is genuinely recording-relative: session-elapsed minus the recording
    # start (in the same session-elapsed domain).
    assert milestones["beans_added"] == round(12.0 - recording_start_session_elapsed, 6)
    assert milestones["first_crack"] == round(95.5 - recording_start_session_elapsed, 6)
    assert milestones["beans_added"] == 8.5
    assert milestones["first_crack"] == 92.0


def test_built_recorder_writes_recording_relative_milestones(tmp_path: Path) -> None:
    """End to end: the sidecar from build_session_recorder carries rebased milestones."""
    import json

    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    recorder = build_session_recorder(
        AppConfig(
            recording=RecordingConfig(
                enabled=True,
                autocapture=True,
                export_location=tmp_path,
                sample_rate=8,
            ),
        ),
        session,
    )
    assert recorder is not None

    recorder.begin()
    recording_started = recorder.started_monotonic_seconds
    assert recording_started is not None
    recording_start_session_elapsed = recording_started - session.monotonic_start

    # Milestones recorded (in session-elapsed seconds) after recording started.
    session.beans_added_monotonic_seconds = 12.0
    session.first_crack_monotonic_seconds = 95.5
    recorder.write_samples((0.1,) * 8)
    recorder.close()

    sidecar = json.loads(
        (tmp_path / session.id / "roast.recording.json").read_text(encoding="utf-8")
    )
    # The closure rebased both milestones onto the recording clock.
    assert sidecar["milestones"]["beans_added"] == round(12.0 - recording_start_session_elapsed, 6)
    assert sidecar["milestones"]["first_crack"] == round(95.5 - recording_start_session_elapsed, 6)


def test_build_session_recorder_multi_device(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import MultiDeviceRoastRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            devices=("USB PnP", "ATR2100x"),
        ),
    )

    recorder = build_session_recorder(config, session)

    assert isinstance(recorder, MultiDeviceRoastRecorder)
    # The FIRST device is the teed detector device; additional devices get fresh
    # streams, one WAV each, with labels derived from the device names.
    assert recorder.wav_path == tmp_path / session.id / "roast.usb-pnp.wav"
    assert recorder.additional_wav_paths == (tmp_path / session.id / "roast.atr2100x.wav",)


def test_build_session_recorder_single_device_labels_wav(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            devices=("USB PnP",),
        ),
    )

    recorder = build_session_recorder(config, session)

    # A single configured device falls back to the v1 single-stream recorder.
    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path == tmp_path / session.id / "roast.wav"


def test_build_session_recorder_no_devices_is_v1_single(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
    )

    recorder = build_session_recorder(config, session)

    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path == tmp_path / session.id / "roast.wav"
