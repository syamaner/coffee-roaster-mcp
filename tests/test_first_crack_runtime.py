from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from coffee_roaster_mcp.artifacts import (
    ArtifactResolutionError,
    ResolvedArtifact,
    ResolvedDetectorArtifacts,
)
from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioCaptureSettings,
    AudioCaptureSnapshot,
    AudioWindow,
    RoastRecorder,
)
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig, RecordingConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorOutput,
    build_first_crack_detector_adapter,
)
from coffee_roaster_mcp.first_crack_observer import FirstCrackObserver
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackSessionRuntime,
)
from coffee_roaster_mcp.session import RoastSessionStore, SessionLifecycleError


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


class TimedDetectorBackend(MockDetectorBackend):
    """Deterministic detector backend that advances the injected runtime clock."""

    def __init__(
        self,
        outputs: Sequence[FirstCrackDetectorOutput],
        *,
        clock: ClockHarness,
        durations_seconds: Sequence[float],
        failure: str | None = None,
    ) -> None:
        super().__init__(outputs)
        self._clock = clock
        self._durations_seconds = list(durations_seconds)
        self._failure = failure

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        self._clock.monotonic_value += self._durations_seconds.pop(0)
        if self._failure is not None:
            raise RuntimeError(self._failure)
        return super().detect(window)


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
        max_consecutive_overflow_count: int = 0,
    ) -> None:
        self._windows = list(windows)
        self.latest_error = latest_error
        self.running_after_stop = running_after_stop
        self.peak_dbfs = peak_dbfs
        self.rms_dbfs = rms_dbfs
        self.overflow_count_last_minute = overflow_count_last_minute
        self.estimated_lost_audio_ms_last_minute = estimated_lost_audio_ms_last_minute
        self.total_overflow_count = total_overflow_count
        self.max_consecutive_overflow_count = max_consecutive_overflow_count
        self.started = False
        self.stopped = False
        self.stop_reasons: list[float] = []
        self.drain_limits: list[int | None] = []
        self.discard_calls = 0

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

    def discard_pending_audio(self, *, timeout_seconds: float = 1.0) -> None:
        # This fake models the whole pipeline as one queued-window list, so
        # there is no separate reader-backlog/sample-buffer stage to clear —
        # discarding is equivalent to draining everything.
        self.discard_calls += 1
        self._windows.clear()

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
            max_consecutive_overflow_count=self.max_consecutive_overflow_count,
        )

    @property
    def shutdown_confirmed(self) -> bool:
        return self.stopped and not self.running_after_stop


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


def test_inference_timing_excludes_store_work_and_tracks_explicit_hop_overruns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful adapter calls exclude delayed store work and track overruns."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    pipeline = FakeAudioPipeline(
        (
            _audio_window(sequence_number=1, started_at_monotonic_seconds=500.0),
            _audio_window(sequence_number=2, started_at_monotonic_seconds=500.25),
        )
    )
    backend = TimedDetectorBackend(
        (
            FirstCrackDetectorOutput(confirmed=False),
            FirstCrackDetectorOutput(confirmed=True, confidence=0.94),
        ),
        clock=clock,
        durations_seconds=(0.25, 0.125),
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(sample_rate=100, hop_seconds=0.25),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )

    original_record_observation = store.record_first_crack_window_observation
    original_record_detection = store.record_first_crack_detection_snapshot

    def delayed_record_observation(*args: Any, **kwargs: Any) -> None:
        clock.monotonic_value += 10.0
        original_record_observation(*args, **kwargs)

    def delayed_record_detection(*args: Any, **kwargs: Any) -> Any:
        clock.monotonic_value += 10.0
        return original_record_detection(*args, **kwargs)

    monkeypatch.setattr(store, "record_first_crack_window_observation", delayed_record_observation)
    monkeypatch.setattr(store, "record_first_crack_detection_snapshot", delayed_record_detection)

    runtime.start_for_session(session)
    snapshot = runtime.process_available_windows(session_store=store, session=session)

    assert snapshot.status == "detected"
    assert snapshot.last_inference_duration_ms == pytest.approx(125.0)
    assert snapshot.max_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.inference_overrun_count == 1

    stopped = runtime.stop_for_session(session.id, reason="first session complete")
    assert stopped.last_inference_duration_ms == pytest.approx(125.0)
    assert stopped.max_inference_duration_ms == pytest.approx(250.0)
    assert stopped.inference_overrun_count == 1
    repeated_stopped = runtime.snapshot()
    assert repeated_stopped.last_inference_duration_ms == pytest.approx(125.0)
    assert repeated_stopped.max_inference_duration_ms == pytest.approx(250.0)
    assert repeated_stopped.inference_overrun_count == 1
    store.stop_session()
    next_session = store.start_session()
    reset = runtime.start_for_session(next_session)

    assert reset.last_inference_duration_ms == 0.0
    assert reset.max_inference_duration_ms == 0.0
    assert reset.inference_overrun_count == 0


def test_inference_timing_records_no_result_and_derived_hop_boundary() -> None:
    """No-result inference uses the rounded effective hop for an overrun."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=500.0),)
    )
    backend = TimedDetectorBackend(
        (FirstCrackDetectorOutput(confirmed=False),),
        clock=clock,
        durations_seconds=(0.25,),
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            # The nominal 300 ms hop is 1.2 samples and rounds down to 1 sample / 250 ms.
            audio=AudioConfig(sample_rate=4, window_seconds=0.5, overlap=0.4),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    snapshot = runtime.process_available_windows(session_store=store, session=session)

    assert snapshot.status == "pending"
    assert snapshot.last_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.max_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.inference_overrun_count == 1


def test_normal_inference_failure_records_timing_before_preserving_fault() -> None:
    """An integration failure still records its duration before existing fault handling."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    store.record_event(session, "beans_added")
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=500.0),),
        overflow_count_last_minute=7,
        estimated_lost_audio_ms_last_minute=700.0,
        total_overflow_count=42,
        max_consecutive_overflow_count=9,
    )
    backend = TimedDetectorBackend(
        (FirstCrackDetectorOutput(confirmed=False),),
        clock=clock,
        durations_seconds=(0.25,),
        failure="normal inference failed",
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(sample_rate=100, hop_seconds=0.25),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    snapshot = runtime.process_available_windows(session_store=store, session=session)

    assert snapshot.status == "faulted"
    assert snapshot.reason == "First-crack detection failed: RuntimeError: normal inference failed"
    assert snapshot.last_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.max_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.inference_overrun_count == 1

    # Fault freezes capture and inference evidence. Repeated reads retain the
    # same runtime metrics while the existing 60-second rolling-overflow decay
    # still applies only to rolling fields.
    repeated = runtime.snapshot()
    assert repeated.last_inference_duration_ms == pytest.approx(250.0)
    assert repeated.max_inference_duration_ms == pytest.approx(250.0)
    assert repeated.inference_overrun_count == 1
    assert repeated.max_consecutive_overflow_count == 9
    clock.monotonic_value += 61.0
    decayed = runtime.snapshot()
    assert decayed.overflow_count_last_minute == 0
    assert decayed.estimated_lost_audio_ms_last_minute == 0.0
    assert decayed.total_overflow_count == 42
    assert decayed.max_consecutive_overflow_count == 9
    assert decayed.last_inference_duration_ms == pytest.approx(250.0)
    assert decayed.max_inference_duration_ms == pytest.approx(250.0)
    assert decayed.inference_overrun_count == 1


def test_post_drop_inference_failure_records_timing_before_preserving_fault() -> None:
    """Post-drop recovery inference records timing without changing its fault path."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=505.0),)
    )
    backend = TimedDetectorBackend(
        (FirstCrackDetectorOutput(confirmed=False),),
        clock=clock,
        durations_seconds=(0.25,),
        failure="post-drop inference failed",
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(sample_rate=100, window_seconds=0.5, overlap=0.5),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")
    snapshot = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert snapshot.status == "faulted"
    assert (
        snapshot.reason == "First-crack detection failed: RuntimeError: post-drop inference failed"
    )
    assert snapshot.last_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.max_inference_duration_ms == pytest.approx(250.0)
    assert snapshot.inference_overrun_count == 1


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

    # Ordinary roast teardown clears handles even when a test double reports an
    # unconfirmed worker; only cold finalisation retains retry state.
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


def test_runtime_overflow_stats_survive_stop_unlike_live_mic_levels() -> None:
    """Overflow stats (#190 review finding) are the OPPOSITE of mic levels:
    they must SURVIVE stop_for_session, not go None/zero. An operator
    reviewing a roast right after drop/stop is exactly when the roast's
    overflow history matters most — zeroing it out at the moment it's
    needed would be a real observability gap.
    """
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline(
        overflow_count_last_minute=7,
        estimated_lost_audio_ms_last_minute=700.0,
        total_overflow_count=42,
        max_consecutive_overflow_count=9,
    )
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
    assert live.overflow_count_last_minute == 7
    assert live.estimated_lost_audio_ms_last_minute == 700.0
    assert live.total_overflow_count == 42
    assert live.max_consecutive_overflow_count == 9

    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    assert stopped.audio_running is False
    # Unlike mic_peak_dbfs/mic_rms_dbfs (live-only, correctly None here),
    # overflow stats carry through unchanged.
    assert stopped.overflow_count_last_minute == 7
    assert stopped.estimated_lost_audio_ms_last_minute == 700.0
    assert stopped.total_overflow_count == 42
    assert stopped.max_consecutive_overflow_count == 9


def test_runtime_overflow_rolling_fields_decay_60s_after_stop() -> None:
    """coffee-roaster-mcp#193 review finding: overflow_count_last_minute and
    estimated_lost_audio_ms_last_minute are contractually a trailing-60-
    SECOND rolling window (AudioCaptureSnapshot's docstring). Carrying them
    through unchanged at stop (the sibling test above, task #59) must not
    leave them frozen forever — a poll 60+ seconds after stop must see them
    decayed to zero, same as a live rolling window naturally would. The
    lifetime total_overflow_count is a whole-roast figure, not a rolling
    one, so it must stay frozen regardless of how long ago capture stopped.
    """
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline(
        overflow_count_last_minute=7,
        estimated_lost_audio_ms_last_minute=700.0,
        total_overflow_count=42,
        max_consecutive_overflow_count=9,
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    runtime.stop_for_session(session.id, reason="roast complete")

    # A poll shortly after stop still sees the accurate figures.
    clock.monotonic_value += 30.0
    soon_after_stop = runtime.snapshot()
    assert soon_after_stop.overflow_count_last_minute == 7
    assert soon_after_stop.estimated_lost_audio_ms_last_minute == 700.0
    assert soon_after_stop.total_overflow_count == 42
    assert soon_after_stop.max_consecutive_overflow_count == 9

    # A poll 60+ seconds after stop must see the rolling fields decayed —
    # the lifetime total is untouched.
    clock.monotonic_value += 31.0
    long_after_stop = runtime.snapshot()
    assert long_after_stop.overflow_count_last_minute == 0
    assert long_after_stop.estimated_lost_audio_ms_last_minute == 0.0
    assert long_after_stop.total_overflow_count == 42
    assert long_after_stop.max_consecutive_overflow_count == 9


def test_runtime_overflow_rolling_fields_decay_from_last_live_poll_when_stop_itself_fails() -> None:
    """coffee-roaster-mcp#193 review finding, round 2: decay must be keyed
    off the AGE of the events behind the aggregate — the LAST LIVE poll
    that actually observed them — not off time-since-STOP as a proxy.
    Normally `stop_for_session`'s own `pipeline.stop()` call is itself a
    fresh live read, so its "as of" instant IS the stop instant. But if
    `pipeline.stop()` itself RAISES, `_last_capture_snapshot` is never
    refreshed by that call — it retains whatever the LAST SUCCESSFUL live
    poll observed, which can be arbitrarily older than "now" the moment
    stop_for_session returns. Decay must measure from that live poll's
    true age, not incorrectly reset the clock to the (failed) stop attempt.
    """
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    class _StopFailingPipeline(FakeAudioPipeline):
        def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
            raise AudioCaptureError("device unplugged mid-stop")

    pipeline = _StopFailingPipeline(
        overflow_count_last_minute=7,
        estimated_lost_audio_ms_last_minute=700.0,
        total_overflow_count=42,
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    # One live poll observes the aggregate — this is the true "as of" instant.
    live_poll = runtime.snapshot()
    assert live_poll.overflow_count_last_minute == 7

    # A long stretch passes with capture still running but no further poll —
    # the aggregate is now 90 seconds old.
    clock.monotonic_value += 90.0

    # stop_for_session's own pipeline.stop() call RAISES, so it can never
    # refresh _last_capture_snapshot with a fresh stop-instant read — the
    # 90-second-old live poll is all that remains.
    stopped = runtime.stop_for_session(session.id, reason="roast complete")
    # Ordinary-stop teardown clears runtime handles even when stop itself fails.
    assert stopped.status == "faulted"
    assert stopped.active is False
    assert stopped.overflow_count_last_minute == 0
    assert stopped.estimated_lost_audio_ms_last_minute == 0.0
    # The lifetime total survives regardless of decay.
    assert stopped.total_overflow_count == 42


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


def test_failed_audio_capture_start_remains_a_retryable_finalisation_failure() -> None:
    """An attempted but failed capture start cannot become not-applicable teardown."""
    session = RoastSessionStore().start_session(purpose="cold_characterisation")

    def fail_pipeline(_config: AudioConfig) -> FakeAudioPipeline:
        raise AudioCaptureError("input unavailable")

    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=fail_pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )

    assert runtime.start_for_session(session).status == "unavailable"
    assert runtime.finalise_for_session(session.id) == (
        "stop_failed",
        "Audio capture did not start.",
        False,
    )


def test_unexpected_capture_preparation_failure_remains_a_finalisation_failure() -> None:
    """An unexpected capture setup error cannot become not-applicable teardown."""
    session = RoastSessionStore().start_session(purpose="cold_characterisation")

    def fail_pipeline(_config: AudioConfig) -> FakeAudioPipeline:
        raise RuntimeError("backend setup failed")

    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=fail_pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )

    snapshot = runtime.start_for_session(session)
    assert snapshot.status == "unavailable"
    assert "RuntimeError: backend setup failed" in (snapshot.reason or "")
    assert runtime.finalise_for_session(session.id)[0] == "stop_failed"


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


def test_default_pipeline_tees_generated_wav_into_real_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary default pipeline records generated WAV samples without a device."""
    import struct
    import wave

    import coffee_roaster_mcp.audio as audio_module
    from coffee_roaster_mcp.audio import RoastAudioRecorder, WavAudioInput
    from coffee_roaster_mcp.config import RecordingConfig

    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(struct.pack("<32000h", *([1024] * 32_000)))
    read_started = Event()

    class EventedWavInput:
        def __init__(self) -> None:
            self._source = WavAudioInput(source, sample_rate=16_000)

        def read_samples(self, sample_count: int) -> Sequence[float]:
            read_started.set()
            return self._source.read_samples(sample_count)

        def close(self) -> None:
            self._source.close()

    def wav_input(_settings: AudioCaptureSettings) -> EventedWavInput:
        return EventedWavInput()

    monkeypatch.setattr(audio_module, "build_configured_audio_input", wav_input)
    store = RoastSessionStore()
    session = store.start_session()
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(source="microphone", sample_rate=16_000),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
            recording=RecordingConfig(
                enabled=True, autocapture=True, export_location=tmp_path / "out"
            ),
        ),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )
    runtime.start_for_session(session)
    assert read_started.wait(timeout=1.0)
    assert runtime.finalise_for_session(session.id) == ("stopped", None, False)
    recorder, plan = runtime.recording_for_session(session.id)
    assert isinstance(recorder, RoastAudioRecorder)
    assert plan is not None and plan.primary_wav.is_file()


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


def test_recording_relative_milestones_first_crack_override_bypasses_session_field() -> None:
    """recording_relative_milestones's override wins over session.first_crack_monotonic_seconds
    without ever touching the session field itself (coffee-roaster-mcp#191)."""
    from coffee_roaster_mcp.first_crack_runtime import recording_relative_milestones

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()

    # The session's own field stays None (a post-drop crack never writes it).
    assert session.first_crack_monotonic_seconds is None

    milestones = recording_relative_milestones(
        session,
        recording_started_monotonic_seconds=None,
        first_crack_override_monotonic_seconds=42.5,
    )

    assert milestones["first_crack"] == 42.5
    assert session.first_crack_monotonic_seconds is None  # still untouched

    # Omitting the override falls back to the session's own value, unchanged.
    session.first_crack_monotonic_seconds = 10.0
    fallback = recording_relative_milestones(session, recording_started_monotonic_seconds=None)
    assert fallback["first_crack"] == 10.0


def test_built_recorder_recovers_first_crack_milestone_from_holder_not_session(
    tmp_path: Path,
) -> None:
    """End to end (coffee-roaster-mcp#191): a value written into the
    recovered_first_crack_holder overrides the sidecar's first_crack
    milestone WITHOUT ever writing session.first_crack_monotonic_seconds or a
    first_crack_detected timeline event — proving the recording and the
    session event log are genuinely independent contracts, exactly as
    process_pending_windows_after_drop relies on.
    """
    import json

    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    recovered_first_crack_holder: list[float | None] = []
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
        recovered_first_crack_holder=recovered_first_crack_holder,
    )
    assert recorder is not None

    recorder.begin()
    recording_started = recorder.started_monotonic_seconds
    assert recording_started is not None
    recording_start_session_elapsed = recording_started - session.monotonic_start

    session.beans_added_monotonic_seconds = 12.0
    store.record_event(session, "beans_added")
    store.record_event(session, "beans_dropped")
    assert session.phase == "dropped"
    # Simulate process_pending_windows_after_drop writing a recovered
    # SESSION-ELAPSED first-crack timestamp into the holder — never into
    # session.first_crack_monotonic_seconds and never as a timeline event.
    recovered_first_crack_holder.append(95.5)

    recorder.write_samples((0.1,) * 8)
    recorder.close()

    sidecar = json.loads(
        (tmp_path / session.id / "roast.recording.json").read_text(encoding="utf-8")
    )
    assert sidecar["milestones"]["first_crack"] == round(95.5 - recording_start_session_elapsed, 6)
    # The control-timeline contract stays untouched by the recovery.
    assert session.first_crack_monotonic_seconds is None
    assert "first_crack_detected" not in [event.kind for event in session.event_timeline]


def test_process_pending_windows_after_drop_recovers_holder_and_logs_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """process_pending_windows_after_drop recovers the holder and logs a
    WARNING breadcrumb (coffee-roaster-mcp#191): a complete pre-drop window
    (end <= drop cutoff) is classified after beans_dropped, written into
    _recovered_first_crack_holder, and never touches the session timeline.
    """
    import logging

    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = TimedDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True, confidence=0.94, detected_at_monotonic_seconds=506.0
            ),
        ),
        clock=clock,
        durations_seconds=(0.25,),
    )
    # The window's end (505.0 + 1.0 = 506.0) is strictly before the drop at
    # session-elapsed 20.0 (absolute 520.0) — a genuinely complete pre-drop
    # window, the case #191 reports.
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=505.0),)
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(sample_rate=100, hop_seconds=0.25),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )
    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")
    assert session.phase == "dropped"

    with caplog.at_level(logging.WARNING, logger="coffee_roaster_mcp.first_crack_runtime"):
        result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status == "detected"
    assert result.last_inference_duration_ms == pytest.approx(250.0)
    assert result.max_inference_duration_ms == pytest.approx(250.0)
    assert result.inference_overrun_count == 1
    assert any(
        "recovered" in record.message and "coffee-roaster-mcp#191" in record.message
        for record in caplog.records
    )
    # Backdated crack onset (window seq 1 starts at 505.0, absolute) rebased
    # into SESSION-ELAPSED: 505.0 - session.monotonic_start (500.0) = 5.0.
    recovered_holder = runtime._recovered_first_crack_holder  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    assert recovered_holder == [5.0]
    # The control timeline stays untouched — the whole point of #191's fix.
    assert session.first_crack_monotonic_seconds is None
    assert [event.kind for event in session.event_timeline] == ["beans_added", "beans_dropped"]


def test_process_pending_windows_after_drop_rejects_a_precharge_candidate() -> None:
    """A recovered milestone must not be seeded by a pre-charge candidate
    window (coffee-roaster-mcp#192 follow-up on #191).

    With min_positive_windows > 1, a positive window whose capture STARTED
    before beans_added can still reach the detector adapter: the live path
    (process_available_windows) is gated on session.phase == "roasting" at
    the METHOD level, so nothing drains while phase is still "pre_roast" —
    but the moment beans_added flips the phase, whatever is still sitting in
    the pipeline's queue from before the charge is drained for the first
    time and fed to the adapter, with no check on the window's own start
    time. That candidate then sits in the adapter's rolling
    confirmation-window state and can be completed by a SECOND positive
    window that only gets classified post-drop via
    process_pending_windows_after_drop (the #191 straggler-drain path),
    producing a recovered onset that is seeded by pre-charge audio (empty
    drum rattle, charge pour — both crack-like transients) rather than a
    real first-crack window.
    """
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = MockDetectorBackend(
        (
            # Window 1: PRE-CHARGE, started at 490.0 — drained only once
            # beans_added flips phase to "roasting" at 505.0.
            FirstCrackDetectorOutput(
                confirmed=True, confidence=0.9, detected_at_monotonic_seconds=491.0
            ),
            # Window 2: the genuine #191 straggler — completes but isn't
            # drained until AFTER the drop.
            FirstCrackDetectorOutput(
                confirmed=True, confidence=0.94, detected_at_monotonic_seconds=506.0
            ),
        )
    )
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=490.0),)
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0", min_positive_windows=2)
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
    )
    runtime.start_for_session(session)

    # Pre-charge: window 1 is queued but the live path is a no-op (phase is
    # still "pre_roast") — this mirrors
    # test_audio_runtime_processes_after_beans_added_and_records_once's
    # pre_t0 assertion.
    pre_t0 = runtime.process_available_windows(session_store=store, session=session)
    assert pre_t0.queued_window_count == 1

    # beans_added flips phase to "roasting"; the NEXT call drains window 1
    # for the first time and feeds it to the adapter as the first positive
    # candidate, even though its capture started before the charge.
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    after_charge = runtime.process_available_windows(session_store=store, session=session)
    assert after_charge.status == "pending"  # only 1 of 2 required candidates so far

    # A second, genuinely post-charge positive window is captured but not
    # drained by the live path before the drop fires (the #191 scenario).
    pipeline.add_window(_audio_window(sequence_number=2, started_at_monotonic_seconds=505.5))
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    recovered_holder = runtime._recovered_first_crack_holder  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    assert result.status != "detected", (
        "a recovered milestone must not be confirmed by a candidate whose "
        "window started before beans_added"
    )
    assert recovered_holder == []
    assert session.first_crack_monotonic_seconds is None


def test_process_pending_windows_after_drop_rejects_a_straddling_window() -> None:
    """A window whose END is at/after the drop is rejected (coffee-roaster-mcp#191):
    the drop's own cascading-beans clatter is acoustically crack-like, so a
    straddling window's tail could confirm a phantom milestone."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = MockDetectorBackend(
        (
            FirstCrackDetectorOutput(
                confirmed=True, confidence=0.94, detected_at_monotonic_seconds=506.0
            ),
        )
    )
    # started_at=505.0 + duration=20.0 => end=525.0, AFTER the drop at
    # session-elapsed 20.0 (absolute 520.0) — a straddler.
    pipeline = FakeAudioPipeline(
        (
            AudioWindow(
                sequence_number=1,
                input_device="fake-mic",
                sample_rate=16_000,
                started_at_monotonic_seconds=505.0,
                duration_seconds=20.0,
                samples=(0.0,) * 16_000,
            ),
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
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status != "detected"
    recovered_holder = runtime._recovered_first_crack_holder  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    assert recovered_holder == []
    assert session.first_crack_monotonic_seconds is None
    assert backend.windows == []  # the detector was never even invoked
    assert result.last_inference_duration_ms == 0.0
    assert result.max_inference_duration_ms == 0.0
    assert result.inference_overrun_count == 0


def test_process_pending_windows_after_drop_times_no_detection_result() -> None:
    """An eligible post-drop adapter call records a no-result duration."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    backend = TimedDetectorBackend(
        (FirstCrackDetectorOutput(confirmed=False),),
        clock=clock,
        durations_seconds=(0.125,),
    )
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=505.0),)
    )
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            audio=AudioConfig(sample_rate=100, hop_seconds=0.25),
            first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0"),
        ),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            backend,
        ),
        monotonic_now=clock.monotonic_now,
    )
    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status == "pending"
    assert result.last_inference_duration_ms == pytest.approx(125.0)
    assert result.max_inference_duration_ms == pytest.approx(125.0)
    assert result.inference_overrun_count == 0


def test_process_pending_windows_after_drop_skips_detector_paced_wav_replay() -> None:
    """Detector-paced WAV replay is a test/replay concern with its own
    deterministic clock, not a live drop race — the post-drop drain no-ops
    for it rather than draining against a mismatched clock domain (#191)."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline((_audio_window(sequence_number=1),))
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
            MockDetectorBackend(()),
        ),
    )
    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status == "pending"
    assert pipeline.drain_limits == []  # never even drained


def test_process_pending_windows_after_drop_noops_when_mode_is_not_audio() -> None:
    """A non-audio first-crack mode is a safe no-op (#191): the "pending"
    status/pipeline pair this method needs never exists outside audio mode."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="disabled")),
    )
    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status == "disabled"


def test_process_pending_windows_after_drop_noops_without_a_drop_timestamp() -> None:
    """A session somehow reaching 'dropped'/'cooling' phase without
    beans_dropped_monotonic_seconds set is defensive-only, but must still
    no-op safely rather than crash (#191)."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session()
    pipeline = FakeAudioPipeline((_audio_window(sequence_number=1),))
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts(),
            MockDetectorBackend(()),
        ),
    )
    runtime.start_for_session(session)
    clock.monotonic_value = 505.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 520.0
    store.record_event(session, "beans_dropped")
    # Force the defensive gap directly: a real store always sets this
    # alongside the phase transition, so this line only guards against a
    # future refactor breaking that invariant.
    session.beans_dropped_monotonic_seconds = None

    result = runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert result.status == "pending"
    assert pipeline.drain_limits == []  # never even drained


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


@pytest.mark.parametrize("devices", (("USB PnP",), ("USB PnP", "ATR2100x", "Mic 3")))
def test_recording_artifact_plan_matches_built_recorder_paths(
    tmp_path: Path, devices: tuple[str, ...]
) -> None:
    """Single and multi-device recorder construction preserves the pure artifact plan."""
    from coffee_roaster_mcp.first_crack_runtime import (
        RecordingMetadata,
        build_session_recorder,
        plan_session_recording_artifacts,
    )

    session = RoastSessionStore().start_session()
    config = AppConfig(
        recording=RecordingConfig(
            enabled=True,
            autocapture=True,
            export_location=tmp_path,
            devices=devices,
        )
    )
    metadata = RecordingMetadata(origin="brazil", roast_num=7)
    plan = plan_session_recording_artifacts(config, session, metadata)
    recorder = build_session_recorder(config, session, metadata=metadata)

    assert plan is not None and recorder is not None
    assert cast(Any, recorder).wav_path == plan.primary_wav
    assert cast(Any, recorder).sidecar_path == plan.recording_sidecar
    assert cast(Any, recorder)._annotation_session.path == plan.annotation_session_sidecar
    assert getattr(recorder, "additional_wav_paths", ()) == plan.additional_wavs


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


def test_finalise_for_session_stops_without_inference_and_preserves_other_session() -> None:
    """Cold finalisation stops only its capture and does not invoke the detector."""
    clock = ClockHarness()
    store = RoastSessionStore(utc_now=clock.utc_now, monotonic_now=clock.monotonic_now)
    session = store.start_session(purpose="cold_characterisation")
    pipeline = FakeAudioPipeline(
        (_audio_window(sequence_number=1, started_at_monotonic_seconds=1),)
    )
    backend = MockDetectorBackend(())
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), backend
        ),
    )
    runtime.start_for_session(session)

    outcome, error, running = runtime.finalise_for_session("other-session")
    assert (outcome, error, running) == ("not_active", None, False)
    assert pipeline.stopped is False

    outcome, error, running = runtime.finalise_for_session(session.id)
    assert (outcome, error, running) == ("stopped", None, False)
    assert pipeline.stopped is True
    assert backend.windows == []
    assert runtime.recording_for_session(session.id) == (None, None)


@pytest.mark.parametrize("mode", ("no_pipeline", "stop_failure", "still_running"))
def test_finalise_for_session_reports_nonclean_capture_states(mode: str) -> None:
    """Finalisation preserves bounded capture failures without detector inference."""
    store = RoastSessionStore()
    session = store.start_session(purpose="cold_characterisation")
    pipeline = FakeAudioPipeline(running_after_stop=mode == "still_running")
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )
    runtime.start_for_session(session)
    if mode == "no_pipeline":
        runtime._pipeline = None  # pyright: ignore[reportPrivateUsage]
        runtime._capture_started_for_session = False  # pyright: ignore[reportPrivateUsage]
        expected = ("not_active", None, False)
    elif mode == "stop_failure":

        def fail_stop(*, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
            del timeout_seconds
            raise RuntimeError("stop failed")

        pipeline.stop = fail_stop  # type: ignore[method-assign]
        expected = ("stop_failed", "RuntimeError: stop failed", True)
    else:
        expected = ("capture_still_running", None, True)
    assert runtime.finalise_for_session(session.id) == expected
    if mode == "still_running":
        pipeline.running_after_stop = False
        assert runtime.finalise_for_session(session.id) == ("stopped", None, False)


def test_finalise_recognises_capture_stopped_before_finalisation() -> None:
    """A prior confirmed stop is not mistaken for never-started capture."""
    store = RoastSessionStore()
    session = store.start_session(purpose="cold_characterisation")
    pipeline = FakeAudioPipeline()
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )
    runtime.start_for_session(session)
    runtime.stop_for_session(session.id, reason="operator stopped capture")
    assert runtime.finalise_for_session(session.id) == ("stopped", None, False)


def test_post_drop_windows_are_not_inferred_after_teardown_begins() -> None:
    """A retained, unconfirmed stop never feeds post-drop windows to the detector."""
    store = RoastSessionStore()
    session = store.start_session(purpose="cold_characterisation")
    pipeline = FakeAudioPipeline((_audio_window(sequence_number=1),), running_after_stop=True)
    backend = MockDetectorBackend(())
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), backend
        ),
    )
    runtime.start_for_session(session)
    assert runtime.finalise_for_session(session.id)[0] == "capture_still_running"
    store.record_event(session, "beans_added")
    store.record_event(session, "beans_dropped")

    runtime.process_pending_windows_after_drop(session_store=store, session=session)

    assert backend.windows == []
    assert pipeline.drain_limits == []


def test_recorder_build_failure_cannot_expose_prior_session_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting a new session clears prior recorder state before recorder construction."""
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    config = AppConfig(
        first_crack=FirstCrackConfig(mode="audio"),
        recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
    )
    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=lambda _: FakeAudioPipeline(),
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )
    first = RoastSessionStore().start_session()
    runtime.start_for_session(first)
    assert runtime.recording_for_session(first.id)[1] is not None

    def fail_recorder(*_args: object, **_kwargs: object) -> RoastRecorder:
        raise RuntimeError("recorder build failed")

    monkeypatch.setattr(runtime_module, "build_session_recorder", fail_recorder)
    second = RoastSessionStore().start_session()
    with pytest.raises(RuntimeError, match="recorder build failed"):
        runtime.start_for_session(second)
    assert runtime.recording_for_session(second.id) == (None, None)


def test_finalisation_fence_session_error_preserves_live_capture_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalisation-race write rejection is an expected inference skip, not a fault."""
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    store = RoastSessionStore()
    session = store.start_session(purpose="cold_characterisation")
    store.record_event(session, "beans_added")
    pipeline = FakeAudioPipeline((_audio_window(sequence_number=1),))
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config, _resolved_detector_artifacts(), MockDetectorBackend(())
        ),
    )
    runtime.start_for_session(session)
    _, rejection, _ = store.begin_finalisation(session.id)
    assert rejection is None

    def fenced_write(**_kwargs: object) -> None:
        raise SessionLifecycleError("Session finalisation is in progress.")

    monkeypatch.setattr(runtime_module, "integrate_first_crack_window_with_session", fenced_write)
    snapshot = runtime.process_available_windows(session_store=store, session=session)
    assert snapshot.status == "pending", snapshot.reason
    assert snapshot.audio_running is True
    store.abandon_finalisation_admission(session)
    pipeline.add_window(_audio_window(sequence_number=2))
    unfenced = runtime.process_available_windows(session_store=store, session=session)
    assert unfenced.status == "faulted"
    assert unfenced.reason == "First-crack detection failed: Session finalisation is in progress."
    assert runtime.finalise_for_session(session.id)[0] == "stopped"


def test_manual_authority_direct_runtime_refuses_before_capture(tmp_path: Path) -> None:
    calls: list[str] = []
    config = AppConfig(first_crack=FirstCrackConfig(mode="audio", event_authority="manual"))

    def forbidden_loader(_: FirstCrackConfig):
        raise RuntimeError("Model loading must not occur in this refusal witness.")

    runtime = FirstCrackSessionRuntime(
        config=config,
        observation_model_loader=forbidden_loader,
        audio_pipeline_factory=lambda _: calls.append("capture") or FakeAudioPipeline(),
    )
    store = RoastSessionStore(default_log_dir=tmp_path)
    snapshot = runtime.start_for_session(store.start_session())
    assert snapshot.status == "unavailable"
    assert snapshot.reason == "manual_authority_not_admitted"
    assert snapshot.observation is not None
    assert snapshot.observation.observer_state == "not_admitted"
    assert not calls


def test_manual_mark_preserves_synthetic_recording_and_canonical_milestone(tmp_path: Path) -> None:
    import wave

    from coffee_roaster_mcp.config import LoggingConfig
    from coffee_roaster_mcp.first_crack_observer import load_observation_model

    config = AppConfig(
        first_crack=FirstCrackConfig(mode="audio", event_authority="manual"),
        recording=RecordingConfig(
            enabled=True, autocapture=True, export_location=tmp_path / "recordings"
        ),
        logging=LoggingConfig(log_dir=tmp_path / "logs"),
    )

    class RecordingPipeline(FakeAudioPipeline):
        recorder: RoastRecorder | None = None

        def start(self) -> AudioCaptureSnapshot:
            assert self.recorder is not None
            self.recorder.begin()
            self.recorder.write_samples((0.0,) * 160)
            return super().start()

        def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
            assert self.recorder is not None
            if not self.stopped:
                self.recorder.write_samples((0.0,) * 160)
                self.recorder.close()
            return super().stop(timeout_seconds=timeout_seconds)

    pipeline = RecordingPipeline()
    model = tmp_path / "synthetic.onnx"
    model.write_bytes(b"synthetic")
    prep = tmp_path / "synthetic.json"
    prep.write_text("{}", encoding="utf-8")
    artifacts = ResolvedDetectorArtifacts(
        ResolvedArtifact("test", None, "model.onnx", model),
        ResolvedArtifact("test", None, "preprocessor.json", prep),
    )

    def capture_factory(_: AudioConfig) -> RecordingPipeline:
        pipeline.recorder = runtime._pending_recorder  # pyright: ignore[reportPrivateUsage] - synthetic boundary witness
        return pipeline

    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=capture_factory,
        observation_model_loader=lambda fc: load_observation_model(
            fc,
            resolver=lambda _: artifacts,
            backend_factory=lambda _config, _artifacts: MockDetectorBackend(()),
        ),
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "logs")
    session = store.start_session()
    runtime.start_for_session(session)
    store.record_event(session, "beans_added")
    runtime.process_available_windows(session_store=store, session=session)
    event = store.record_event(session, "first_crack_detected")
    runtime.notify_human_mark(session)
    assert not pipeline.stopped
    assert runtime._recovered_first_crack_holder == []  # pyright: ignore[reportPrivateUsage] - synthetic boundary witness
    assert session.first_crack_monotonic_seconds == event.monotonic_seconds
    store.record_event(session, "beans_dropped")
    runtime.close_observation(session, reason="beans_dropped")
    assert runtime._observer is not None and runtime._observer.reaped.wait(2)  # pyright: ignore[reportPrivateUsage] - synthetic boundary witness
    _, plan = runtime.recording_for_session(session.id)
    assert plan is not None
    with wave.open(str(plan.primary_wav), "rb") as stream:
        assert stream.getnframes() == 320
    assert plan.recording_sidecar.is_file()
    assert session.first_crack_monotonic_seconds == event.monotonic_seconds


def test_manual_recording_continues_during_pending_boundary_until_real_freeze(
    tmp_path: Path,
) -> None:
    """A pending public boundary retains the owned synthetic recorder until it freezes."""
    import wave
    from types import SimpleNamespace

    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    path = tmp_path / "pending-recording-context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    context = build_server_context(config_path=path)
    object.__setattr__(context, "config", config)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str, **arguments: object) -> Any:
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    started = call("start_roast_session", first_crack_observer_ack="manual/v1")
    session = context.session_store.get_session_snapshot(session_id=started.session.session_id)
    context.telemetry_sampler.stop_for_session(session.id, reason="synthetic test")
    call("mark_beans_added")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None and len(captures) == 1
    capture = cast(Any, captures[0])
    recorder = cast(RoastRecorder, capture.recorder)
    assert recorder is not None
    ticket = runtime.begin_observation_boundary(session, reason="beans_dropped")
    assert ticket is not None and not capture.stopped
    # The capture-owned recorder keeps accepting synthetic samples while the
    # canonical event has not accepted the ticket.
    recorder.write_samples((0.0,) * 160)
    assert not capture.stopped and observer.summary is None
    runtime.freeze_observation_boundary(session, ticket, reason="synthetic_pending_cleanup")
    observer.changed.set()
    assert observer.reaped.wait(2)
    retained_recorder, plan = runtime.recording_for_session(session.id)
    assert retained_recorder is recorder and plan is not None
    with wave.open(str(plan.primary_wav), "rb") as stream:
        assert stream.getnframes() == 480
    assert plan.recording_sidecar.is_file() and plan.annotation_session_sidecar.is_file()
    assert observer.summary is not None and observer.summary.drop_cutoff.value is None


def _manual_runtime_fixture(
    tmp_path: Path,
    *,
    stop_barrier: tuple[Event, Event] | None = None,
    fail_capture_start: bool = False,
    session_is_retained: Callable[[str], bool] | None = None,
):
    from coffee_roaster_mcp.config import LoggingConfig
    from coffee_roaster_mcp.first_crack_observer import load_observation_model

    model = tmp_path / "synthetic-model"
    model.write_bytes(b"synthetic")
    prep = tmp_path / "synthetic-prep"
    prep.write_text("{}", encoding="utf-8")
    artifacts = ResolvedDetectorArtifacts(
        ResolvedArtifact("test", None, "model.onnx", model),
        ResolvedArtifact("test", None, "preprocessor.json", prep),
    )
    config = AppConfig(
        first_crack=FirstCrackConfig(mode="audio", event_authority="manual"),
        recording=RecordingConfig(
            enabled=True, autocapture=True, export_location=tmp_path / "recordings"
        ),
        logging=LoggingConfig(log_dir=tmp_path / "logs"),
    )
    captures: list[FakeAudioPipeline] = []

    class RecordingCapture(FakeAudioPipeline):
        recorder: RoastRecorder | None = None

        def start(self) -> AudioCaptureSnapshot:
            assert self.recorder is not None
            self.recorder.begin()
            self.recorder.write_samples((0.0,) * 160)
            result = super().start()
            if fail_capture_start:
                raise AudioCaptureError("synthetic partial start failure")
            return result

        def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
            if stop_barrier is not None:
                stop_barrier[0].set()
                assert stop_barrier[1].wait(5)
            if self.recorder is not None and not self.stopped:
                self.recorder.write_samples((0.0,) * 160)
                self.recorder.close()
            return super().stop(timeout_seconds=timeout_seconds)

    def capture_factory(_: AudioConfig) -> RecordingCapture:
        pipeline = RecordingCapture()
        pipeline.recorder = runtime._pending_recorder  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
        captures.append(pipeline)
        return pipeline

    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=capture_factory,
        session_is_retained=session_is_retained,
        observation_model_loader=lambda fc: load_observation_model(
            fc,
            resolver=lambda _: artifacts,
            backend_factory=lambda _config, _artifacts: MockDetectorBackend(()),
        ),
    )
    return runtime, captures, config


@pytest.mark.parametrize("lifecycle", ["stop", "finalise"])
def test_manual_runtime_lifecycle_close_has_no_synthetic_cutoff(
    tmp_path: Path, lifecycle: str
) -> None:
    """Runtime stop/finalisation closes diagnostic acceptance without a drop event."""
    runtime, captures, _ = _manual_runtime_fixture(tmp_path)
    store = RoastSessionStore(default_log_dir=tmp_path / "lifecycle")
    session = store.start_session()
    runtime.start_for_session(session)
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage] - public lifecycle witness
    assert observer is not None and len(captures) == 1
    if lifecycle == "stop":
        runtime.stop_for_session(session.id, reason="process_shutdown")
    else:
        state, _, _ = runtime.finalise_for_session(session.id)
        assert state in {"stopped", "capture_still_running"}
    assert observer.reaped.wait(2)
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert captures[0].stopped


def test_manual_capture_setup_failure_closes_running_observer_without_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-start setup failure retains truthful null cutoff evidence."""
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    runtime, captures, _ = _manual_runtime_fixture(tmp_path)
    original_start = runtime_module.FirstCrackObserver.start

    def start_then_fail(self: FirstCrackObserver, reservation: object | None = None) -> bool:
        assert original_start(self, reservation)
        raise RuntimeError("synthetic post-start setup failure")

    monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", start_then_fail)
    store = RoastSessionStore(default_log_dir=tmp_path / "setup-failure")
    runtime.start_for_session(store.start_session())
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage] - setup witness
    assert observer is not None and len(captures) == 1
    assert observer.reaped.wait(2)
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert captures[0].stopped


@pytest.mark.parametrize(
    "failure", ["constructor", "observer_start", "observer_refused", "capture_start"]
)
def test_partial_setup_keeps_single_slot_until_off_request_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import threading

    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    entered = Event()
    release = Event()
    runtime, captures, config = _manual_runtime_fixture(
        tmp_path, stop_barrier=(entered, release), fail_capture_start=failure == "capture_start"
    )
    if failure == "constructor":

        def fail_constructor(**_: object):
            raise RuntimeError("synthetic constructor failure")

        monkeypatch.setattr(runtime_module, "FirstCrackObserver", fail_constructor)
    elif failure == "observer_start":

        def fail_start(self: object, reservation: object | None = None) -> bool:
            raise RuntimeError("synthetic start failure")

        monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", fail_start)
    if failure == "observer_refused":

        def refused_start(self: object, reservation: object | None = None) -> bool:
            return False

        monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", refused_start)
    store = RoastSessionStore(default_log_dir=tmp_path / "logs")
    snapshot = runtime.start_for_session(store.start_session())
    assert snapshot.status == "unavailable" and entered.wait(2)
    cleanup = runtime._capture_cleanup  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    assert cleanup is not None and not cleanup.reaped.is_set()
    assert snapshot.observation is not None and not snapshot.observation.worker_reaped
    assert len(captures) == 1 and not captures[0].stopped
    assert (
        sum(
            t.name in {"fc-observation-worker", "fc-observation-cleanup"}
            for t in threading.enumerate()
        )
        <= 1
    )
    calls: list[str] = []
    next_runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=lambda _: calls.append("capture") or FakeAudioPipeline(),
    )
    other_store = RoastSessionStore(default_log_dir=tmp_path / "other")
    denied = next_runtime.start_for_session(other_store.start_session())
    assert denied.reason == "previous_observer_not_reaped" and not calls
    release.set()
    assert cleanup.reaped.wait(2)
    assert captures[0].stopped and not runtime.snapshot().active


def test_public_runtime_supersession_cannot_publish_old_result(tmp_path: Path) -> None:
    runtime, _captures, _config = _manual_runtime_fixture(tmp_path)
    entered = Event()
    release = Event()
    original = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(fc)

    runtime._observation_model_loader = blocked  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    first_store = RoastSessionStore(default_log_dir=tmp_path / "first")
    first = first_store.start_session()
    runtime.start_for_session(first)
    assert entered.wait(2)
    old = runtime._observer  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    assert old is not None
    next_store = RoastSessionStore(default_log_dir=tmp_path / "next")
    second = next_store.start_session()
    denied = runtime.start_for_session(second)
    assert denied.active_session_id == second.id and denied.status == "unavailable"
    assert denied.observation is not None and denied.observation.first_confirmation.value is None
    release.set()
    assert old.reaped.wait(2)
    after = runtime.snapshot()
    assert after.active_session_id == second.id
    assert after.observation is not None and after.observation.first_confirmation.value is None
    assert first.first_crack_at_utc is None and second.first_crack_at_utc is None


def test_successful_manual_finalise_accepts_next_recording_metadata(tmp_path: Path) -> None:
    runtime, _captures, _ = _manual_runtime_fixture(tmp_path)
    store = RoastSessionStore(default_log_dir=tmp_path / "logs")
    runtime.set_recording_metadata(origin="first", roast_num=1)
    first = store.start_session()
    runtime.start_for_session(first)
    runtime.close_observation(first, reason="beans_dropped")
    assert runtime._observer is not None and runtime._observer.reaped.wait(2)  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    assert runtime.finalise_for_session(first.id) == ("stopped", None, False)
    assert not runtime.snapshot().active and not runtime._recorder_built_for_session  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    assert runtime.snapshot().observation is not None
    runtime.set_recording_metadata(origin="second", roast_num=2)
    second_store = RoastSessionStore(default_log_dir=tmp_path / "second")
    second = second_store.start_session()
    runtime.start_for_session(second)
    _, plan = runtime.recording_for_session(second.id)
    assert plan is not None and "second-roast2" in plan.primary_wav.name
    runtime.close_observation(second, reason="beans_dropped")
    assert runtime._observer is not None and runtime._observer.reaped.wait(2)  # pyright: ignore[reportPrivateUsage] - synthetic lifecycle witness
    assert runtime.finalise_for_session(second.id) == ("stopped", None, False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence_threshold", float("nan")),
        ("min_positive_windows", 0),
        ("confirmation_window_seconds", float("inf")),
        ("hop_seconds", 0.0),
    ],
)
def test_invalid_direct_manual_settings_refuse_before_capture(
    tmp_path: Path, field: str, value: float
) -> None:
    from dataclasses import replace

    _runtime, captures, config = _manual_runtime_fixture(tmp_path)
    bad = (
        replace(config, audio=replace(config.audio, hop_seconds=value))
        if field == "hop_seconds"
        else replace(config, first_crack=replace(config.first_crack, **{field: value}))
    )
    calls: list[str] = []
    refused = FirstCrackSessionRuntime(
        config=bad, audio_pipeline_factory=lambda _: calls.append("capture") or FakeAudioPipeline()
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "logs")
    snapshot = refused.start_for_session(store.start_session())
    assert snapshot.status == "unavailable" and snapshot.observation is None
    assert snapshot.reason == "manual_authority_not_admitted"
    assert not calls and not captures


def test_injected_missing_recorder_is_never_started_not_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    _unused, _, config = _manual_runtime_fixture(tmp_path)

    def no_recorder(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runtime_module, "build_session_recorder", no_recorder)
    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=lambda _: FakeAudioPipeline(),
        observation_model_loader=_unused._observation_model_loader,  # pyright: ignore[reportPrivateUsage] - synthetic only
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "missing-recorder")
    session = store.start_session()
    runtime.start_for_session(session)
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage] - injected recorder boundary
    assert observer is not None
    try:
        clock = observer.clock.at(session.monotonic_start)
        assert clock.recording_relative_seconds.value is None
        assert clock.recording_relative_seconds.null_reason == "recorder_never_started"
        assert not observer.clock.recording_pending
    finally:
        runtime.close_observation(session, reason="beans_dropped")
        assert observer.reaped.wait(2)


def test_unreaped_manual_finalisation_reports_existing_detail_token(tmp_path: Path) -> None:
    runtime, _, _ = _manual_runtime_fixture(tmp_path)
    entered, release = Event(), Event()
    original = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(fc)

    runtime._observation_model_loader = blocked  # pyright: ignore[reportPrivateUsage]
    store = RoastSessionStore(default_log_dir=tmp_path / "pending-finalise")
    session = store.start_session()
    runtime.start_for_session(session)
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None
    try:
        assert entered.wait(2)
        status, detail, pending = runtime.finalise_for_session(session.id)
        assert status == "capture_still_running" and pending
        assert detail is not None and "observer_still_running" in detail
    finally:
        release.set()
        assert observer.reaped.wait(2)


def test_supervisor_is_manual_only_singleton_and_survives_failure_callback(tmp_path: Path) -> None:
    """Isolated process: no immortal test supervisor leaks into this pytest process."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = """
import threading
from coffee_roaster_mcp.config import AppConfig, FirstCrackConfig, RecordingConfig
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackSessionRuntime,
)
from coffee_roaster_mcp.first_crack_observer import coordinator
from coffee_roaster_mcp.session import RoastSessionStore

def supervisors():
    return [t for t in threading.enumerate() if t.name == 'fc-observation-supervisor']
auto = FirstCrackSessionRuntime(config=AppConfig())
auto.start_for_session(RoastSessionStore().start_session())
assert supervisors() == []
config = AppConfig(
    first_crack=FirstCrackConfig(mode='audio', event_authority='manual'),
    recording=RecordingConfig(enabled=True, autocapture=True))
first = FirstCrackSessionRuntime(config=config)
second = FirstCrackSessionRuntime(config=config)
assert len(supervisors()) == 1
failed = threading.Event()
class BrokenOwner:
    reaped = threading.Event()
    def check_deadline(self):
        raise ValueError('synthetic deadline error')
    def supervision_failed(self):
        failed.set()
        raise ValueError('synthetic reporting error')
broken = BrokenOwner()
shared = coordinator()
assert shared.claim(broken)
shared.wake.set()
assert failed.wait(2)
broken.reaped.set()
reached = threading.Event()
class ValidOwner:
    reaped = threading.Event()
    def check_deadline(self):
        reached.set()
    def supervision_failed(self):
        raise AssertionError('valid owner must not fail')
valid = ValidOwner()
assert shared.claim(valid)
shared.wake.set()
assert reached.wait(2), 'failure callback killed sole deadline supervisor'
valid.reaped.set()
assert len(supervisors()) == 1 and supervisors()[0].is_alive()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_unknown_direct_authority_refuses_every_legacy_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coffee_roaster_mcp.first_crack_runtime as runtime_module
    from coffee_roaster_mcp.config import FirstCrackEventAuthority

    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object):
        calls.append("effect")
        raise AssertionError("unknown authority reached a legacy effect")

    monkeypatch.setattr(runtime_module, "build_session_recorder", forbidden)
    runtime = FirstCrackSessionRuntime(
        config=AppConfig(
            first_crack=FirstCrackConfig(
                mode="audio", event_authority=cast(FirstCrackEventAuthority, "unknown")
            ),
            recording=RecordingConfig(enabled=True, autocapture=True),
        ),
        audio_pipeline_factory=forbidden,
        detector_adapter_factory=forbidden,
        observation_model_loader=forbidden,
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "unknown")
    snapshot = runtime.start_for_session(store.start_session())
    assert snapshot.status == "unavailable"
    assert snapshot.reason == "first_crack_authority_not_admitted"
    assert not snapshot.audio_running and snapshot.observation is None
    assert not calls


def test_invalid_direct_driver_preserves_representable_refusal(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from coffee_roaster_mcp.config import RoasterConfig

    _unused, _, config = _manual_runtime_fixture(tmp_path)
    config = replace(config, roaster=RoasterConfig(driver="hottop_kn8828b_2k_plus"))
    calls: list[str] = []
    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=lambda _: calls.append("capture") or FakeAudioPipeline(),
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "invalid-driver")
    snapshot = runtime.start_for_session(store.start_session())
    assert snapshot.status == "unavailable" and snapshot.reason == "manual_authority_not_admitted"
    assert snapshot.observation is not None
    assert snapshot.observation.observer_state == "not_admitted"
    assert (
        snapshot.observation.settings.confidence_threshold
        == config.first_crack.confidence_threshold
    )
    assert not calls


def test_late_release_keeps_tools_sampler_and_finalise_live_then_admits_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from types import SimpleNamespace

    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    config_file = tmp_path / "context.yaml"
    config_file.write_text(
        "first_crack:\n  mode: audio\n  event_authority: manual\n"
        "recording:\n  enabled: true\n  autocapture: true\n"
        f"logging:\n  log_dir: {tmp_path / 'context-logs'}\n",
        encoding="utf-8",
    )
    context = build_server_context(config_path=config_file)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=config_file)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str):
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx)

    session = context.session_store.start_session()
    runtime.start_for_session(session)
    pipeline = captures[0]
    released, persisted, after_persist = Event(), Event(), Event()
    stop_calls: list[str] = []
    original_stop = pipeline.stop

    def stop(*, timeout_seconds: float = 1.0):
        stop_calls.append("stop")
        return original_stop(timeout_seconds=timeout_seconds)

    def capture_released(self: FakeAudioPipeline) -> bool:
        if self is pipeline:
            if persisted.is_set():
                after_persist.set()
            return released.is_set()
        return self.stopped

    monkeypatch.setattr(pipeline, "stop", stop)
    monkeypatch.setattr(type(pipeline), "shutdown_confirmed", property(capture_released))
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None
    call("mark_beans_added")
    runtime.close_observation(session, reason="beans_dropped")
    try:
        # Public state polling and the sampler capability must remain available
        # while that same observation worker is waiting for resource release.
        def no_join(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("request joined observation worker")

        assert observer._thread is not None  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setattr(observer._thread, "join", no_join)  # pyright: ignore[reportPrivateUsage]
        deadline = time.monotonic() + 2
        while observer.snapshot().durability != "persisted":
            assert time.monotonic() < deadline
            observer.changed.wait(0.001)
        persisted.set()
        observer.changed.set()
        assert after_persist.wait(2)
        assert observer.worker_running and not observer.reaped.is_set()
        assert observer.summary is not None
        frozen = observer.summary.model_dump_json()
        state = call("get_roast_state")
        assert state.session_id == session.id
        runtime.process_available_windows(session_store=context.session_store, session=session)
        call("mark_first_crack")
        assert session.first_crack_at_utc is not None
        call("drop_beans")
        call("stop_cooling")
        assert runtime.finalise_for_session(session.id)[0] == "capture_still_running"
        assert stop_calls == ["stop"]
    finally:
        released.set()
        observer.changed.set()
        assert observer.reaped.wait(2)
    assert runtime.finalise_for_session(session.id) == ("stopped", None, False)
    assert observer.summary.model_dump_json() == frozen
    assert stop_calls == ["stop"]
    # A different runtime proves the process-wide slot was released, not merely
    # a local finalisation flag. All capture/model work remains synthetic.
    successor = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=lambda _: FakeAudioPipeline(),
        observation_model_loader=runtime._observation_model_loader,  # pyright: ignore[reportPrivateUsage]
    )
    other = RoastSessionStore(default_log_dir=tmp_path / "successor").start_session()
    admitted = successor.start_for_session(other)
    next_observer = successor._observer  # pyright: ignore[reportPrivateUsage]
    assert next_observer is not None and admitted.status == "pending"
    successor.close_observation(other, reason="beans_dropped")
    assert next_observer.reaped.wait(2)


def _retains_session(store: RoastSessionStore, session_id: str) -> bool:
    try:
        store.get_session_snapshot(session_id=session_id)
    except SessionLifecycleError:
        return False
    return True


def _complete_manual_observation(runtime: FirstCrackSessionRuntime, store: RoastSessionStore):
    session = store.start_session()
    runtime.start_for_session(session)
    store.record_event(session, "beans_added")
    store.record_event(session, "first_crack_detected")
    runtime.notify_human_mark(session)
    store.record_event(session, "beans_dropped")
    runtime.close_observation(session, reason="beans_dropped")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage] - scoped synthetic lifecycle
    assert observer is not None and observer.reaped.wait(2)
    assert runtime.finalise_for_session(session.id) == ("stopped", None, False)
    store.stop_session()
    return session


@pytest.mark.parametrize("limit", [1, 3, 12])
def test_observation_history_follows_actual_store_retention(tmp_path: Path, limit: int) -> None:
    import threading

    store = RoastSessionStore(default_log_dir=tmp_path / "retained", session_history_limit=limit)
    runtime: FirstCrackSessionRuntime
    threads: list[str] = []

    def retained(session_id: str) -> bool:
        assert not runtime._lock._is_owned()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportUnknownMemberType]
        threads.append(threading.current_thread().name)
        return _retains_session(store, session_id)

    runtime, _, _ = _manual_runtime_fixture(tmp_path, session_is_retained=retained)
    sessions = [_complete_manual_observation(runtime, store) for _ in range(limit + 2)]
    retained_ids = {session.id for session in sessions[-limit:]}
    for session in sessions:
        observation = runtime.observation_for_session(session.id)
        if session.id not in retained_ids:
            assert observation is None
        else:
            assert observation is not None and observation.worker_reaped
            summary = observation.frozen_summary.value
            assert summary is not None and summary.human_mark.value is not None
            assert (
                summary.human_mark.value.session_relative_seconds.value
                == session.first_crack_monotonic_seconds
            )
    history = runtime._observation_history  # pyright: ignore[reportPrivateUsage]
    assert set(history) <= retained_ids and len(history) <= limit
    assert all(entry.observer is None and entry.cleanup is None for entry in history.values())
    assert threads and set(threads) == {threading.current_thread().name}


def test_observation_history_without_membership_is_current_only(tmp_path: Path) -> None:
    runtime, _, _ = _manual_runtime_fixture(tmp_path)
    store = RoastSessionStore(default_log_dir=tmp_path / "current-only")
    first = _complete_manual_observation(runtime, store)
    second = _complete_manual_observation(runtime, store)
    assert runtime.observation_for_session(first.id) is None
    assert runtime.observation_for_session(second.id) is not None
    assert not runtime._observation_history  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("limit", [1, 3])
def test_pending_history_is_own_generation_and_prunes_evicted_owner(
    tmp_path: Path, limit: int
) -> None:
    store = RoastSessionStore(
        default_log_dir=tmp_path / "pending-history", session_history_limit=limit
    )
    runtime, _, _ = _manual_runtime_fixture(
        tmp_path, session_is_retained=lambda sid: _retains_session(store, sid)
    )
    entered, release = Event(), Event()
    original = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(fc)

    runtime._observation_model_loader = blocked  # pyright: ignore[reportPrivateUsage]
    first = store.start_session()
    runtime.start_for_session(first)
    owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    try:
        assert entered.wait(2)
        store.stop_session()
        second = store.start_session()
        refused = runtime.start_for_session(second)
        assert refused.reason == "previous_observer_not_reaped"
        before = runtime.observation_for_session(second.id)
        assert before is not None and before.observer_state == "not_admitted"
        previous = runtime.observation_for_session(first.id)
        if limit == 1:
            assert previous is None
            assert first.id not in runtime._observation_history  # pyright: ignore[reportPrivateUsage]
        else:
            assert previous is not None and previous.durability == "pending"
            assert previous.frozen_summary.value is not None
        assert not owner.reaped.is_set()
    finally:
        release.set()
        assert owner.reaped.wait(2)
    after = runtime.observation_for_session(first.id)
    if limit > 1:
        assert after is not None and after.worker_reaped and after.durability == "persisted"
        entry = runtime._observation_history[first.id]  # pyright: ignore[reportPrivateUsage]
        assert entry.observer is None
    else:
        assert after is None
    assert runtime.observation_for_session(second.id) == before
    assert runtime.snapshot().active_session_id == second.id
    assert first.first_crack_at_utc is None and second.first_crack_at_utc is None


def test_blocked_history_membership_never_participates_in_sampler_or_dispatch(
    tmp_path: Path,
) -> None:
    import threading

    store = RoastSessionStore(default_log_dir=tmp_path / "history-callback")
    entered, release = Event(), Event()
    forbid_main = [False]
    runtime: FirstCrackSessionRuntime

    def retained(session_id: str) -> bool:
        assert not runtime._lock._is_owned()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportUnknownMemberType]
        assert threading.current_thread().name not in {
            "fc-observation-worker",
            "fc-observation-supervisor",
        }
        if threading.current_thread().name == "history-reader":
            entered.set()
            assert release.wait(5)
        else:
            assert not forbid_main[0], "retention callback reached a canonical/sampler path"
        return _retains_session(store, session_id)

    runtime, _, _ = _manual_runtime_fixture(tmp_path, session_is_retained=retained)
    first = _complete_manual_observation(runtime, store)
    second = store.start_session()
    runtime.start_for_session(second)
    owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    reader = threading.Thread(
        target=lambda: runtime.observation_for_session(first.id), name="history-reader"
    )
    reader.start()
    try:
        assert entered.wait(2)
        forbid_main[0] = True
        store.record_event(second, "beans_added")
        runtime.process_available_windows(session_store=store, session=second)
        store.record_event(second, "first_crack_detected")
        runtime.notify_human_mark(second)
        store.record_event(second, "beans_dropped")
        runtime.close_observation(second, reason="beans_dropped")
        runtime.stop_for_session(second.id, reason="synthetic stop")
        runtime.snapshot()
        assert owner.reaped.wait(2)
    finally:
        forbid_main[0] = False
        release.set()
        reader.join(2)
        assert not reader.is_alive()


def test_manual_paced_wav_refuses_before_recorder_model_capture_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    _, _, config = _manual_runtime_fixture(tmp_path)
    config = replace(
        config,
        audio=replace(
            config.audio,
            source="wav",
            replay_mode="detector_paced",
            wav_path=tmp_path / "never-opened.wav",
        ),
    )
    effects: list[str] = []

    def forbidden(*_args: object, **_kwargs: object):
        effects.append("effect")
        raise AssertionError("recording-incompatible replay reached an effect")

    monkeypatch.setattr(runtime_module, "build_session_recorder", forbidden)
    runtime = FirstCrackSessionRuntime(
        config=config,
        audio_pipeline_factory=forbidden,
        detector_adapter_factory=forbidden,
        observation_model_loader=forbidden,
    )
    store = RoastSessionStore(default_log_dir=tmp_path / "paced-refusal")
    for _ in range(2):
        session = store.start_session()
        snapshot = runtime.start_for_session(session)
        assert (
            snapshot.status == "unavailable" and snapshot.reason == "manual_authority_not_admitted"
        )
        assert (
            snapshot.observation is not None
            and snapshot.observation.observer_state == "not_admitted"
        )
        assert not effects
        assert config.audio.replay_mode == "detector_paced"
        assert config.recording.enabled and config.recording.autocapture
        store.stop_session()


@pytest.mark.parametrize("limit", [1, 3])
def test_cleanup_history_scope_and_shutdown_owner_survive_successor_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    store = RoastSessionStore(
        default_log_dir=tmp_path / "cleanup-history", session_history_limit=limit
    )
    runtime, captures, _ = _manual_runtime_fixture(
        tmp_path, session_is_retained=lambda sid: _retains_session(store, sid)
    )
    entered, release = Event(), Event()
    stops: list[str] = []
    closes: list[str] = []
    original_factory = runtime._audio_pipeline_factory  # pyright: ignore[reportPrivateUsage]

    def pipeline_factory(audio: AudioConfig):
        pipeline = original_factory(audio)
        recorder = cast(RoastRecorder | None, cast(Any, captures[-1]).recorder)
        assert recorder is not None
        original_close = recorder.close

        def stop(*, timeout_seconds: float = 1.0):
            stops.append("stop")
            entered.set()
            assert release.wait(5)
            captures[-1].stopped = True
            return pipeline.snapshot()

        def close() -> None:
            closes.append("close")
            original_close()

        monkeypatch.setattr(pipeline, "stop", stop)
        monkeypatch.setattr(recorder, "close", close)
        return pipeline

    def refused_start(self: object, reservation: object | None = None) -> bool:
        return False

    monkeypatch.setattr(runtime, "_audio_pipeline_factory", pipeline_factory)
    monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", refused_start)
    first = store.start_session()
    runtime.start_for_session(first)
    cleanup = runtime._capture_cleanup  # pyright: ignore[reportPrivateUsage]
    disowned = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert cleanup is not None and disowned is not None

    def forbidden_shutdown(*, timeout: float) -> None:
        raise AssertionError("disowned observer tried a second capture stop")

    monkeypatch.setattr(disowned, "shutdown_capture", forbidden_shutdown)
    try:
        assert entered.wait(2)
        store.stop_session()
        second = store.start_session()
        denied = runtime.start_for_session(second)
        before = runtime.observation_for_session(second.id)
        assert denied.reason == "previous_observer_not_reaped"
        assert before is not None and before.observer_state == "not_admitted"
        assert runtime.finalise_for_session(second.id) == ("not_active", None, False)
        runtime._stop_timeout_seconds = 0  # pyright: ignore[reportPrivateUsage] - injected wait budget
        runtime.shutdown()
        assert stops == ["stop"] and closes == []
        assert not cleanup.finished.is_set() and not cleanup.reaped.is_set()
        if limit == 1:
            assert runtime.observation_for_session(first.id) is None
        else:
            previous = runtime.observation_for_session(first.id)
            assert previous is not None and previous.observer_state == "incomplete"
    finally:
        release.set()
        assert cleanup.reaped.wait(2)
    assert stops == ["stop"] and closes == ["close"]
    assert runtime.observation_for_session(second.id) == before
    if limit > 1:
        previous = runtime.observation_for_session(first.id)
        assert previous is not None and previous.worker_reaped
        assert previous.observer_state == "unavailable" and previous.durability == "failed"
        assert runtime._observation_history[first.id].cleanup is None  # pyright: ignore[reportPrivateUsage]


def test_history_lookup_is_memory_only_and_does_not_restore_across_runtime_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RoastSessionStore(default_log_dir=tmp_path / "memory-only")

    def retained(session_id: str) -> bool:
        return _retains_session(store, session_id)

    runtime, _, config = _manual_runtime_fixture(tmp_path, session_is_retained=retained)
    first = _complete_manual_observation(runtime, store)
    _complete_manual_observation(runtime, store)
    expected = runtime.observation_for_session(first.id)
    effects: list[str] = []

    def forbidden(*_args: object, **_kwargs: object):
        effects.append("I/O or model")
        raise AssertionError("historical lookup tried to read files or run a model")

    fresh = FirstCrackSessionRuntime(
        config=config,
        session_is_retained=retained,
        audio_pipeline_factory=forbidden,
        detector_adapter_factory=forbidden,
        observation_model_loader=forbidden,
        observation_sink_factory=forbidden,
    )
    monkeypatch.setattr(runtime, "_observation_model_loader", forbidden)
    monkeypatch.setattr(runtime, "_observation_sink_factory", forbidden)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden)
        historical = runtime.observation_for_session(first.id)
        absent = fresh.observation_for_session(first.id)
    assert historical == expected and expected is not None
    assert absent is None and not effects


@pytest.mark.parametrize("owner_kind", ["observer", "cleanup"])
def test_new_admission_compacts_all_retained_terminal_handles_without_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_kind: str
) -> None:
    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    store = RoastSessionStore(
        default_log_dir=tmp_path / "terminal-history", session_history_limit=3
    )
    entered, release = Event(), Event()
    runtime, _, _ = _manual_runtime_fixture(
        tmp_path,
        stop_barrier=(entered, release) if owner_kind == "cleanup" else None,
        session_is_retained=lambda sid: _retains_session(store, sid),
    )
    original_loader = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]
    original_start = runtime_module.FirstCrackObserver.start

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original_loader(fc)

    def refused_start(self: object, reservation: object | None = None) -> bool:
        return False

    if owner_kind == "observer":
        monkeypatch.setattr(runtime, "_observation_model_loader", blocked)
    else:
        monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", refused_start)
    first = store.start_session()
    runtime.start_for_session(first)
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    cleanup = runtime._capture_cleanup  # pyright: ignore[reportPrivateUsage]
    owner = observer if owner_kind == "observer" else cleanup
    assert owner is not None
    try:
        assert entered.wait(2)
        store.stop_session()
        second = store.start_session()
        assert runtime.start_for_session(second).reason == "previous_observer_not_reaped"
        # Do not call observation_for_session(A): the next admission must
        # compact every retained terminal handle without a historical read.
        entry = runtime._observation_history[first.id]  # pyright: ignore[reportPrivateUsage]
        assert entry.observer is owner if owner_kind == "observer" else entry.cleanup is owner
    finally:
        release.set()
        assert owner.reaped.wait(2)
    if owner_kind == "observer":
        assert observer is not None
        expected = observer.snapshot()
    else:
        expected = entry.snapshot.model_copy(
            update=dict(observer_state="unavailable", worker_reaped=True, durability="failed")
        )
    monkeypatch.setattr(runtime, "_observation_model_loader", original_loader)
    monkeypatch.setattr(runtime_module.FirstCrackObserver, "start", original_start)
    store.stop_session()
    third = store.start_session()
    runtime.start_for_session(third)
    next_owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert next_owner is not None
    try:
        compacted = runtime._observation_history[first.id]  # pyright: ignore[reportPrivateUsage]
        assert compacted.observer is None and compacted.cleanup is None
        assert compacted.snapshot == expected
        assert runtime.snapshot().active_session_id == third.id
    finally:
        runtime.close_observation(third, reason="beans_dropped")
        assert next_owner.reaped.wait(2)


def test_public_manual_emergency_cutoff_uses_this_accepted_event_not_cleanup_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accepted_emergency_clock_witness(
        tmp_path, monkeypatch, trigger="public", expires_before_release=False
    )


def test_sampler_failure_cutoff_uses_this_accepted_event_not_cleanup_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accepted_emergency_clock_witness(
        tmp_path, monkeypatch, trigger="sampler", expires_before_release=False
    )


def test_public_manual_emergency_clock_witness_freezes_expired_unaccepted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired first-signal budget freezes work rather than inventing rows."""
    _accepted_emergency_clock_witness(
        tmp_path, monkeypatch, trigger="public", expires_before_release=True
    )


def test_sampler_failure_clock_witness_freezes_expired_unaccepted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampler faults use the same honest expired-work outcome as public faults."""
    _accepted_emergency_clock_witness(
        tmp_path, monkeypatch, trigger="sampler", expires_before_release=True
    )


def _accepted_emergency_clock_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trigger: str,
    expires_before_release: bool,
) -> None:
    import time
    from datetime import timedelta
    from types import SimpleNamespace

    from coffee_roaster_mcp.first_crack_observation import Record, WindowRecord
    from coffee_roaster_mcp.first_crack_observer import JsonlObservationSink
    from coffee_roaster_mcp.mcp_server import (
        _fault_active_session_after_sampler_failure,  # pyright: ignore[reportPrivateUsage]
        build_server_context,
        create_mcp_server,
    )
    from coffee_roaster_mcp.session import RoastSession

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    base = time.monotonic()
    clock = [base]
    utc_origin = datetime(2026, 11, 8, tzinfo=UTC)
    store = RoastSessionStore(
        default_log_dir=tmp_path / "event-clock",
        monotonic_now=lambda: clock[0],
        utc_now=lambda: utc_origin + timedelta(seconds=clock[0] - base),
    )
    runtime._monotonic_now = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
    entered, release = Event(), Event()
    original_loader = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]
    rows: list[WindowRecord] = []

    class EvidenceSink(JsonlObservationSink):
        def append(self, record: Record) -> None:
            super().append(record)
            if isinstance(record, WindowRecord):
                rows.append(record)

    def loader(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        loaded = original_loader(fc)

        def negative(window: AudioWindow) -> FirstCrackDetectorOutput:
            return FirstCrackDetectorOutput(confirmed=False, confidence=0.2)

        loaded.adapter.backend.detect = negative
        return loaded

    monkeypatch.setattr(runtime, "_observation_model_loader", loader)
    monkeypatch.setattr(runtime, "_observation_sink_factory", EvidenceSink)
    path = tmp_path / "context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'context-logs'}\n", encoding="utf-8")
    context = build_server_context(config_path=path)
    object.__setattr__(context, "config", config)
    object.__setattr__(context, "session_store", store)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str, **arguments: object):
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    def emergency_event(*, repeated: bool = False):
        if trigger == "public":
            return call(
                "emergency_stop",
                reason="synthetic clock witness",
                **({"expected_session_id": session_id} if repeated else {}),
            ).event
        active = store.get_latest_session()
        assert active is not None
        _fault_active_session_after_sampler_failure(
            context, session=active, error=RuntimeError("synthetic sampler witness")
        )
        return store.get_session_snapshot(session_id=active.id).event_timeline[-1]

    started = call("start_roast_session", first_crack_observer_ack="manual/v1")
    session_id = started.session.session_id
    context.telemetry_sampler.stop_for_session(session_id, reason="controlled synthetic clock")
    owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    trace: list[str] = []
    cutoffs: list[float | None] = []
    original_emergency = context.roaster_driver.emergency_stop
    original_event = store.emergency_stop_snapshot
    original_resolve = runtime.resolve_observation_boundary

    def emergency(*, reason: str):
        result = original_emergency(reason=reason)
        trace.append("driver")
        return result

    def accepted(*args: Any, **kwargs: Any):
        result = original_event(*args, **kwargs)
        trace.append("accepted_event")
        # The closing budget starts at the first driver-return boundary.  This
        # deliberately tests both a later cleanup clock still within that
        # original budget and a genuinely expired budget; it never stretches
        # or disables the deadline.
        clock[0] += 10 if expires_before_release else 1
        return result

    def resolve(
        session: RoastSession, ticket: object | None, *, cutoff_monotonic_seconds: float
    ) -> None:
        assert trace[-1] == "accepted_event"
        cutoffs.append(cutoff_monotonic_seconds)
        trace.append("resolve")
        return original_resolve(session, ticket, cutoff_monotonic_seconds=cutoff_monotonic_seconds)

    monkeypatch.setattr(context.roaster_driver, "emergency_stop", emergency)
    monkeypatch.setattr(store, "emergency_stop_snapshot", accepted)
    monkeypatch.setattr(runtime, "resolve_observation_boundary", resolve)
    try:
        assert entered.wait(2)
        clock[0] = base + 1
        call("mark_beans_added")
        captures[0].add_window(
            _audio_window(sequence_number=1, started_at_monotonic_seconds=base + 8)
        )
        captures[0].add_window(
            _audio_window(sequence_number=2, started_at_monotonic_seconds=base + 9)
        )
        captures[0].add_window(
            _audio_window(sequence_number=3, started_at_monotonic_seconds=base + 10)
        )
        clock[0] = base + 10
        first = emergency_event()
        assert first.monotonic_seconds == 10
        assert clock[0] == base + (20 if expires_before_release else 11)
        assert cutoffs == [base + 10]
        assert owner.cutoff == base + 10
    finally:
        release.set()
        assert owner.reaped.wait(2)
    if expires_before_release:
        # The model was still unaccepted at the real first-signal deadline:
        # this is incomplete coverage, not a claim that the queued windows
        # were classified.
        assert rows == []
        assert owner.summary is not None
        assert owner.summary.coverage_complete is False
    else:
        assert [row.payload.eligibility for row in rows] == [
            "eligible",
            "ineligible_post_drop",
            "ineligible_post_drop",
        ]
        assert all(row.payload.availability_class == "after_drop" for row in rows)
    assert owner.confirmation is None
    before = store.get_session_snapshot(session_id=session_id)
    assert before.first_crack_at_utc is None
    assert [event.kind for event in before.event_timeline] == ["beans_added", "fault"]
    clock[0] = base + 30
    second = emergency_event(repeated=True)
    assert second.monotonic_seconds == 30
    assert cutoffs == [base + 10, base + 30]
    # Repeated command supplies its own accepted event; the first frozen
    # observation cutoff and original canonical fault time remain immutable.
    assert owner.cutoff == base + 10
    after = store.get_session_snapshot(session_id=session_id)
    assert after.faulted_monotonic_seconds == 10
    assert [event.kind for event in after.event_timeline] == ["beans_added", "fault", "fault"]
    assert trace == ["driver", "accepted_event", "resolve"] * 2


def test_public_drop_pending_boundary_fences_post_accepted_positive_until_store_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real public drop fences a positive window before its Store return."""
    import threading
    import time
    from datetime import timedelta
    from types import SimpleNamespace

    from coffee_roaster_mcp.first_crack_observation import IdentityRecord, Record, WindowRecord
    from coffee_roaster_mcp.first_crack_observer import JsonlObservationSink
    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    base = time.monotonic()
    clock = [base]
    store = RoastSessionStore(
        default_log_dir=tmp_path / "public-drop-race",
        monotonic_now=lambda: clock[0],
        utc_now=lambda: datetime(2026, 11, 8, tzinfo=UTC) + timedelta(seconds=clock[0] - base),
    )
    runtime._monotonic_now = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
    model_entered, model_release, identity_ready = Event(), Event(), Event()
    accepted_return, release_return = Event(), Event()
    queued, two_worker_cycles = Event(), Event()
    rows: list[WindowRecord] = []
    original_loader = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

    class EvidenceSink(JsonlObservationSink):
        def append(self, record: Record) -> None:
            super().append(record)
            if isinstance(record, IdentityRecord):
                identity_ready.set()
            if isinstance(record, WindowRecord):
                rows.append(record)

    def loader(fc: FirstCrackConfig):
        model_entered.set()
        assert model_release.wait(5)
        loaded = original_loader(fc)
        loaded.adapter.backend.detect = lambda window: FirstCrackDetectorOutput(
            confirmed=True, confidence=0.95
        )
        return loaded

    monkeypatch.setattr(runtime, "_observation_model_loader", loader)
    monkeypatch.setattr(runtime, "_observation_sink_factory", EvidenceSink)
    path = tmp_path / "public-drop-context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'context-logs'}\n", encoding="utf-8")
    context = build_server_context(config_path=path)
    object.__setattr__(context, "config", config)
    object.__setattr__(context, "session_store", store)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))
    driver_calls: list[str] = []
    trace: list[str] = []
    original_drop = context.roaster_driver.drop_beans
    original_begin = runtime.begin_observation_boundary
    original_resolve = runtime.resolve_observation_boundary

    def dispatch_drop():
        driver_calls.append("driver")
        trace.append("driver")
        return original_drop()

    def begin(*args: Any, **kwargs: Any) -> object | None:
        assert trace == ["driver"]
        trace.append("begin")
        return original_begin(*args, **kwargs)

    def resolve(*args: Any, **kwargs: Any) -> None:
        trace.append("resolve")
        original_resolve(*args, **kwargs)

    monkeypatch.setattr(context.roaster_driver, "drop_beans", dispatch_drop)
    monkeypatch.setattr(runtime, "begin_observation_boundary", begin)
    monkeypatch.setattr(runtime, "resolve_observation_boundary", resolve)

    def call(name: str, **arguments: object):
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    started = call("start_roast_session", first_crack_observer_ack="manual/v1")
    session_id = started.session.session_id
    session = context.session_store.get_latest_session()
    assert session is not None
    context.telemetry_sampler.stop_for_session(session_id, reason="controlled synthetic clock")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None
    drop_thread: threading.Thread | None = None
    try:
        assert model_entered.wait(2)
        model_release.set()
        assert identity_ready.wait(2)
        clock[0] = base + 1
        call("mark_beans_added")

        original_complete = store.complete_reserved_driver_drop_snapshot

        def held_complete(*args: Any, **kwargs: Any):
            completed = original_complete(*args, **kwargs)
            trace.append("accepted_event")
            accepted_return.set()
            assert release_return.wait(5)
            return completed

        monkeypatch.setattr(store, "complete_reserved_driver_drop_snapshot", held_complete)
        original_wait = observer.changed.wait
        cycles = [0]

        def watched_wait(timeout: float | None = None) -> bool:
            result = original_wait(timeout)
            if queued.is_set():
                cycles[0] += 1
                if cycles[0] >= 2:
                    two_worker_cycles.set()
            return result

        monkeypatch.setattr(observer.changed, "wait", watched_wait)
        clock[0] = base + 10
        result: list[object] = []
        drop_thread = threading.Thread(target=lambda: result.append(call("drop_beans")))
        drop_thread.start()
        assert accepted_return.wait(2)
        captures[0].add_window(
            _audio_window(sequence_number=237, started_at_monotonic_seconds=base + 10)
        )
        queued.set()
        # The current clock is strictly later than the post-cutoff window end
        # while remaining inside the original closing budget.  The resolver
        # must retain the accepted event scalar (10), never resample this 12.
        clock[0] = base + 12
        observer.changed.set()
        # This is a positive causal guard, including an already-sampled empty
        # worker iteration.  A missing fence confirms before this point.
        assert two_worker_cycles.wait(2)
        assert rows == [] and observer.confirmation is None
        assert store.get_session_snapshot(session_id=session_id).first_crack_at_utc is None

        release_return.set()
        drop_thread.join(2)
        assert not drop_thread.is_alive() and len(result) == 1
        assert observer.reaped.wait(2)
    finally:
        release_return.set()
        model_release.set()
        observer.changed.set()
        if drop_thread is not None:
            drop_thread.join(2)
        if not observer.reaped.is_set():
            runtime.close_observation(session, reason="diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)

    assert [row.payload.eligibility for row in rows] == ["ineligible_post_drop"]
    assert rows[0].payload.availability_class == "after_drop"
    snapshot = store.get_session_snapshot(session_id=session_id)
    assert snapshot.first_crack_at_utc is None
    assert [event.kind for event in snapshot.event_timeline] == [
        "beans_added",
        "beans_dropped",
        "cooling_started",
    ]
    assert snapshot.beans_dropped_monotonic_seconds == 10
    assert driver_calls == ["driver"]
    assert trace == ["driver", "begin", "accepted_event", "resolve"]
    assert observer.cutoff == base + 10
    assert observer.confirmation is None
    assert captures[0].started and captures[0].stopped


def _public_boundary_matrix_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority: str,
    negative_backend: bool = False,
) -> tuple[Any, Any, Any, Any, str, Any | None, Event | None]:
    """Build an actual public context with only the admitted test seams."""
    from types import SimpleNamespace

    from coffee_roaster_mcp.first_crack_observation import IdentityRecord, Record, WindowRecord
    from coffee_roaster_mcp.first_crack_observer import JsonlObservationSink
    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    path = tmp_path / f"{authority}-public-boundary-context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'context-logs'}\n", encoding="utf-8")
    identity_ready: Event | None = None
    if authority == "manual":
        runtime, _captures, config = _manual_runtime_fixture(tmp_path)
        identity_ready = Event()
        window_recorded = Event()

        if negative_backend:
            original_loader = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

            def loader(first_crack: FirstCrackConfig) -> Any:
                loaded = original_loader(first_crack)
                loaded.adapter.backend.detect = lambda window: FirstCrackDetectorOutput(
                    confirmed=False, confidence=0.1
                )
                return loaded

            runtime._observation_model_loader = loader  # pyright: ignore[reportPrivateUsage]

        class ReadySink(JsonlObservationSink):
            def append(self, record: Record) -> None:
                super().append(record)
                if isinstance(record, IdentityRecord):
                    identity_ready.set()
                if isinstance(record, WindowRecord):
                    window_recorded.set()

        runtime._observation_sink_factory = ReadySink  # pyright: ignore[reportPrivateUsage]
        context = build_server_context(config_path=path)
        object.__setattr__(context, "config", config)
        object.__setattr__(context, "first_crack_runtime", runtime)
    else:
        # The real legacy default is disabled and receives no observer ack.
        context = build_server_context(config_path=path)
        runtime = context.first_crack_runtime
        window_recorded = None
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str, **arguments: object) -> Any:
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    started = call(
        "start_roast_session",
        **({"first_crack_observer_ack": "manual/v1"} if authority == "manual" else {}),
    )
    session_id = started.session.session_id
    context.telemetry_sampler.stop_for_session(session_id, reason="controlled matrix")
    call("mark_beans_added")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage] - public lifecycle witness
    if authority == "manual":
        assert identity_ready is not None
        assert identity_ready.wait(2)
    return context, runtime, call, started, session_id, observer, window_recorded


def _install_public_boundary_trace(
    monkeypatch: pytest.MonkeyPatch, context: Any, runtime: Any
) -> list[tuple[str, object]]:
    """Trace actual dispatch, boundary, accepted-event, and resolution calls."""
    trace: list[tuple[str, object]] = []
    for name in ("drop_beans", "emergency_stop"):
        original = getattr(context.roaster_driver, name)

        def driver(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            result = _original(*args, **kwargs)
            trace.append(("driver", _name))
            return result

        monkeypatch.setattr(context.roaster_driver, name, driver)

    for name in (
        "complete_reserved_driver_drop_snapshot",
        "complete_reserved_driver_fault_recovery_snapshot",
        "emergency_stop_snapshot",
    ):
        original = getattr(context.session_store, name)

        def accepted(
            *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            result = _original(*args, **kwargs)
            trace.append(("accepted", (_name, result[0])))
            return result

        monkeypatch.setattr(context.session_store, name, accepted)

    original_begin = runtime.begin_observation_boundary
    original_resolve = runtime.resolve_observation_boundary
    original_freeze = runtime.freeze_observation_boundary

    def begin(*args: Any, **kwargs: Any) -> object | None:
        ticket = original_begin(*args, **kwargs)
        trace.append(("begin", (kwargs["reason"], ticket)))
        return ticket

    def resolve(*args: Any, **kwargs: Any) -> None:
        trace.append(("resolve", (args[1], kwargs["cutoff_monotonic_seconds"])))
        original_resolve(*args, **kwargs)

    def freeze(*args: Any, **kwargs: Any) -> None:
        trace.append(("freeze", (args[1], kwargs["reason"])))
        original_freeze(*args, **kwargs)

    monkeypatch.setattr(runtime, "begin_observation_boundary", begin)
    monkeypatch.setattr(runtime, "resolve_observation_boundary", resolve)
    monkeypatch.setattr(runtime, "freeze_observation_boundary", freeze)
    return trace


def _assert_manual_public_boundary_trace(
    trace: list[tuple[str, object]], *, reason: str, session: Any, target: Any
) -> None:
    """Check the ticket and accepted-event scalar stay with one dispatch."""
    assert 4 <= len(trace) <= 5
    assert [entry[0] for entry in trace[:4]] == ["driver", "begin", "accepted", "resolve"]
    begin_reason, ticket = cast(tuple[str, object | None], trace[1][1])
    _method, event = cast(tuple[str, Any], trace[2][1])
    resolved_ticket, cutoff = cast(tuple[object | None, float], trace[3][1])
    assert begin_reason == reason
    assert ticket is not None
    assert resolved_ticket is ticket
    assert cutoff == session.monotonic_start + event.monotonic_seconds
    accepted_event = event.event if hasattr(event, "event") else event
    returned_event = target.event if hasattr(target, "event") else target
    assert accepted_event.kind == returned_event.kind
    assert accepted_event.monotonic_seconds == returned_event.monotonic_seconds
    accepted_utc = accepted_event.recorded_at_utc
    returned_utc = returned_event.recorded_at_utc
    assert (accepted_utc.isoformat() if hasattr(accepted_utc, "isoformat") else accepted_utc) == (
        returned_utc.isoformat() if hasattr(returned_utc, "isoformat") else returned_utc
    )
    assert accepted_event.payload == returned_event.payload
    if len(trace) == 5:
        frozen_ticket, frozen_reason = cast(tuple[object | None, str], trace[4][1])
        assert trace[4][0] == "freeze"
        assert frozen_ticket is ticket and frozen_reason == "emergency_stop"


@pytest.mark.parametrize(
    "path", ["ordinary_drop", "stopped_fault_drop", "public_emergency", "sampler_failure"]
)
@pytest.mark.parametrize("authority", ["manual", "automatic"])
def test_public_observation_boundary_dispatch_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, authority: str
) -> None:
    """Each public dispatch keeps manual boundaries causal; automatic stays legacy-disabled."""
    from coffee_roaster_mcp import mcp_server

    context, runtime, call, _started, session_id, observer, _window_recorded = (
        _public_boundary_matrix_context(tmp_path, monkeypatch, authority=authority)
    )
    trace = _install_public_boundary_trace(monkeypatch, context, runtime)
    session = context.session_store.get_session_snapshot(session_id=session_id)
    frozen_summary: str | None = None
    try:
        if path == "stopped_fault_drop":
            setup = call("emergency_stop", reason="matrix stopped-fault setup")
            assert setup.event.kind == "fault"
            if observer is not None:
                assert observer.reaped.wait(2)
                frozen_summary = observer.summary.model_dump_json()
            trace.clear()
            result = call("drop_beans", expected_session_id=session_id)
            expected_reason = "beans_dropped"
        elif path == "ordinary_drop":
            result = call("drop_beans")
            expected_reason = "beans_dropped"
        elif path == "public_emergency":
            result = call("emergency_stop", reason="matrix public emergency")
            expected_reason = "emergency_stop"
        else:
            active = context.session_store.get_active_session()
            assert active is not None
            sampler_failure = getattr(  # noqa: B009 - invokes the registered private callback.
                mcp_server, "_fault_active_session_after_sampler_failure"
            )
            sampler_failure(context, session=active, error=RuntimeError("matrix sampler callback"))
            result = context.session_store.get_session_snapshot(
                session_id=session_id
            ).event_timeline[-1]
            expected_reason = "autonomous_telemetry_sampler_failure"

        event = result.event if hasattr(result, "event") else result
        assert event is not None
        snapshot = context.session_store.get_session_snapshot(session_id=session_id)
        assert snapshot.first_crack_at_utc is None
        if authority == "manual":
            _assert_manual_public_boundary_trace(
                trace, reason=expected_reason, session=session, target=event
            )
            if path == "stopped_fault_drop":
                assert frozen_summary is not None
                assert observer is not None and observer.summary.model_dump_json() == frozen_summary
        else:
            assert not any(name in {"begin", "resolve"} for name, _value in trace)
            assert [name for name, _value in trace] == ["driver", "accepted"]
    finally:
        if observer is not None and not observer.reaped.is_set():
            runtime.close_observation(session, reason="matrix diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("public_emergency", "public coverage fault"),
        ("sampler_failure", "sampler coverage fault"),
    ],
)
def test_public_fault_first_boundary_marks_processed_negative_prefix_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, reason: str
) -> None:
    """Public fault paths retain a negative prefix but never call it complete."""
    from coffee_roaster_mcp import mcp_server

    context, runtime, call, _started, session_id, observer, window_recorded = (
        _public_boundary_matrix_context(
            tmp_path, monkeypatch, authority="manual", negative_backend=True
        )
    )
    assert observer is not None and window_recorded is not None
    session = context.session_store.get_session_snapshot(session_id=session_id)
    charge = session.event_timeline[-1]
    observer.pipeline.add_window(  # pyright: ignore[reportPrivateUsage]
        AudioWindow(
            991,
            None,
            16000,
            session.monotonic_start + charge.monotonic_seconds + 0.001,
            0.001,
            (0.0,) * 16,
        )
    )
    observer.changed.set()
    try:
        assert window_recorded.wait(2), observer.snapshot()
        if path == "public_emergency":
            result = call("emergency_stop", reason=reason)
            assert result.event.kind == "fault"
        else:
            active = context.session_store.get_active_session()
            assert active is not None
            mcp_server._fault_active_session_after_sampler_failure(  # pyright: ignore[reportPrivateUsage]
                context, session=active, error=RuntimeError(reason)
            )
        assert observer.reaped.wait(2)
    finally:
        if not observer.reaped.is_set():
            runtime.close_observation(session, reason="coverage diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)

    assert observer.summary is not None
    assert observer.summary.counts.eligible_windows == 1
    assert not observer.summary.coverage_complete
    assert observer.summary.outcome == "incomplete"


@pytest.mark.parametrize(
    "path", ["ordinary_retry", "stopped_fault_start_cooling", "stopped_fault_stop_cooling"]
)
def test_public_drop_retry_and_fault_cooling_do_not_open_new_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Retries and cooling recovery retain their events without a new drop boundary."""
    context, runtime, call, _started, session_id, observer, _window_recorded = (
        _public_boundary_matrix_context(tmp_path, monkeypatch, authority="manual")
    )
    trace = _install_public_boundary_trace(monkeypatch, context, runtime)
    session = context.session_store.get_session_snapshot(session_id=session_id)
    try:
        if path == "ordinary_retry":
            first = call("drop_beans")
            trace.clear()
            result = call("drop_beans")
            assert result.event == first.event
            assert [name for name, _value in trace] == []
        else:
            setup = call("emergency_stop", reason="matrix cooling setup")
            assert setup.event.kind == "fault"
            assert observer is not None and observer.reaped.wait(2)
            trace.clear()
            if path == "stopped_fault_stop_cooling":
                recovered_drop = call("drop_beans", expected_session_id=session_id)
                assert recovered_drop.event is not None
                started_cooling = call("start_cooling", expected_session_id=session_id)
                assert started_cooling.event is not None
                trace.clear()
            result = call(
                "start_cooling" if path == "stopped_fault_start_cooling" else "stop_cooling",
                expected_session_id=session_id,
            )
            assert result.event is not None
            assert [name for name, _value in trace] == ["accepted"]
        snapshot = context.session_store.get_session_snapshot(session_id=session_id)
        assert snapshot.first_crack_at_utc is None
        assert not any(name in {"begin", "resolve"} for name, _value in trace)
    finally:
        if observer is not None and not observer.reaped.is_set():
            runtime.close_observation(session, reason="matrix diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)


@pytest.mark.parametrize(
    "path", ["ordinary_drop", "stopped_fault_drop", "public_emergency", "sampler_failure"]
)
def test_manual_boundary_raised_dispatch_has_no_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A dispatch which raises before returning never opens a manual boundary."""
    import coffee_roaster_mcp.mcp_server as mcp_server

    context, runtime, call, _started, session_id, observer, _window_recorded = (
        _public_boundary_matrix_context(tmp_path, monkeypatch, authority="manual")
    )
    trace = _install_public_boundary_trace(monkeypatch, context, runtime)
    session = context.session_store.get_session_snapshot(session_id=session_id)
    try:
        if path == "stopped_fault_drop":
            assert call("emergency_stop", reason="raised dispatch setup").event.kind == "fault"
            assert observer is not None and observer.reaped.wait(2)
            trace.clear()

        if path in {"ordinary_drop", "stopped_fault_drop"}:

            def raised_drop() -> Any:
                raise RuntimeError("synthetic drop before return")

            monkeypatch.setattr(context.roaster_driver, "drop_beans", raised_drop)
            with pytest.raises(RuntimeError, match="synthetic drop before return"):
                call(
                    "drop_beans",
                    **({"expected_session_id": session_id} if path == "stopped_fault_drop" else {}),
                )
        else:

            def raised_dispatch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("synthetic emergency before return")

            monkeypatch.setattr(mcp_server, "run_driver_emergency_stop", raised_dispatch)
            if path == "public_emergency":
                with pytest.raises(RuntimeError, match="synthetic emergency before return"):
                    call("emergency_stop", reason="raised public dispatch")
            else:
                active = context.session_store.get_active_session()
                assert active is not None
                with pytest.raises(RuntimeError, match="synthetic emergency before return"):
                    getattr(  # noqa: B009 - invokes the registered private callback.
                        mcp_server, "_fault_active_session_after_sampler_failure"
                    )(context, session=active, error=RuntimeError("synthetic sampler callback"))

        assert not any(name in {"begin", "resolve"} for name, _ in trace)
        assert all(
            ticket is None
            for name, value in trace
            if name == "freeze"
            for ticket, _reason in [cast(tuple[object | None, str], value)]
        )
        snapshot = context.session_store.get_session_snapshot(session_id=session_id)
        assert snapshot.first_crack_at_utc is None
        if path == "ordinary_drop":
            assert snapshot.beans_dropped_at_utc is None
        if path == "stopped_fault_drop":
            assert snapshot.phase == "fault"
    finally:
        if observer is not None and not observer.reaped.is_set():
            runtime.close_observation(session, reason="raised dispatch diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)


@pytest.mark.parametrize(
    "path",
    [
        "ordinary_drop",
        "public_emergency",
        "sampler_failure",
        "stopped_fault_drop",
        "returned_failed_containment",
    ],
)
def test_manual_boundary_failed_commit_and_failed_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Returned dispatches keep their own ticket through failure handling."""
    import coffee_roaster_mcp.mcp_server as mcp_server
    from coffee_roaster_mcp.session import default_emergency_safety_payload

    context, runtime, call, _started, session_id, observer, _window_recorded = (
        _public_boundary_matrix_context(tmp_path, monkeypatch, authority="manual")
    )
    trace = _install_public_boundary_trace(monkeypatch, context, runtime)
    session = context.session_store.get_session_snapshot(session_id=session_id)
    ambient_stops: list[str] = []
    original_ambient_stop = context.ambient_runtime.stop_for_session

    def ambient_stop(*args: Any, **kwargs: Any) -> Any:
        ambient_stops.append(str(kwargs["reason"]))
        return original_ambient_stop(*args, **kwargs)

    monkeypatch.setattr(context.ambient_runtime, "stop_for_session", ambient_stop)
    try:
        if path == "stopped_fault_drop":
            assert call("emergency_stop", reason="failed commit setup").event.kind == "fault"
            assert observer is not None and observer.reaped.wait(2)
            frozen_before = observer.summary.model_dump_json()
            trace.clear()

            def failed_recovery(*_args: Any, **_kwargs: Any) -> Any:
                raise SessionLifecycleError("synthetic recovery persistence failure")

            monkeypatch.setattr(
                context.session_store,
                "complete_reserved_driver_fault_recovery_snapshot",
                failed_recovery,
            )
            with pytest.raises(
                SessionLifecycleError, match="synthetic recovery persistence failure"
            ):
                call("drop_beans", expected_session_id=session_id)
            assert observer.summary.model_dump_json() == frozen_before
            expected_freeze = "fault_recovery_incomplete"
        elif path == "ordinary_drop":

            def failed_drop(*_args: Any, **_kwargs: Any) -> Any:
                raise SessionLifecycleError("synthetic drop persistence failure")

            monkeypatch.setattr(
                context.session_store, "complete_reserved_driver_drop_snapshot", failed_drop
            )
            with pytest.raises(SessionLifecycleError, match="synthetic drop persistence failure"):
                call("drop_beans")
            expected_freeze = "drop_recording_failed"
        elif path in {"public_emergency", "sampler_failure"}:

            def failed_fault(*_args: Any, **_kwargs: Any) -> Any:
                raise SessionLifecycleError("synthetic fault persistence failure")

            monkeypatch.setattr(context.session_store, "emergency_stop_snapshot", failed_fault)
            if path == "public_emergency":
                with pytest.raises(SessionLifecycleError, match="fault recording failed"):
                    call("emergency_stop", reason="failed public persistence")
            else:
                active = context.session_store.get_active_session()
                assert active is not None
                getattr(  # noqa: B009 - invokes the registered private callback.
                    mcp_server, "_fault_active_session_after_sampler_failure"
                )(context, session=active, error=RuntimeError("synthetic sampler callback"))
                assert ambient_stops == []
            expected_freeze = (
                "emergency_stop" if path == "public_emergency" else "sampler_fault_recording_failed"
            )
        else:

            def failed_containment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                trace.append(("driver", "emergency_stop_returned_failed_containment"))
                return default_emergency_safety_payload(
                    driver="mock", driver_error="synthetic containment"
                )

            monkeypatch.setattr(mcp_server, "run_driver_emergency_stop", failed_containment)
            with pytest.raises(SessionLifecycleError, match="did not report verified"):
                call("emergency_stop", reason="returned failed containment")
            expected_freeze = "emergency_stop"

        names = [name for name, _ in trace]
        assert names.index("driver") < names.index("begin")
        begin_reason, ticket = cast(tuple[str, object | None], trace[names.index("begin")][1])
        assert ticket is not None
        assert begin_reason in {
            "beans_dropped",
            "emergency_stop",
            "autonomous_telemetry_sampler_failure",
        }
        if path == "returned_failed_containment":
            assert "accepted" in names and "resolve" in names
            _method, accepted_event = cast(tuple[str, Any], trace[names.index("accepted")][1])
            resolved_ticket, cutoff = cast(
                tuple[object | None, float], trace[names.index("resolve")][1]
            )
            assert accepted_event.kind == "fault"
            assert resolved_ticket is ticket
            assert cutoff == session.monotonic_start + accepted_event.monotonic_seconds
        else:
            assert "resolve" not in names
            frozen_ticket, freeze_reason = cast(tuple[object | None, str], trace[-1][1])
            assert trace[-1][0] == "freeze"
            assert frozen_ticket is ticket and freeze_reason == expected_freeze
            if path != "stopped_fault_drop":
                assert observer is not None
                runtime.close_observation(session, reason="failed commit lifecycle cleanup")
                observer.changed.set()
                assert observer.reaped.wait(2)
                assert observer.summary is not None and observer.summary.drop_cutoff.value is None
                assert observer.summary.coverage_complete is False
        snapshot = context.session_store.get_session_snapshot(session_id=session_id)
        assert snapshot.first_crack_at_utc is None
    finally:
        if observer is not None and not observer.reaped.is_set():
            runtime.close_observation(session, reason="failed commit diagnostic cleanup")
            observer.changed.set()
            assert observer.reaped.wait(2)


def test_public_drop_pending_timeout_keeps_owned_result_cutoff_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public drop timeout does not count an owned result against an unknown cutoff."""
    import threading
    import time
    from datetime import timedelta
    from types import SimpleNamespace

    from coffee_roaster_mcp.first_crack_observer import coordinator
    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    base = time.monotonic()
    clock = [base]
    store = RoastSessionStore(
        default_log_dir=tmp_path / "public-drop-timeout",
        monotonic_now=lambda: clock[0],
        utc_now=lambda: datetime(2026, 11, 8, tzinfo=UTC) + timedelta(seconds=clock[0] - base),
    )
    runtime._monotonic_now = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
    detect_entered, detect_release = Event(), Event()
    accepted_return, release_return, supervisor_checked = Event(), Event(), Event()
    original_loader = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

    def loader(fc: FirstCrackConfig):
        loaded = original_loader(fc)

        def blocked_positive(window: AudioWindow) -> FirstCrackDetectorOutput:
            detect_entered.set()
            assert detect_release.wait(5)
            return FirstCrackDetectorOutput(confirmed=True, confidence=0.95)

        loaded.adapter.backend.detect = blocked_positive
        return loaded

    monkeypatch.setattr(runtime, "_observation_model_loader", loader)
    path = tmp_path / "public-drop-timeout-context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'context-logs'}\n", encoding="utf-8")
    context = build_server_context(config_path=path)
    object.__setattr__(context, "config", config)
    object.__setattr__(context, "session_store", store)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str, **arguments: object):
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    started = call("start_roast_session", first_crack_observer_ack="manual/v1")
    session_id = started.session.session_id
    context.telemetry_sampler.stop_for_session(session_id, reason="controlled synthetic clock")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None
    drop_thread: threading.Thread | None = None
    try:
        clock[0] = base + 1
        call("mark_beans_added")
        clock[0] = base + 7
        captures[0].add_window(
            _audio_window(sequence_number=238, started_at_monotonic_seconds=base + 5)
        )
        observer.changed.set()
        assert detect_entered.wait(2)

        original_complete = store.complete_reserved_driver_drop_snapshot

        def held_complete(*args: Any, **kwargs: Any):
            completed = original_complete(*args, **kwargs)
            accepted_return.set()
            assert release_return.wait(5)
            return completed

        monkeypatch.setattr(store, "complete_reserved_driver_drop_snapshot", held_complete)
        original_check = observer.check_deadline

        def watched_check() -> None:
            original_check()
            if threading.current_thread().name == "fc-observation-supervisor":
                supervisor_checked.set()

        monkeypatch.setattr(observer, "check_deadline", watched_check)
        clock[0] = base + 10
        drop_thread = threading.Thread(target=lambda: call("drop_beans"))
        drop_thread.start()
        assert accepted_return.wait(2)
        # Advance to the original first-signal deadline itself, not a value
        # recomputed after the accepted event or held return.
        assert observer.deadline is not None
        original_deadline = observer.deadline
        clock[0] = original_deadline
        supervisor_checked.clear()
        coordinator().wake.set()
        assert supervisor_checked.wait(2)
        assert observer.summary is not None
        frozen = observer.summary.model_dump_json()
        assert observer.summary.drop_cutoff.value is None
        assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
        assert observer.summary.coverage_complete is False
        assert observer.summary.counts.undecided_eligible_windows == 0

        release_return.set()
        drop_thread.join(2)
        assert not drop_thread.is_alive()
        # A late accepted event and late model result cannot rewrite the
        # already frozen unknown-boundary summary.
        detect_release.set()
        assert observer.reaped.wait(2)
        assert observer.summary.model_dump_json() == frozen
    finally:
        release_return.set()
        detect_release.set()
        observer.changed.set()
        if drop_thread is not None:
            drop_thread.join(2)
        if not observer.reaped.is_set():
            assert observer.reaped.wait(2)
    assert store.get_session_snapshot(session_id=session_id).first_crack_at_utc is None


def test_manual_public_generic_drop_commit_failure_freezes_at_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain post-driver drop failure leaves the original boundary deadline authoritative."""
    import threading
    import time
    from datetime import timedelta
    from types import SimpleNamespace

    from coffee_roaster_mcp.first_crack_observer import coordinator
    from coffee_roaster_mcp.mcp_server import build_server_context, create_mcp_server

    runtime, captures, config = _manual_runtime_fixture(tmp_path)
    base = time.monotonic()
    clock = [base]
    store = RoastSessionStore(
        default_log_dir=tmp_path / "generic-drop",
        monotonic_now=lambda: clock[0],
        utc_now=lambda: datetime(2026, 11, 8, tzinfo=UTC) + timedelta(seconds=clock[0] - base),
    )
    runtime._monotonic_now = lambda: clock[0]  # pyright: ignore[reportPrivateUsage]
    path = tmp_path / "generic-drop-context.yaml"
    path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    context = build_server_context(config_path=path)
    object.__setattr__(context, "config", config)
    object.__setattr__(context, "session_store", store)
    object.__setattr__(context, "first_crack_runtime", runtime)
    server = create_mcp_server(config_path=path)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))

    def call(name: str, **arguments: object) -> Any:
        tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        return tool.fn(ctx, **arguments)

    started = call("start_roast_session", first_crack_observer_ack="manual/v1")
    session_id = started.session.session_id
    context.telemetry_sampler.stop_for_session(session_id, reason="synthetic clock")
    clock[0] = base + 1
    call("mark_beans_added")
    observer = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert observer is not None and len(captures) == 1
    driver_returned = Event()
    original_drop = context.roaster_driver.drop_beans

    def drop() -> Any:
        result = original_drop()
        driver_returned.set()
        return result

    monkeypatch.setattr(context.roaster_driver, "drop_beans", drop)

    def failed_commit(*_args: Any, **_kwargs: Any) -> Any:
        assert driver_returned.is_set()
        raise RuntimeError("synthetic ordinary drop commit failure")

    monkeypatch.setattr(store, "complete_reserved_driver_drop_snapshot", failed_commit)
    clock[0] = base + 10
    with pytest.raises(RuntimeError, match="synthetic ordinary drop commit failure"):
        call("drop_beans")
    assert observer.deadline is not None
    original_deadline = observer.deadline
    assert original_deadline == base + 10 + config.first_crack.observation_closing_timeout_seconds
    checked = Event()
    original_check = observer.check_deadline

    def watched_check() -> None:
        original_check()
        if threading.current_thread().name == "fc-observation-supervisor":
            checked.set()

    monkeypatch.setattr(observer, "check_deadline", watched_check)
    clock[0] = original_deadline
    coordinator().wake.set()
    assert checked.wait(2)
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert observer.summary.outcome in {"unavailable", "incomplete"}
    assert store.get_session_snapshot(session_id=session_id).first_crack_at_utc is None
    observer.changed.set()
    assert observer.reaped.wait(2) and captures[0].stopped


def _assert_empty_successor_capture(snapshot: FirstCrackRuntimeSnapshot) -> None:
    assert not snapshot.active and not snapshot.audio_running
    assert (
        snapshot.queued_window_count
        == snapshot.emitted_window_count
        == snapshot.dropped_window_count
        == 0
    )
    assert snapshot.processed_window_count == snapshot.inference_overrun_count == 0
    assert snapshot.last_inference_duration_ms == snapshot.max_inference_duration_ms == 0
    assert snapshot.overflow_count_last_minute == snapshot.total_overflow_count == 0
    assert snapshot.estimated_lost_audio_ms_last_minute == 0
    assert snapshot.max_consecutive_overflow_count == 0
    assert snapshot.mic_peak_dbfs is None and snapshot.mic_rms_dbfs is None
    assert snapshot.status == "unavailable"
    assert (
        snapshot.observation is not None and snapshot.observation.first_confirmation.value is None
    )


def _seed_previous_capture_metrics(
    runtime: FirstCrackSessionRuntime, pipeline: FakeAudioPipeline, monkeypatch: pytest.MonkeyPatch
):
    from dataclasses import replace

    polls: list[str] = []
    original = pipeline.snapshot

    def snapshot() -> AudioCaptureSnapshot:
        polls.append("A")
        return replace(
            original(),
            queued_window_count=3,
            emitted_window_count=13,
            dropped_window_count=4,
            latest_error="previous_A_capture_error",
            peak_dbfs=-3,
            rms_dbfs=-8,
            overflow_count_last_minute=2,
            estimated_lost_audio_ms_last_minute=150,
            total_overflow_count=9,
            max_consecutive_overflow_count=2,
        )

    monkeypatch.setattr(pipeline, "snapshot", snapshot)
    runtime._processed_window_count = 7  # pyright: ignore[reportPrivateUsage]
    runtime._last_inference_duration_ms = 81  # pyright: ignore[reportPrivateUsage]
    runtime._max_inference_duration_ms = 125  # pyright: ignore[reportPrivateUsage]
    runtime._inference_overrun_count = 3  # pyright: ignore[reportPrivateUsage]
    seeded = runtime.snapshot()
    assert seeded.emitted_window_count == 13 and seeded.processed_window_count == 7
    return polls


def test_refused_successor_does_not_poll_or_relabel_owned_previous_capture_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RoastSessionStore(default_log_dir=tmp_path / "projection-history")
    runtime, captures, _ = _manual_runtime_fixture(
        tmp_path, session_is_retained=lambda sid: _retains_session(store, sid)
    )
    entered, release = Event(), Event()
    original = runtime._observation_model_loader  # pyright: ignore[reportPrivateUsage]

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(fc)

    monkeypatch.setattr(runtime, "_observation_model_loader", blocked)
    first = store.start_session()
    runtime.start_for_session(first)
    owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    pipeline = captures[0]
    recorder_a, plan_a = runtime.recording_for_session(first.id)
    assert recorder_a is not None and plan_a is not None
    try:
        assert entered.wait(2)
        polls = _seed_previous_capture_metrics(runtime, pipeline, monkeypatch)
        before = len(polls)
        store.stop_session()
        second = store.start_session()
        snapshot = runtime.start_for_session(second)
        _assert_empty_successor_capture(snapshot)
        assert runtime.snapshot().active_session_id == second.id
        assert len(polls) == before
        assert runtime.recording_for_session(second.id) == (None, None)
        assert runtime._pipeline is pipeline  # pyright: ignore[reportPrivateUsage] - ownership must remain real
        assert runtime._active_recorder is recorder_a  # pyright: ignore[reportPrivateUsage]
        assert not owner.reaped.is_set() and not pipeline.shutdown_confirmed
        prior = runtime.observation_for_session(first.id)
        assert prior is not None and not prior.worker_reaped
        assert len(captures) == 1 and not pipeline.stop_reasons
    finally:
        release.set()
        assert owner.reaped.wait(2)
    assert len(pipeline.stop_reasons) == 1
    store.stop_session()
    third = store.start_session()
    runtime.start_for_session(third)
    next_owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert next_owner is not None
    try:
        assert runtime.snapshot().active and runtime.snapshot().active_session_id == third.id
        recorder_c, plan_c = runtime.recording_for_session(third.id)
        assert recorder_c is not recorder_a and plan_c != plan_a
    finally:
        runtime.close_observation(third, reason="beans_dropped")
        assert next_owner.reaped.wait(2)


@pytest.mark.parametrize("stage", ["recorder", "factory", "partial_capture"])
def test_successor_setup_failure_exposes_only_its_allocated_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from dataclasses import replace

    import coffee_roaster_mcp.first_crack_runtime as runtime_module

    store = RoastSessionStore(default_log_dir=tmp_path / "setup-projections")
    runtime, captures, _ = _manual_runtime_fixture(tmp_path)
    first = store.start_session()
    runtime.start_for_session(first)
    first_owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert first_owner is not None
    pipeline_a = captures[0]
    recorder_a, plan_a = runtime.recording_for_session(first.id)
    polls = _seed_previous_capture_metrics(runtime, pipeline_a, monkeypatch)
    runtime.close_observation(first, reason="beans_dropped")
    assert first_owner.reaped.wait(2)
    runtime.snapshot()  # Cache A's terminal nonzero metrics; do not finalise/drop handles.
    before = len(polls)
    store.stop_session()
    second = store.start_session()
    entered, release = Event(), Event()
    original_factory = runtime._audio_pipeline_factory  # pyright: ignore[reportPrivateUsage]

    def fail_recorder(*_args: object, **_kwargs: object):
        raise RuntimeError("synthetic recorder construction failure")

    def factory(audio: AudioConfig):
        if stage == "factory":
            raise RuntimeError("synthetic capture construction failure")
        pipeline = original_factory(audio)
        start = pipeline.start
        snapshot = pipeline.snapshot
        stop = pipeline.stop

        def own_snapshot() -> AudioCaptureSnapshot:
            return replace(
                snapshot(),
                emitted_window_count=2,
                dropped_window_count=1,
                total_overflow_count=1,
                peak_dbfs=-12,
                rms_dbfs=-15,
            )

        def partial_start():
            start()
            raise RuntimeError("synthetic partially started B capture")

        def blocked_stop(*, timeout_seconds: float = 1.0):
            entered.set()
            assert release.wait(5)
            return stop(timeout_seconds=timeout_seconds)

        monkeypatch.setattr(pipeline, "snapshot", own_snapshot)
        monkeypatch.setattr(pipeline, "start", partial_start)
        monkeypatch.setattr(pipeline, "stop", blocked_stop)
        return pipeline

    with monkeypatch.context() as patch:
        if stage == "recorder":
            patch.setattr(runtime_module, "build_session_recorder", fail_recorder)
        else:
            patch.setattr(runtime, "_audio_pipeline_factory", factory)
        try:
            failed = runtime.start_for_session(second)
            assert failed.status == "unavailable" and len(polls) == before
            recorder_b, plan_b = runtime.recording_for_session(second.id)
            if stage == "recorder":
                _assert_empty_successor_capture(failed)
                assert (recorder_b, plan_b) == (None, None)
            else:
                assert recorder_b is not None and recorder_b is not recorder_a and plan_b != plan_a
                cleanup = runtime._capture_cleanup  # pyright: ignore[reportPrivateUsage]
                assert cleanup is not None
                if stage == "factory":
                    assert cleanup.reaped.wait(2)
                    _assert_empty_successor_capture(runtime.snapshot())
                else:
                    assert entered.wait(2)
                    assert failed.active and failed.audio_running
                    assert failed.emitted_window_count == 2 and failed.dropped_window_count == 1
                    assert failed.total_overflow_count == 1 and failed.mic_peak_dbfs == -12
                    assert not cleanup.reaped.is_set()
        finally:
            release.set()
            cleanup = runtime._capture_cleanup  # pyright: ignore[reportPrivateUsage]
            if cleanup is not None and runtime._capture_cleanup_session_id == second.id:  # pyright: ignore[reportPrivateUsage]
                assert cleanup.reaped.wait(2)
    store.stop_session()
    third = store.start_session()
    runtime.start_for_session(third)
    third_owner = runtime._observer  # pyright: ignore[reportPrivateUsage]
    assert third_owner is not None
    try:
        assert runtime.snapshot().active_session_id == third.id and runtime.snapshot().active
        assert runtime.recording_for_session(third.id)[0] is not recorder_a
    finally:
        runtime.close_observation(third, reason="beans_dropped")
        assert third_owner.reaped.wait(2)
