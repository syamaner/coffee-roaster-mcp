from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from queue import Queue
from threading import Lock, Thread

import pytest

from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSettings,
    AudioInput,
    audio_capture_settings_from_config,
    build_audio_capture_pipeline,
)
from coffee_roaster_mcp.config import AudioConfig


class FiniteAudioInput:
    def __init__(self, samples: Sequence[float]) -> None:
        self._samples = list(samples)
        self.read_counts: list[int] = []

    def read_samples(self, sample_count: int) -> Sequence[float]:
        self.read_counts.append(sample_count)
        if not self._samples:
            return ()
        samples = self._samples[:sample_count]
        del self._samples[:sample_count]
        return tuple(samples)


class MutableAudioInput:
    def __init__(self, samples: Sequence[float]) -> None:
        self._samples = list(samples)
        self._lock = Lock()
        self.read_counts: list[int] = []

    def add_samples(self, samples: Sequence[float]) -> None:
        with self._lock:
            self._samples.extend(samples)

    def read_samples(self, sample_count: int) -> Sequence[float]:
        with self._lock:
            self.read_counts.append(sample_count)
            if not self._samples:
                return ()
            samples = self._samples[:sample_count]
            del self._samples[:sample_count]
            return tuple(samples)


class FailingAudioInput:
    def read_samples(self, sample_count: int) -> Sequence[float]:
        raise RuntimeError(f"capture failed after {sample_count} requested samples")


class ClockHarness:
    def __init__(self) -> None:
        self.value = 100.0

    def monotonic_now(self) -> float:
        self.value += 1.0
        return self.value


def test_audio_capture_settings_use_configured_audio_input() -> None:
    settings = audio_capture_settings_from_config(
        AudioConfig(input_device="roast-mic", sample_rate=8_000),
        window_seconds=0.5,
        queue_limit=3,
    )

    assert settings.input_device == "roast-mic"
    assert settings.sample_rate == 8_000
    assert settings.window_sample_count == 4_000
    assert settings.queue_limit == 3


def test_audio_capture_settings_reject_invalid_values() -> None:
    with pytest.raises(AudioCaptureError, match="sample_rate"):
        audio_capture_settings_from_config(AudioConfig(sample_rate=0))

    with pytest.raises(AudioCaptureError, match="window_seconds"):
        audio_capture_settings_from_config(AudioConfig(), window_seconds=0)

    with pytest.raises(AudioCaptureError, match="queue_limit"):
        audio_capture_settings_from_config(AudioConfig(), queue_limit=0)


def test_build_audio_capture_pipeline_passes_config_to_input_factory() -> None:
    audio_input = FiniteAudioInput(())
    factory_calls: list[AudioCaptureSettings] = []

    def input_factory(settings: AudioCaptureSettings) -> AudioInput:
        factory_calls.append(settings)
        return audio_input

    pipeline = build_audio_capture_pipeline(
        AudioConfig(input_device="configured-mic", sample_rate=4),
        input_factory,
        window_seconds=1.0,
    )

    assert pipeline.settings.input_device == "configured-mic"
    assert pipeline.settings.sample_rate == 4
    assert factory_calls == [pipeline.settings]


def test_audio_capture_pipeline_feeds_detector_windows_from_mock_input() -> None:
    clock = ClockHarness()
    audio_input = FiniteAudioInput((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
        monotonic_now=clock.monotonic_now,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 2)
    pipeline.stop()

    windows = pipeline.drain_windows()
    assert [window.sequence_number for window in windows] == [0, 1]
    assert [window.samples for window in windows] == [
        (0.1, 0.2, 0.3, 0.4),
        (0.5, 0.6, 0.7, 0.8),
    ]
    assert {window.input_device for window in windows} == {"mock-mic"}
    assert {window.sample_rate for window in windows} == {4}
    assert {window.duration_seconds for window in windows} == {1.0}
    assert [window.started_at_monotonic_seconds for window in windows] == [101.0, 102.0]


def test_audio_capture_pipeline_drops_windows_when_detector_queue_is_full() -> None:
    audio_input = FiniteAudioInput(tuple(float(value) for value in range(12)))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            queue_limit=1,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().dropped_window_count == 2)
    snapshot = pipeline.stop()

    assert snapshot.queued_window_count == 1
    assert snapshot.emitted_window_count == 1
    assert snapshot.dropped_window_count == 2
    assert pipeline.drain_windows()[0].samples == (0.0, 1.0, 2.0, 3.0)


def test_audio_capture_pipeline_resets_run_state_on_restart() -> None:
    audio_input = MutableAudioInput((10.0, 11.0, 12.0, 13.0, 1.0, 2.0))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    pipeline.stop()
    assert pipeline.drain_windows()[0].samples == (10.0, 11.0, 12.0, 13.0)

    audio_input.add_samples((3.0, 4.0, 5.0, 6.0))

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    pipeline.stop()

    windows = pipeline.drain_windows()
    assert [window.sequence_number for window in windows] == [0]
    assert windows[0].samples == (3.0, 4.0, 5.0, 6.0)


def test_audio_capture_pipeline_keeps_blocking_consumer_across_restart() -> None:
    audio_input = MutableAudioInput((10.0, 11.0, 12.0, 13.0))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    pipeline.stop()
    assert pipeline.drain_windows()[0].samples == (10.0, 11.0, 12.0, 13.0)

    received_windows: Queue[tuple[float, ...] | None] = Queue()
    consumer = Thread(
        target=lambda: received_windows.put(
            None
            if (
                window := pipeline.get_window(
                    block=True,
                    timeout_seconds=1.0,
                )
            )
            is None
            else window.samples
        )
    )
    consumer.start()
    _wait_for(lambda: consumer.is_alive())
    audio_input.add_samples((1.0, 2.0, 3.0, 4.0))

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    pipeline.stop()
    consumer.join(timeout=1.0)

    assert not consumer.is_alive()
    assert received_windows.get_nowait() == (1.0, 2.0, 3.0, 4.0)


def test_audio_capture_pipeline_records_source_errors_without_raising_on_caller() -> None:
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(input_device="bad-mic", sample_rate=4),
        audio_input=FailingAudioInput(),
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().latest_error is not None)
    snapshot = pipeline.stop()

    assert snapshot.running is False
    assert snapshot.latest_error == "capture failed after 4 requested samples"


def test_audio_capture_pipeline_rejects_double_start() -> None:
    audio_input = FiniteAudioInput(())
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    try:
        with pytest.raises(AudioCaptureError, match="already running"):
            pipeline.start()
    finally:
        pipeline.stop()


def test_audio_capture_pipeline_validates_finite_samples() -> None:
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(input_device=None, sample_rate=4),
        audio_input=FiniteAudioInput((0.0, float("nan"), 0.0, 0.0)),
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().latest_error is not None)
    snapshot = pipeline.stop()

    assert snapshot.latest_error == "audio samples must be finite numbers."


def _wait_for(predicate: Callable[[], bool], *, timeout_seconds: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("Timed out waiting for condition.")
