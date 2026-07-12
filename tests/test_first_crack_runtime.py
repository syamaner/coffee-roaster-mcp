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
        peak_dbfs: float | None = None,
        rms_dbfs: float | None = None,
        overflow_count_last_minute: int = 0,
        estimated_lost_audio_ms_last_minute: float = 0.0,
        total_overflow_count: int = 0,
    ) -> None:
        self._windows = list(windows)
        self.latest_error = latest_error
        self.running_after_stop = running_after_stop
        self.peak_dbfs = peak_dbfs
        self.rms_dbfs = rms_dbfs
        self.overflow_count_last_minute = overflow_count_last_minute
        self.estimated_lost_audio_ms_last_minute = estimated_lost_audio_ms_last_minute
        self.total_overflow_count = total_overflow_count
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
            peak_dbfs=self.peak_dbfs,
            rms_dbfs=self.rms_dbfs,
            overflow_count_last_minute=self.overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=self.estimated_lost_audio_ms_last_minute,
            total_overflow_count=self.total_overflow_count,
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
    # #181: detection stops INFERENCE only — the capture pipeline keeps running
    # (and the recorder keeps teeing) so the recording spans charge→session stop,
    # not charge→FC. The pipeline is finalised only at stop_for_session.
    assert pipeline.stopped is False
    assert detected.active is True
    assert detected.audio_running is True
    assert [window.sequence_number for window in backend.windows] == [1]
    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
    ]
    # FC is backdated to the confirming-window onset (seq 1 starts at 505.0,
    # elapsed 5.0), not the detector timestamp 506.0 (#168).
    assert session.first_crack_monotonic_seconds == 5.0

    # The real roast end finalises capture + the recorder (#181).
    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    assert pipeline.stopped is True
    assert stopped.active is False
    assert stopped.audio_running is False


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

    # #181: at first crack the capture pipeline keeps running, so the runtime
    # still reports active + audio_running. The recording spans charge→stop.
    assert detected.status == "detected"
    assert detected.active is True
    assert detected.audio_running is True
    assert snapshot.active is True
    assert snapshot.audio_running is True

    # stop_for_session finalises capture. Even though this pipeline's snapshot
    # keeps claiming running after stop() (running_after_stop=True), the runtime
    # forces the stopped snapshot once it has torn the pipeline down.
    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    assert pipeline.stopped is True
    assert stopped.active is False
    assert stopped.audio_running is False


def test_runtime_mic_levels_are_live_only_and_none_after_stop() -> None:
    """mic_*_dbfs report the LIVE signal, and are None once capture stops (#178).

    While audio capture runs, the runtime surfaces the meter's peak/RMS. After
    stop_for_session — even though the runtime keeps the last capture snapshot —
    the levels go None so a caller never reads a stale post-stop value (None
    means "no live signal", per the field's documented semantics).
    """
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline(peak_dbfs=-6.02, rms_dbfs=-9.03)
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
    )

    runtime.start_for_session(session)
    live = runtime.snapshot()
    assert live.audio_running is True
    assert live.mic_peak_dbfs == -6.02
    assert live.mic_rms_dbfs == -9.03

    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    assert stopped.audio_running is False
    # The last capture snapshot is retained, but the stale levels are not surfaced.
    assert stopped.mic_peak_dbfs is None
    assert stopped.mic_rms_dbfs is None


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
    # No metadata → fallback origin=session.id, roast_num=0; mic1 WAV name.
    assert recorder.wav_path == tmp_path / "captures" / session.id / f"mic1-{session.id}-roast0.wav"
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
    assert (
        recorder.wav_path
        == tmp_path / "logs" / "captures" / session.id / f"mic1-{session.id}-roast0.wav"
    )


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
    from coffee_roaster_mcp.first_crack_runtime import RecordingMetadata, build_session_recorder

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

    recorder = build_session_recorder(
        config, session, metadata=RecordingMetadata(origin="brazil", roast_num=7)
    )

    assert isinstance(recorder, MultiDeviceRoastRecorder)
    # mic1 = the teed detector device; mic2 = the first additional device. WAVs
    # are named for the annotation pipeline: mic{N}-{origin}-roast{N}.wav.
    assert recorder.wav_path == tmp_path / session.id / "mic1-brazil-roast7.wav"
    assert recorder.additional_wav_paths == (tmp_path / session.id / "mic2-brazil-roast7.wav",)


def test_build_session_recorder_single_device_labels_wav(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import RecordingMetadata, build_session_recorder

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

    recorder = build_session_recorder(
        config, session, metadata=RecordingMetadata(origin="ethiopia", roast_num=3)
    )

    # A single configured device uses the single-stream recorder, mic1-named.
    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path == tmp_path / session.id / "mic1-ethiopia-roast3.wav"


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

    # No devices + no metadata → single stream, fallback origin=session.id/roast0.
    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path == tmp_path / session.id / f"mic1-{session.id}-roast0.wav"


def test_runtime_set_recording_metadata_flows_into_built_recorder(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
    )
    runtime = FirstCrackSessionRuntime(config=config)

    # The tool stores the metadata and echoes it back.
    stored = runtime.set_recording_metadata(origin="Brazil Cerrado", roast_num=12)
    assert stored.origin == "Brazil Cerrado"
    assert stored.roast_num == 12

    # The recorder consumes that exact metadata object to name the WAVs.
    recorder = build_session_recorder(config, session, metadata=stored)
    assert isinstance(recorder, RoastAudioRecorder)
    # Origin is slugified to [a-z0-9-]+ for the FC pipeline.
    assert recorder.wav_path.name == "mic1-brazil-cerrado-roast12.wav"


def test_set_recording_metadata_validates_input() -> None:
    runtime = FirstCrackSessionRuntime(config=AppConfig())
    with pytest.raises(ValueError, match="origin must not be blank"):
        runtime.set_recording_metadata(origin="  ", roast_num=1)
    with pytest.raises(ValueError, match="roast_num must be >= 0"):
        runtime.set_recording_metadata(origin="brazil", roast_num=-1)


def test_built_recorder_no_metadata_fallback_writes_valid_files(tmp_path: Path) -> None:
    """Finding: the no-metadata fallback (session_id / 0) still produces valid files."""
    import json

    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    recorder = build_session_recorder(
        AppConfig(
            recording=RecordingConfig(
                enabled=True, autocapture=True, export_location=tmp_path, sample_rate=8
            ),
        ),
        session,
        metadata=None,
    )
    assert recorder is not None

    recorder.begin()
    recorder.write_samples((0.2,) * 8)
    recorder.close()

    session_dir = tmp_path / session.id
    annotation_path = session_dir / f"{session.id}-roast0-session.json"
    assert annotation_path.exists()
    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert payload["origin"] == session.id  # fallback origin
    assert payload["roast_num"] == 0  # fallback roast number
    assert payload["mics"][0]["file"] == f"mic1-{session.id}-roast0.wav"
    # The WAV and recording sidecar are valid too.
    assert (session_dir / f"mic1-{session.id}-roast0.wav").exists()
    assert (session_dir / "roast.recording.json").exists()


def test_normalize_origin_slug_edge_cases() -> None:
    from coffee_roaster_mcp.first_crack_runtime import (
        _normalize_origin_slug,  # pyright: ignore[reportPrivateUsage]
    )

    assert _normalize_origin_slug("Brazil Cerrado") == "brazil-cerrado"
    assert _normalize_origin_slug("ETHIOPIA__yirgacheffe") == "ethiopia-yirgacheffe"
    assert _normalize_origin_slug("  !!  ") == "roast"  # empty after stripping → fallback
    assert _normalize_origin_slug("kenya-aa") == "kenya-aa"


def test_teed_stream_wav_uses_detector_sample_rate_not_recording_rate(tmp_path: Path) -> None:
    """Bug 1: the teed mic1 WAV header must use audio.sample_rate (the detector's
    real capture rate), NOT recording.sample_rate."""
    import wave

    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import AudioConfig, RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        audio=AudioConfig(sample_rate=16_000),
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            sample_rate=44_100,  # the WRONG rate for the teed detector stream
        ),
    )

    recorder = build_session_recorder(config, session)
    assert isinstance(recorder, RoastAudioRecorder)
    # The recorder's nominal rate is the detector rate, not recording.sample_rate.
    assert recorder.sample_rate == 16_000

    recorder.begin()
    recorder.write_samples((0.1,) * 16_000)  # 1.0 s of 16 kHz audio
    recorder.close()

    with wave.open(str(recorder.wav_path), "rb") as wav_file:
        assert wav_file.getframerate() == 16_000  # NOT 44_100
        frame_count = wav_file.getnframes()
    assert frame_count == 16_000
    # frame_count / rate gives the TRUE wall-clock duration (1.0 s), not 0.36 s.
    assert round(frame_count / wav_file.getframerate(), 3) == 1.0


def test_multi_device_teed_and_additional_rates_differ(tmp_path: Path) -> None:
    """Bug 1: mic1 (teed) uses audio.sample_rate; additional streams use
    recording.sample_rate."""
    import time
    import wave

    from coffee_roaster_mcp.audio import (
        AdditionalRecordingDevice,
        AudioInput,
        MultiDeviceRoastRecorder,
    )
    from coffee_roaster_mcp.config import AudioConfig, RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import RecordingMetadata, build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    config = AppConfig(
        audio=AudioConfig(sample_rate=16_000),
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            sample_rate=44_100,
            devices=("USB PnP", "ATR2100x"),
        ),
    )

    captured_rates: dict[str, int] = {}

    def factory(device: AdditionalRecordingDevice) -> AudioInput:
        # Record the rate the additional stream was configured with, then no-op
        # the capture (no hardware in tests).
        captured_rates[device.device_label] = device.sample_rate
        raise RuntimeError("not opening hardware in this test")

    recorder = build_session_recorder(
        config,
        session,
        metadata=RecordingMetadata(origin="brazil", roast_num=7),
    )
    assert isinstance(recorder, MultiDeviceRoastRecorder)

    # We cannot reach the private additional specs through build_session_recorder,
    # so re-create an equivalent recorder with a capturing factory to observe the
    # rate threaded into the additional device.
    observed = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "x" / "mic1-brazil-roast7.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "x" / "roast.recording.json",
        sample_rate=16_000,  # detector rate
        session_id="s",
        additional_devices=[
            AdditionalRecordingDevice(
                "ATR2100x", tmp_path / "x" / "mic2-brazil-roast7.wav", 44_100
            ),
        ],
        additional_input_factory=factory,
        stop_timeout_seconds=2.0,
    )
    observed.begin()
    time.sleep(0.05)
    observed.close()

    # The teed mic1 WAV header is the detector rate; the additional stream is the
    # recording rate.
    with wave.open(str(observed.wav_path), "rb") as wav_file:
        assert wav_file.getframerate() == 16_000
    assert captured_rates["ATR2100x"] == 44_100


def test_zero_frame_teed_stream_still_finalises_wav_and_both_sidecars(tmp_path: Path) -> None:
    """Bug 2: a teed stream that captures NO frames before close still produces a
    valid finalised mic1 WAV plus both JSON sidecars."""
    import json
    import wave

    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    recorder = build_session_recorder(
        AppConfig(
            recording=RecordingConfig(
                enabled=True, autocapture=True, export_location=tmp_path, sample_rate=16_000
            ),
        ),
        session,
        metadata=None,
    )
    assert recorder is not None

    # begin() then close() with NO write_samples in between (zero frames).
    recorder.begin()
    recorder.close()

    session_dir = tmp_path / session.id
    wav_path = session_dir / f"mic1-{session.id}-roast0.wav"
    # Valid, finalised WAV (not 0 bytes) with a correct header and 0 frames.
    assert wav_path.exists()
    assert wav_path.stat().st_size >= 44  # WAV header is 44 bytes
    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.getnframes() == 0
        assert wav_file.getframerate() == 16_000
    # BOTH sidecars written.
    annotation_path = session_dir / f"{session.id}-roast0-session.json"
    recording_sidecar = session_dir / "roast.recording.json"
    assert annotation_path.exists()
    assert recording_sidecar.exists()
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert annotation["mics"][0]["file"] == f"mic1-{session.id}-roast0.wav"


def test_set_recording_metadata_after_build_warns_and_is_not_applied(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hardening: metadata set AFTER the recorder is built logs a warning and the
    WAV names stay fixed (it is not silently applied)."""
    import logging

    from coffee_roaster_mcp.config import RecordingConfig

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
            recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
        ),
        audio_pipeline_factory=lambda _config: FakeAudioPipeline(),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )

    # Metadata BEFORE start: applied with no warning.
    with caplog.at_level(logging.WARNING, logger="coffee_roaster_mcp.first_crack_runtime"):
        runtime.set_recording_metadata(origin="early", roast_num=1)
    assert not any("arrived AFTER" in record.message for record in caplog.records)

    # Starting the roast builds the recorder for the active session.
    runtime.start_for_session(session)

    # Metadata AFTER the recorder was built now warns (non-silent).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="coffee_roaster_mcp.first_crack_runtime"):
        runtime.set_recording_metadata(origin="late", roast_num=99)
    assert any("arrived AFTER" in record.message for record in caplog.records)

    # A new session resets the gate: metadata is accepted again without warning.
    runtime.stop_for_session(session.id, reason="done")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="coffee_roaster_mcp.first_crack_runtime"):
        runtime.set_recording_metadata(origin="next", roast_num=2)
    assert not any("arrived AFTER" in record.message for record in caplog.records)


# --- #181: recording must span charge→session stop, not charge→first crack ---


class _StreamingToneInput:
    """An ``AudioInput`` that delivers a continuous tone in fixed-size reads.

    Models a live microphone for the soak test: it never runs dry (always
    returns the requested sample count), so the capture worker keeps reading,
    teeing, and updating levels for the full session — before AND after first
    crack — exactly as a real mic would.
    """

    def __init__(self, *, amplitude: float = 0.5) -> None:
        self._amplitude = amplitude
        self._phase = 0
        self.total_read = 0
        self.closed = False

    def read_samples(self, sample_count: int) -> Sequence[float]:
        if sample_count <= 0:
            return ()
        out: list[float] = []
        for _ in range(sample_count):
            # Alternate sign so peak and RMS are non-trivial and stable.
            out.append(self._amplitude if self._phase % 2 == 0 else -self._amplitude)
            self._phase += 1
        self.total_read += sample_count
        return tuple(out)

    def close(self) -> None:
        self.closed = True


def _confirmed_outputs(count: int) -> tuple[FirstCrackDetectorOutput, ...]:
    return tuple(FirstCrackDetectorOutput(confirmed=True, confidence=0.95) for _ in range(count))


def test_recording_spans_charge_to_session_stop_not_to_first_crack(tmp_path: Path) -> None:
    """#181 soak: a sustained MCP-driven session records charge→stop, FC inside.

    Drives the runtime API with a REAL capture pipeline teeing a REAL recorder:
    charge, run a while, fire first crack via the detector, run MORE, then stop
    the session. Asserts the WAV spans charge→stop (its duration exceeds the
    first-crack offset by a clear margin — the post-FC tail is captured, not
    truncated at FC), that first crack sits INSIDE the recording, and that
    finalisation wrote both the recording sidecar and the annotation session
    JSON. Run sustained: many capture cycles before and after FC.
    """
    import time

    from coffee_roaster_mcp.audio import RoastAudioRecorder, build_audio_capture_pipeline
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    # Drive this soak on the REAL monotonic clock so the capture pipeline, the
    # recorder, and the first-crack integrator all agree on time (the integrator
    # rejects window timestamps that are future relative to its own clock). This
    # is a genuine sustained run, not a stepped fixture.
    store = RoastSessionStore()
    session = store.start_session()

    sample_rate = 4_000
    config = AppConfig(
        audio=AudioConfig(sample_rate=sample_rate, source="microphone"),
        first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path / "captures",
        ),
    )

    audio_input = _StreamingToneInput(amplitude=0.5)
    # The detector confirms on the first window it sees once roasting; many
    # confirmations queued so the post-FC ticks (which never reach the detector
    # again) cannot exhaust it.
    backend = MockDetectorBackend(_confirmed_outputs(50))

    captured: dict[str, RoastAudioRecorder] = {}

    def pipeline_factory(audio_config: AudioConfig) -> object:
        # Tee the session recorder the runtime built into a REAL capture
        # pipeline driven by the streaming tone input (the recorder is exposed
        # to the runtime via build_session_recorder + the default factory; here
        # we build the pipeline explicitly so the test owns the input).
        recorder = build_session_recorder(config, session)
        assert isinstance(recorder, RoastAudioRecorder)
        captured["recorder"] = recorder
        return build_audio_capture_pipeline(
            audio_config,
            input_factory=lambda settings: audio_input,  # noqa: ARG005 - fixed test input
            window_seconds=0.05,
            recorder=recorder,
        )

    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=pipeline_factory,  # type: ignore[arg-type]
        detector_adapter_factory=lambda fc_config: build_first_crack_detector_adapter(
            fc_config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )

    runtime.start_for_session(session)
    recorder = captured["recorder"]

    # --- Phase 1: pre-charge / pre-FC — capture runs, no detection. ---
    deadline = time.monotonic() + 2.0
    while recorder.frames_written < sample_rate // 4 and time.monotonic() < deadline:
        runtime.process_available_windows(session_store=store, session=session)
        time.sleep(0.01)
    frames_before_charge = recorder.frames_written
    assert frames_before_charge > 0, "capture worker should be teeing before charge"

    # --- Phase 2: charge, then let the detector confirm first crack. ---
    store.record_event(session, "beans_added")
    # Drop the pre-charge windows so first crack backdates to a post-charge
    # window onset (a window captured before charge would be rejected as
    # pre-beans by the integrator). Mirrors the auto-T0 boundary discard.
    runtime.discard_queued_windows_for_session(session.id, reason="charge boundary")
    # Let fresh post-charge windows accumulate before detecting.
    time.sleep(0.2)
    detected = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        snap = runtime.process_available_windows(session_store=store, session=session)
        if snap.status == "detected":
            detected = snap
            break
        time.sleep(0.01)
    assert detected is not None, "first crack should be detected during roasting"
    assert detected.status == "detected"
    # Capture is STILL running after FC — the #181 invariant.
    assert detected.active is True
    assert detected.audio_running is True
    frames_at_fc = recorder.frames_written

    # --- Phase 3: run MORE after FC — capture must keep growing the WAV. ---
    deadline = time.monotonic() + 2.0
    target = frames_at_fc + sample_rate // 2  # at least ~0.5s more audio
    while recorder.frames_written < target and time.monotonic() < deadline:
        # Post-FC ticks must NOT drain/detect again (inference stopped, #181).
        runtime.process_available_windows(session_store=store, session=session)
        time.sleep(0.01)
    frames_after_fc = recorder.frames_written
    assert frames_after_fc > frames_at_fc, "the WAV must keep growing after first crack"
    # The detector saw windows only up to the confirming one — post-FC windows
    # are dropped from the bounded queue, never re-detected.
    detector_windows_after = len(backend.windows)

    # --- Stop the session: finalises capture + recorder at the REAL roast end. ---
    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    assert stopped.active is False
    assert stopped.audio_running is False
    # No further detection happened during the post-FC tail.
    runtime.process_available_windows(session_store=store, session=session)
    assert len(backend.windows) == detector_windows_after

    # --- Assert the on-disk recording spans charge→stop, FC inside it. ---
    import json
    import wave

    wav_path = recorder.wav_path
    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wav_file:
        wav_frames = wav_file.getnframes()
        wav_rate = wav_file.getframerate()
    recorded_seconds = wav_frames / wav_rate
    # The WAV holds the full pre-FC + post-FC audio, not a charge→FC slice.
    assert wav_frames >= frames_after_fc
    assert wav_frames > frames_at_fc

    sidecar = json.loads(recorder.sidecar_path.read_text())
    fc_offset = sidecar["milestones"]["first_crack"]
    assert fc_offset is not None, "first crack milestone must be written"
    # First crack sits INSIDE the recording, with a real post-FC tail after it.
    assert 0.0 <= fc_offset < recorded_seconds
    assert recorded_seconds - fc_offset > 0.1, "a post-FC tail must follow first crack in the WAV"

    # Finalisation wrote BOTH JSONs: the recording sidecar and the annotation
    # session JSON for the coffee-first-crack-detection pipeline.
    annotation_path = recorder.sidecar_path.parent / f"{session.id}-roast0-session.json"
    assert annotation_path.exists(), "annotation session JSON must be written at finalisation"
    annotation = json.loads(annotation_path.read_text())
    assert annotation["mics"][0]["file"] == wav_path.name
