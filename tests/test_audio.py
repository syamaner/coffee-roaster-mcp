from __future__ import annotations

import struct
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread, current_thread

import pytest

from coffee_roaster_mcp import audio as audio_module
from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSettings,
    AudioInput,
    DetectorPacedWavReplayPipeline,
    MicrophoneAudioInput,
    OverflowSnapshot,
    RoastAudioRecorder,
    WavAudioInput,
    amplitude_to_dbfs,
    audio_capture_settings_from_config,
    build_audio_capture_pipeline,
    build_configured_audio_input,
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


class FakeRawInputStream:
    created: list[FakeRawInputStream] = []

    def __init__(
        self,
        *,
        samplerate: int,
        device: str | None,
        channels: int,
        dtype: str,
        blocksize: int,
    ) -> None:
        self.samplerate = samplerate
        self.device = device
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.started = False
        self.stopped = False
        self.closed = False
        self.overflowed = False
        self._samples = [0.25, -0.5, 0.75, 1.0]
        self.read_counts: list[int] = []
        FakeRawInputStream.created.append(self)

    def start(self) -> None:
        self.started = True

    def read(self, sample_count: int) -> tuple[bytes, bool]:
        self.read_counts.append(sample_count)
        samples = self._samples[:sample_count]
        del self._samples[:sample_count]
        return struct.pack(f"{len(samples)}f", *samples), self.overflowed

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class StartFailingRawInputStream(FakeRawInputStream):
    def start(self) -> None:
        raise RuntimeError("device busy")


class OverflowingRawInputStream(FakeRawInputStream):
    def __init__(
        self,
        *,
        samplerate: int,
        device: str | None,
        channels: int,
        dtype: str,
        blocksize: int,
    ) -> None:
        super().__init__(
            samplerate=samplerate,
            device=device,
            channels=channels,
            dtype=dtype,
            blocksize=blocksize,
        )
        self.overflowed = True


class FakeSoundDeviceModule:
    RawInputStream = FakeRawInputStream


class RecoveringSoundDeviceModule:
    RawInputStream: type[FakeRawInputStream] = StartFailingRawInputStream


class StartFailingSoundDeviceModule:
    RawInputStream = StartFailingRawInputStream


class OverflowingSoundDeviceModule:
    RawInputStream = OverflowingRawInputStream


class ClockHarness:
    def __init__(self) -> None:
        self.value = 100.0

    def monotonic_now(self) -> float:
        self.value += 1.0
        return self.value


def test_audio_capture_settings_use_configured_audio_input() -> None:
    settings = audio_capture_settings_from_config(
        AudioConfig(input_device="roast-mic", sample_rate=8_000, replay_mode="realtime"),
        window_seconds=0.5,
        queue_limit=3,
    )

    assert settings.input_device == "roast-mic"
    assert settings.sample_rate == 8_000
    assert settings.replay_mode == "realtime"
    assert settings.window_sample_count == 4_000
    assert settings.queue_limit == 3


def test_audio_capture_settings_reject_invalid_values() -> None:
    with pytest.raises(AudioCaptureError, match="sample_rate"):
        audio_capture_settings_from_config(AudioConfig(sample_rate=0))

    with pytest.raises(AudioCaptureError, match="window_seconds"):
        audio_capture_settings_from_config(AudioConfig(), window_seconds=0)

    with pytest.raises(AudioCaptureError, match="queue_limit"):
        audio_capture_settings_from_config(AudioConfig(), queue_limit=0)

    with pytest.raises(AudioCaptureError, match="replay_mode"):
        audio_capture_settings_from_config(
            AudioConfig(source="microphone", replay_mode="detector_paced")
        )


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


def test_configured_audio_input_factory_builds_wav_source(tmp_path: Path) -> None:
    wav_path = tmp_path / "fixture.wav"
    _write_pcm16_wav(
        wav_path,
        sample_rate=4,
        channel_count=1,
        samples=(0, 16_384, -16_384, 32_767),
    )
    settings = audio_capture_settings_from_config(
        AudioConfig(source="wav", sample_rate=4, wav_path=wav_path),
    )

    audio_input = build_configured_audio_input(settings)

    assert isinstance(audio_input, WavAudioInput)
    assert audio_input.read_samples(4) == (0.0, 0.5, -0.5, 32_767 / 32_768)


def test_detector_paced_wav_pipeline_drains_without_background_thread(tmp_path: Path) -> None:
    wav_path = tmp_path / "fixture.wav"
    _write_pcm16_wav(
        wav_path,
        sample_rate=4,
        channel_count=1,
        samples=(0, 8192, 16384, 24576, -8192, -16384, -24576, -32768),
    )

    pipeline = build_audio_capture_pipeline(
        AudioConfig(
            source="wav",
            sample_rate=4,
            wav_path=wav_path,
            replay_mode="detector_paced",
        ),
        window_seconds=1.0,
        monotonic_now=lambda: 100.0,
    )

    start = pipeline.start()
    first_batch = pipeline.drain_windows(max_windows=1)
    second_batch = pipeline.drain_windows()
    exhausted = pipeline.snapshot()
    stopped = pipeline.stop()

    assert isinstance(pipeline, DetectorPacedWavReplayPipeline)
    assert start.running is True
    assert first_batch[0].sequence_number == 0
    assert first_batch[0].started_at_monotonic_seconds == 100.0
    assert first_batch[0].samples == (0.0, 0.25, 0.5, 0.75)
    assert second_batch[0].sequence_number == 1
    assert second_batch[0].started_at_monotonic_seconds == 101.0
    assert exhausted.running is False
    assert exhausted.emitted_window_count == 2
    assert exhausted.dropped_window_count == 0
    assert stopped.running is False


def test_configured_audio_input_factory_requires_wav_path() -> None:
    settings = audio_capture_settings_from_config(AudioConfig(source="wav", sample_rate=4))

    with pytest.raises(AudioCaptureError, match="audio.wav_path"):
        build_configured_audio_input(settings)


def test_wav_audio_input_averages_channels_to_mono_float_samples(tmp_path: Path) -> None:
    wav_path = tmp_path / "stereo.wav"
    _write_pcm16_wav(
        wav_path,
        sample_rate=4,
        channel_count=2,
        samples=(32_767, 32_767, 0, 0, -32_768, -32_768),
    )

    with WavAudioInput(wav_path, sample_rate=4) as audio_input:
        assert audio_input.read_samples(3) == (32_767 / 32_768, 0.0, -1.0)
        assert audio_input.read_samples(3) == ()


def test_wav_audio_input_rejects_sample_rate_mismatch(tmp_path: Path) -> None:
    wav_path = tmp_path / "wrong-rate.wav"
    _write_pcm16_wav(wav_path, sample_rate=8, channel_count=1, samples=(0,))

    with pytest.raises(AudioCaptureError, match="sample rate 8"):
        WavAudioInput(wav_path, sample_rate=4)


def test_microphone_audio_input_uses_configured_portaudio_stream() -> None:
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(
        AudioConfig(source="microphone", input_device="hw:1,0", sample_rate=4)
    )

    audio_input = MicrophoneAudioInput(settings, sounddevice_module=FakeSoundDeviceModule())
    assert FakeRawInputStream.created == []

    with audio_input:
        samples = audio_input.read_samples(3)

    stream = FakeRawInputStream.created[0]
    assert stream.samplerate == 4
    assert stream.device == "hw:1,0"
    assert stream.channels == 1
    assert stream.dtype == "float32"
    assert stream.started is True
    assert stream.stopped is True
    assert stream.closed is True
    assert stream.read_counts == [3]
    assert samples == (0.25, -0.5, 0.75)


def test_microphone_audio_input_tolerates_transient_overflow() -> None:
    """A transient input overflow is non-fatal (#160): the captured samples still
    come through and capture continues. Only a sustained run of consecutive
    overflows (the device cannot keep up at all) faults."""
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=OverflowingSoundDeviceModule(),
        max_consecutive_overflows=3,
    )

    try:
        # The first overflows do NOT raise; the samples captured despite the
        # overflow are returned so detection keeps running.
        assert audio_input.read_samples(1) == (0.25,)
        assert audio_input.read_samples(1) == (-0.5,)
        # The third consecutive overflow trips the sustained-failure backstop.
        with pytest.raises(AudioCaptureError, match="consecutive"):
            audio_input.read_samples(1)
    finally:
        audio_input.close()


def test_microphone_audio_input_resets_overflow_streak_on_clean_read() -> None:
    """A clean read between overflows resets the consecutive-overflow counter, so
    intermittent overflows never accumulate into a fault (#160)."""
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=FakeSoundDeviceModule(),
        max_consecutive_overflows=2,
    )

    try:
        audio_input.read_samples(1)  # opens the stream; clean read
        stream = FakeRawInputStream.created[-1]
        stream.overflowed = True
        audio_input.read_samples(1)  # first consecutive overflow (streak = 1)
        stream.overflowed = False
        audio_input.read_samples(1)  # clean read resets the streak to 0
        stream.overflowed = True
        # A fresh streak: this is overflow #1 again, not #2, so it must not raise.
        audio_input.read_samples(1)
    finally:
        audio_input.close()


def test_microphone_audio_input_closes_stream_when_start_fails() -> None:
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(
        AudioConfig(source="microphone", input_device="busy-mic", sample_rate=4)
    )

    with pytest.raises(AudioCaptureError, match="device busy"):
        MicrophoneAudioInput(
            settings,
            sounddevice_module=StartFailingSoundDeviceModule(),
        ).read_samples(1)

    stream = FakeRawInputStream.created[0]
    assert stream.closed is True


def test_microphone_audio_input_retries_after_start_failure() -> None:
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(
        AudioConfig(source="microphone", input_device="busy-once", sample_rate=4)
    )
    sounddevice = RecoveringSoundDeviceModule()
    audio_input = MicrophoneAudioInput(settings, sounddevice_module=sounddevice)

    with pytest.raises(AudioCaptureError, match="device busy"):
        audio_input.read_samples(1)

    sounddevice.RawInputStream = FakeRawInputStream
    samples = audio_input.read_samples(1)

    assert len(FakeRawInputStream.created) == 2
    assert FakeRawInputStream.created[0].closed is True
    assert FakeRawInputStream.created[1].started is True
    assert samples == (0.25,)


def test_microphone_audio_input_normalizes_missing_portaudio_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))

    def fail_import(module_name: str) -> object:
        assert module_name == "sounddevice"
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(audio_module.importlib, "import_module", fail_import)

    with pytest.raises(AudioCaptureError, match="PortAudio runtime"):
        build_configured_audio_input(settings)


def test_wav_and_microphone_sources_feed_same_window_contract(tmp_path: Path) -> None:
    wav_path = tmp_path / "fixture.wav"
    _write_pcm16_wav(wav_path, sample_rate=4, channel_count=1, samples=(0, 8192, -8192, 16_384))
    wav_pipeline = build_audio_capture_pipeline(
        AudioConfig(source="wav", sample_rate=4, wav_path=wav_path),
        window_seconds=1.0,
        idle_sleep_seconds=0.001,
    )

    wav_pipeline.start()
    _wait_for(lambda: wav_pipeline.snapshot().emitted_window_count == 1)
    wav_pipeline.stop()

    microphone_pipeline = build_audio_capture_pipeline(
        AudioConfig(source="microphone", input_device="mock-mic", sample_rate=4),
        lambda settings: MicrophoneAudioInput(
            settings,
            sounddevice_module=FakeSoundDeviceModule(),
        ),
        window_seconds=1.0,
        idle_sleep_seconds=0.001,
    )
    microphone_pipeline.start()
    _wait_for(lambda: microphone_pipeline.snapshot().emitted_window_count == 1)
    microphone_pipeline.stop()

    wav_window = wav_pipeline.drain_windows()[0]
    microphone_window = microphone_pipeline.drain_windows()[0]
    microphone_stream = FakeRawInputStream.created[-1]
    assert wav_window.sample_rate == microphone_window.sample_rate == 4
    assert len(wav_window.samples) == len(microphone_window.samples) == 4
    assert wav_window.duration_seconds == microphone_window.duration_seconds == 1.0
    assert microphone_stream.stopped is True
    assert microphone_stream.closed is True


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


def test_audio_capture_pipeline_emits_overlapping_windows_from_mock_input() -> None:
    audio_input = FiniteAudioInput(tuple(float(value) for value in range(10)))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            overlap=0.5,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 4)
    pipeline.stop()

    windows = pipeline.drain_windows()
    assert [window.sequence_number for window in windows] == [0, 1, 2, 3]
    assert [window.samples for window in windows] == [
        (0.0, 1.0, 2.0, 3.0),
        (2.0, 3.0, 4.0, 5.0),
        (4.0, 5.0, 6.0, 7.0),
        (6.0, 7.0, 8.0, 9.0),
    ]


def test_detector_paced_wav_replay_emits_overlapping_source_timeline(tmp_path: Path) -> None:
    wav_path = tmp_path / "overlap.wav"
    _write_pcm16_wav(
        wav_path,
        sample_rate=4,
        channel_count=1,
        samples=tuple(range(10)),
    )
    pipeline = build_audio_capture_pipeline(
        AudioConfig(
            source="wav",
            sample_rate=4,
            wav_path=wav_path,
            replay_mode="detector_paced",
            window_seconds=1.0,
            overlap=0.5,
        ),
        monotonic_now=lambda: 100.0,
    )

    pipeline.start()
    windows = pipeline.drain_windows()

    assert [window.sequence_number for window in windows] == [0, 1, 2, 3]
    assert [window.started_at_monotonic_seconds for window in windows] == [
        100.0,
        100.5,
        101.0,
        101.5,
    ]
    assert [window.samples for window in windows] == [
        (0.0, 1 / 32768.0, 2 / 32768.0, 3 / 32768.0),
        (2 / 32768.0, 3 / 32768.0, 4 / 32768.0, 5 / 32768.0),
        (4 / 32768.0, 5 / 32768.0, 6 / 32768.0, 7 / 32768.0),
        (6 / 32768.0, 7 / 32768.0, 8 / 32768.0, 9 / 32768.0),
    ]


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


def test_audio_capture_pipeline_resets_overflow_tracking_on_restart() -> None:
    """coffee-roaster-mcp#193 review finding: a pipeline can be start()ed
    more than once against the SAME microphone input (the sibling
    non-overflow restart test above proves this is a real, already-tested
    pattern) — total_overflow_count is documented as "lifetime... for the
    CURRENT capture run", so a prior run's overflow history must not leak
    into a fresh roast's totals.
    """
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    clock = ClockHarness()
    clock.value = 0.0
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=FakeSoundDeviceModule(),
        max_consecutive_overflows=10,
        monotonic_now=lambda: clock.value,
    )
    pipeline = AudioCapturePipeline(settings=settings, audio_input=audio_input)

    # First run: drive one overflowed read directly (no background thread
    # racing this, per the sibling overflow tests' documented discipline),
    # then reset via a second start() — start()/stop() themselves don't
    # spawn a worker whose reads would interfere with this direct call.
    audio_input.read_samples(1)  # opens the stream; clean read at t=0.0
    stream = FakeRawInputStream.created[-1]
    clock.value = 1.0
    stream.overflowed = True
    audio_input.read_samples(1)
    assert audio_input.overflow_snapshot.total_count == 1
    # Clear the fake stream's overflow flag before the real background
    # worker below starts reading — it never resets on its own, and the
    # point of this test is the RESET plumbing, not accumulating more
    # overflows from the worker's own reads.
    stream.overflowed = False

    # A fresh start() (the same restart pattern the sibling test exercises
    # for windows/buffers) must reset overflow tracking too, not just
    # windows/buffers/the level meter.
    pipeline.start()
    pipeline.stop()

    fresh_snapshot = audio_input.overflow_snapshot
    assert fresh_snapshot.total_count == 0
    assert fresh_snapshot.count_last_minute == 0
    assert fresh_snapshot.estimated_lost_audio_ms_last_minute == 0.0


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


class BlockingClosableAudioInput:
    """Audio input whose reads block, recording which thread closes it.

    Models the real microphone path: a worker thread is parked inside a blocking
    ``read`` when ``stop`` is requested. Closing the underlying stream from any
    thread other than the one reading it frees the native ring buffer under an
    in-flight read and crashes the process, so this records the closing thread
    and whether the worker was still alive at close time.
    """

    def __init__(self) -> None:
        self._release = Event()
        self.closed = False
        self.closed_by_worker: bool | None = None
        self.worker_alive_at_close: bool | None = None
        self.worker_thread: Thread | None = None
        self.read_started = Event()

    def read_samples(self, sample_count: int) -> Sequence[float]:
        self.worker_thread = current_thread()
        self.read_started.set()
        # Block until released, mimicking a microphone read that outlasts the
        # stop join timeout.
        self._release.wait(timeout=2.0)
        return (0.1,) * sample_count

    def release(self) -> None:
        self._release.set()

    def close(self) -> None:
        self.closed = True
        self.closed_by_worker = current_thread() is self.worker_thread
        self.worker_alive_at_close = (
            self.worker_thread is not None and self.worker_thread.is_alive()
        )


def test_audio_capture_stop_does_not_close_input_while_worker_reads() -> None:
    """A worker blocked mid-read must close its own input, never the stop caller.

    Regression for the end-of-roast SIGSEGV: ``stop`` closed the PortAudio stream
    after a join timeout even though the worker was still inside ``read``, freeing
    the ring buffer under the in-flight read and crashing the process.
    """
    audio_input = BlockingClosableAudioInput()
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)
    # Worker is parked inside read(); a zero-timeout join must not close the input.
    pipeline.stop(timeout_seconds=0.0)
    assert audio_input.closed is False

    # Releasing the read lets the worker observe the stop request, exit, and close.
    audio_input.release()
    _wait_for(lambda: audio_input.closed)
    assert audio_input.closed_by_worker is True
    worker = audio_input.worker_thread
    assert worker is not None
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def _wait_for(predicate: Callable[[], bool], *, timeout_seconds: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("Timed out waiting for condition.")


def _write_pcm16_wav(
    path: Path,
    *,
    sample_rate: int,
    channel_count: int,
    samples: Sequence[int],
) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channel_count)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _read_wav_samples(path: Path) -> tuple[tuple[int, ...], int, int]:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)
    values = tuple(value[0] for value in struct.iter_unpack("<h", raw))
    return values, channels, sample_rate


def test_roast_recorder_writes_wav_and_sidecar(tmp_path: Path) -> None:
    wav_path = tmp_path / "session-1" / "roast.wav"
    sidecar_path = tmp_path / "session-1" / "roast.recording.json"
    recorder = RoastAudioRecorder(
        wav_path=wav_path,
        sidecar_path=sidecar_path,
        sample_rate=8,
        session_id="session-1",
        milestones_provider=lambda: {"beans_added": 1.5, "first_crack": None},
        monotonic_now=lambda: 123.0,
        flush_sample_threshold=2,
    )

    recorder.begin()
    recorder.write_samples((0.0, 1.0, -1.0))
    recorder.write_samples((0.5,))
    recorder.close()

    assert recorder.frames_written == 4
    assert recorder.started_monotonic_seconds == 123.0
    values, channels, sample_rate = _read_wav_samples(wav_path)
    assert channels == 1
    assert sample_rate == 8
    assert values == (0, 32767, -32767, round(0.5 * 32767))

    import json

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["session_id"] == "session-1"
    assert sidecar["sample_rate"] == 8
    assert sidecar["channels"] == 1
    assert sidecar["frame_count"] == 4
    assert sidecar["recording_started_monotonic_seconds"] == 123.0
    assert sidecar["milestones"] == {"beans_added": 1.5, "first_crack": None}
    assert sidecar["wav_filename"] == "roast.wav"


def test_roast_recorder_clamps_out_of_range_samples(tmp_path: Path) -> None:
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "r.wav",
        sidecar_path=tmp_path / "r.json",
        sample_rate=4,
        session_id="s",
    )
    recorder.begin()
    recorder.write_samples((2.0, -2.0))
    recorder.close()

    values, _, _ = _read_wav_samples(tmp_path / "r.wav")
    assert values == (32767, -32767)


def test_roast_recorder_rejects_invalid_sample_rate(tmp_path: Path) -> None:
    with pytest.raises(AudioCaptureError, match="sample_rate"):
        RoastAudioRecorder(
            wav_path=tmp_path / "r.wav",
            sidecar_path=tmp_path / "r.json",
            sample_rate=0,
            session_id="s",
        )


def test_roast_recorder_close_is_idempotent_and_write_after_close_noops(
    tmp_path: Path,
) -> None:
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "r.wav",
        sidecar_path=tmp_path / "r.json",
        sample_rate=4,
        session_id="s",
    )
    recorder.begin()
    recorder.write_samples((0.25,))
    recorder.close()
    recorder.close()
    recorder.write_samples((0.5,))

    assert recorder.frames_written == 1


def test_pipeline_tees_detector_stream_into_wav(tmp_path: Path) -> None:
    samples = tuple(float(value) / 8.0 for value in range(8))
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=4,
        session_id="s",
        flush_sample_threshold=1,
    )
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=FiniteAudioInput(samples),
        recorder=recorder,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 2)
    pipeline.stop()

    windows = pipeline.drain_windows()
    # The detector still sees the unmodified float windows: teeing did not change
    # detector behavior.
    assert windows[0].samples == samples[:4]
    assert windows[1].samples == samples[4:]
    # The recorder captured the SAME samples (as 16-bit PCM).
    values, channels, sample_rate = _read_wav_samples(tmp_path / "roast.wav")
    assert channels == 1
    assert sample_rate == 4
    assert values == tuple(round(sample * 32767) for sample in samples)


def test_pipeline_without_recorder_writes_no_wav(tmp_path: Path) -> None:
    wav_path = tmp_path / "roast.wav"
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=FiniteAudioInput((0.0, 1.0, 2.0, 3.0)),
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    pipeline.stop()

    assert not wav_path.exists()


def test_pipeline_recorder_write_failure_does_not_kill_detection(tmp_path: Path) -> None:
    class FailingRecorder(RoastAudioRecorder):
        def write_samples(self, samples: Sequence[float]) -> None:
            raise RuntimeError("disk full")

    recorder = FailingRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=4,
        session_id="s",
    )
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=FiniteAudioInput((0.0, 1.0, 2.0, 3.0)),
        recorder=recorder,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    snapshot = pipeline.stop()

    # Detection survived the recorder failure: the window was still emitted and
    # the capture worker reported no fatal error.
    assert snapshot.emitted_window_count == 1
    assert snapshot.latest_error is None
    assert pipeline.drain_windows()[0].samples == (0.0, 1.0, 2.0, 3.0)


def test_pipeline_recorder_begin_failure_does_not_kill_detection(tmp_path: Path) -> None:
    """Finding #1: a recording-START failure must not stop capture/detection."""

    class BeginFailingRecorder(RoastAudioRecorder):
        def begin(self) -> None:
            raise RuntimeError("could not open WAV")

    recorder = BeginFailingRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=4,
        session_id="s",
    )
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=FiniteAudioInput((0.0, 1.0, 2.0, 3.0)),
        recorder=recorder,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 1)
    snapshot = pipeline.stop()

    # The begin() failure dropped the recorder but detection ran to completion.
    assert snapshot.emitted_window_count == 1
    assert snapshot.latest_error is None
    assert pipeline.drain_windows()[0].samples == (0.0, 1.0, 2.0, 3.0)
    # No sidecar: recording never started.
    assert not (tmp_path / "roast.json").exists()


def test_pipeline_recorder_write_failure_finalizes_partial_recording(tmp_path: Path) -> None:
    """Finding #2: a write failure flushes the WAV + writes the sidecar before drop."""
    import json

    class FailOnSecondWriteRecorder(RoastAudioRecorder):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self._writes = 0

        def write_samples(self, samples: Sequence[float]) -> None:
            self._writes += 1
            if self._writes >= 2:
                raise RuntimeError("disk full mid-roast")
            super().write_samples(samples)

    recorder = FailOnSecondWriteRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
        flush_sample_threshold=1,
    )
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        # Two windows: the first write succeeds and is captured; the second
        # write raises, finalizing the partial recording.
        audio_input=FiniteAudioInput((0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)),
        recorder=recorder,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().emitted_window_count == 2)
    snapshot = pipeline.stop()

    # Detection unaffected.
    assert snapshot.latest_error is None
    # The partial recording was finalized: the first window's samples are in the
    # WAV (not leaked) and the sidecar was written.
    values, _, _ = _read_wav_samples(tmp_path / "roast.wav")
    assert len(values) >= 4
    sidecar = json.loads((tmp_path / "roast.recording.json").read_text(encoding="utf-8"))
    assert sidecar["frame_count"] >= 4
    assert sidecar["wav_filename"] == "roast.wav"


def test_device_label_to_filename_slug() -> None:
    from coffee_roaster_mcp.audio import device_label_to_filename

    assert device_label_to_filename("USB PnP") == "usb-pnp"
    assert device_label_to_filename("ATR2100x-USB") == "atr2100x-usb"
    assert device_label_to_filename("  !!  ") == "device"
    assert device_label_to_filename("A  B__C") == "a-b-c"


class _BoundedInput:
    """Yields a fixed number of constant-amplitude reads, then end-of-stream."""

    def __init__(self, amplitude: float, reads: int) -> None:
        self._amplitude = amplitude
        self._reads = reads
        self.closed = False

    def read_samples(self, sample_count: int) -> Sequence[float]:
        if self._reads <= 0:
            return ()
        self._reads -= 1
        return (self._amplitude,) * sample_count

    def close(self) -> None:
        self.closed = True


class _BoundedInputWithFixedOverflow(_BoundedInput):
    """`_BoundedInput` that also reports a fixed overflow snapshot (#193
    review finding coverage: additional-device overflow aggregation)."""

    def __init__(self, amplitude: float, reads: int, overflow: OverflowSnapshot) -> None:
        super().__init__(amplitude, reads)
        self._overflow = overflow

    @property
    def overflow_snapshot(self) -> OverflowSnapshot:
        return self._overflow


def test_multi_device_recorder_writes_two_wavs(tmp_path: Path) -> None:
    import json
    import time

    from coffee_roaster_mcp.audio import AdditionalRecordingDevice, MultiDeviceRoastRecorder

    inputs: dict[str, _BoundedInput] = {}

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        created = _BoundedInput(amplitude=0.5, reads=4)
        inputs[device.device_label] = created
        return created

    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "roast.usb-pnp.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
        additional_devices=[
            AdditionalRecordingDevice("ATR2100x", tmp_path / "roast.atr2100x.wav", 4),
        ],
        milestones_provider=lambda: {"beans_added": 1.0, "first_crack": 9.0},
        additional_input_factory=factory,
        additional_read_seconds=0.25,
        idle_sleep_seconds=0.001,
        stop_timeout_seconds=2.0,
    )

    recorder.begin()
    recorder.write_samples((0.25,) * 8)  # teed detector samples → detector WAV only
    # Let the independent ATR2100x stream drain its bounded input, then close
    # (close() joins the capture thread and finalizes every WAV).
    time.sleep(0.1)
    recorder.close()

    # Two WAVs: the teed detector stream + the independent additional stream.
    detector_values, _, _ = _read_wav_samples(tmp_path / "roast.usb-pnp.wav")
    additional_values, _, _ = _read_wav_samples(tmp_path / "roast.atr2100x.wav")
    assert detector_values == (round(0.25 * 32767),) * 8  # teed samples only
    assert len(additional_values) > 0
    assert all(value == round(0.5 * 32767) for value in additional_values)  # independent capture
    # The additional device was opened fresh and closed on its own thread.
    assert inputs["ATR2100x"].closed is True

    sidecar = json.loads((tmp_path / "roast.recording.json").read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == 2
    devices = [stream["device"] for stream in sidecar["streams"]]
    assert devices == ["USB PnP", "ATR2100x"]
    # Per-stream sample rate + the detector stream surfaced at top level (back-compat).
    assert all(stream["sample_rate"] == 4 for stream in sidecar["streams"])
    assert sidecar["wav_filename"] == "roast.usb-pnp.wav"
    assert sidecar["milestones"] == {"beans_added": 1.0, "first_crack": 9.0}
    assert recorder.additional_wav_paths == (tmp_path / "roast.atr2100x.wav",)


def test_multi_device_recorder_aggregates_overflow_across_additional_streams(
    tmp_path: Path,
) -> None:
    """coffee-roaster-mcp#193 review finding: each additional device has its
    OWN overflow tracker, previously invisible to any diagnostic —
    MultiDeviceRoastRecorder.overflow_snapshot must aggregate them
    additively (count/estimated-ms/lifetime-total all sum across streams).
    """
    import time

    from coffee_roaster_mcp.audio import AdditionalRecordingDevice, MultiDeviceRoastRecorder

    inputs: dict[str, _BoundedInputWithFixedOverflow] = {}

    def factory(device: AdditionalRecordingDevice) -> _BoundedInputWithFixedOverflow:
        overflow = (
            OverflowSnapshot(
                count_last_minute=2, estimated_lost_audio_ms_last_minute=200.0, total_count=5
            )
            if device.device_label == "MIC-A"
            else OverflowSnapshot(
                count_last_minute=3, estimated_lost_audio_ms_last_minute=150.0, total_count=1
            )
        )
        created = _BoundedInputWithFixedOverflow(amplitude=0.5, reads=4, overflow=overflow)
        inputs[device.device_label] = created
        return created

    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "roast.usb-pnp.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
        additional_devices=[
            AdditionalRecordingDevice("MIC-A", tmp_path / "roast.mic-a.wav", 4),
            AdditionalRecordingDevice("MIC-B", tmp_path / "roast.mic-b.wav", 4),
        ],
        additional_input_factory=factory,
        additional_read_seconds=0.25,
        idle_sleep_seconds=0.001,
        stop_timeout_seconds=2.0,
    )

    recorder.begin()
    time.sleep(0.1)
    recorder.close()

    overflow = recorder.overflow_snapshot
    assert overflow is not None
    assert overflow.count_last_minute == 5  # 2 + 3
    assert overflow.estimated_lost_audio_ms_last_minute == 350.0  # 200.0 + 150.0
    assert overflow.total_count == 6  # 5 + 1


def test_multi_device_recorder_overflow_snapshot_is_none_without_reporting_streams(
    tmp_path: Path,
) -> None:
    """No additional devices (or none reporting overflows, e.g. plain test
    doubles) must surface as `None`, matching the "no overflow-capable
    input" convention `AudioCapturePipeline.snapshot()` already uses."""
    from coffee_roaster_mcp.audio import MultiDeviceRoastRecorder

    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "roast.usb-pnp.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
    )

    assert recorder.overflow_snapshot is None


def test_pipeline_snapshot_folds_in_recorder_overflow_additively() -> None:
    """coffee-roaster-mcp#193 review finding: AudioCapturePipeline.snapshot()
    must merge the detector device's OWN overflow stats with the recorder's
    aggregate (additional devices) additively, not overwrite one with the
    other — the two are independent streams and neither double-counts.
    """

    class _RecorderWithOverflow:
        overflow_snapshot = OverflowSnapshot(
            count_last_minute=3, estimated_lost_audio_ms_last_minute=300.0, total_count=9
        )

        @property
        def started_monotonic_seconds(self) -> float | None:
            return None

        def begin(self) -> None:
            return None

        def write_samples(self, samples: Sequence[float]) -> None:
            return None

        def close(self) -> None:
            return None

    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    clock = ClockHarness()
    clock.value = 0.0
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=FakeSoundDeviceModule(),
        max_consecutive_overflows=10,
        monotonic_now=lambda: clock.value,
    )
    pipeline = AudioCapturePipeline(
        settings=settings,
        audio_input=audio_input,
        recorder=_RecorderWithOverflow(),
    )

    audio_input.read_samples(1)  # clean read at t=0.0, opens the stream
    stream = FakeRawInputStream.created[-1]
    clock.value = 1.0
    stream.overflowed = True
    audio_input.read_samples(1)  # detector device: 1 overflow, 750ms estimate

    snapshot = pipeline.snapshot()
    # Detector device (1, 750.0, 1) + recorder aggregate (3, 300.0, 9).
    assert snapshot.overflow_count_last_minute == 4
    assert snapshot.estimated_lost_audio_ms_last_minute == 1050.0
    assert snapshot.total_overflow_count == 10


def test_multi_device_recorder_drops_only_failing_stream(tmp_path: Path) -> None:
    import json
    import time

    from coffee_roaster_mcp.audio import AdditionalRecordingDevice, MultiDeviceRoastRecorder

    class FailingInput:
        def read_samples(self, sample_count: int) -> Sequence[float]:
            raise RuntimeError("device unplugged")

        def close(self) -> None:
            return None

    good = _BoundedInput(amplitude=0.5, reads=4)

    def factory(device: AdditionalRecordingDevice) -> AudioInput:
        return FailingInput() if device.device_label == "BAD" else good

    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "roast.usb-pnp.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
        additional_devices=[
            AdditionalRecordingDevice("BAD", tmp_path / "roast.bad.wav", 4),
            AdditionalRecordingDevice("GOOD", tmp_path / "roast.good.wav", 4),
        ],
        additional_input_factory=factory,
        additional_read_seconds=0.25,
        idle_sleep_seconds=0.001,
        stop_timeout_seconds=2.0,
    )

    recorder.begin()
    recorder.write_samples((0.25,) * 8)
    # Let both independent streams run (one fails immediately, one captures),
    # then close — which joins every thread and finalizes the surviving WAVs.
    time.sleep(0.1)
    recorder.close()

    # The detector (teed) WAV and the good additional WAV survived the failing one.
    detector_values, _, _ = _read_wav_samples(tmp_path / "roast.usb-pnp.wav")
    good_values, _, _ = _read_wav_samples(tmp_path / "roast.good.wav")
    assert detector_values == (round(0.25 * 32767),) * 8
    assert len(good_values) > 0

    sidecar = json.loads((tmp_path / "roast.recording.json").read_text(encoding="utf-8"))
    # All three streams are listed; the failing one captured zero frames.
    by_device = {stream["device"]: stream for stream in sidecar["streams"]}
    assert by_device["BAD"]["frame_count"] == 0
    assert by_device["GOOD"]["frame_count"] > 0
    assert by_device["USB PnP"]["frame_count"] == 8


def test_capture_devices_independently_writes_all(tmp_path: Path) -> None:
    import time

    from coffee_roaster_mcp.audio import (
        AdditionalRecordingDevice,
        capture_devices_independently,
    )

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=0.5, reads=3)

    devices = [
        AdditionalRecordingDevice("USB PnP", tmp_path / "c.usb-pnp.wav", 4),
        AdditionalRecordingDevice("ATR2100x", tmp_path / "c.atr2100x.wav", 4),
    ]

    results = capture_devices_independently(
        devices,
        record_seconds=0.05,
        sidecar_path=tmp_path / "c.json",
        session_id="record-check",
        input_factory=factory,
        sleep=time.sleep,
        read_seconds=0.01,
        idle_sleep_seconds=0.001,
        stop_timeout_seconds=2.0,
    )

    assert [r.device_label for r in results] == ["USB PnP", "ATR2100x"]
    assert all(r.frame_count > 0 for r in results)
    assert (tmp_path / "c.usb-pnp.wav").exists()
    assert (tmp_path / "c.atr2100x.wav").exists()
    assert (tmp_path / "c.json").exists()


def test_multi_device_recorder_writes_annotation_session_json(tmp_path: Path) -> None:
    """Finding: the {origin}-roast{N}-session.json matches the record_mics.py shape."""
    import json
    import time

    from coffee_roaster_mcp.audio import (
        AdditionalRecordingDevice,
        AnnotationSessionSpec,
        MultiDeviceRoastRecorder,
    )

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=0.5, reads=4)

    annotation_path = tmp_path / "brazil-roast7-session.json"
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "mic1-brazil-roast7.wav",
        detector_device_label="USB PnP",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=8,
        session_id="s",
        additional_devices=[
            AdditionalRecordingDevice("ATR2100x", tmp_path / "mic2-brazil-roast7.wav", 8),
        ],
        annotation_session=AnnotationSessionSpec(
            path=annotation_path,
            origin="brazil",
            roast_num=7,
            mic_labels=("USB PnP", "ATR2100x"),
        ),
        additional_input_factory=factory,
        additional_read_seconds=0.25,
        idle_sleep_seconds=0.001,
        stop_timeout_seconds=2.0,
    )

    recorder.begin()
    recorder.write_samples((0.25,) * 8)
    time.sleep(0.1)
    recorder.close()

    assert annotation_path.exists()
    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    # Exact record_mics.py session-JSON shape.
    assert payload["origin"] == "brazil"
    assert isinstance(payload["origin"], str)
    assert payload["roast_num"] == 7
    assert isinstance(payload["roast_num"], int)
    assert payload["sample_rate"] == 8
    assert payload["mics"] == [
        {"mic_num": 1, "label": "USB PnP", "file": "mic1-brazil-roast7.wav"},
        {"mic_num": 2, "label": "ATR2100x", "file": "mic2-brazil-roast7.wav"},
    ]
    for mic in payload["mics"]:
        assert isinstance(mic["mic_num"], int)
        assert isinstance(mic["label"], str)
        assert isinstance(mic["file"], str)
    # The recording sidecar (milestones / recording-relative alignment) is NOT lost.
    assert (tmp_path / "roast.recording.json").exists()


def test_single_recorder_writes_annotation_session_json(tmp_path: Path) -> None:
    from coffee_roaster_mcp.audio import AnnotationSessionSpec, RoastAudioRecorder

    annotation_path = tmp_path / "ethiopia-roast3-session.json"
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "mic1-ethiopia-roast3.wav",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=4,
        session_id="s",
        device_label="USB PnP",
        annotation_session=AnnotationSessionSpec(
            path=annotation_path,
            origin="ethiopia",
            roast_num=3,
            mic_labels=("USB PnP",),
        ),
    )

    import json

    recorder.begin()
    recorder.write_samples((0.25,) * 4)
    recorder.close()

    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert payload["origin"] == "ethiopia"
    assert payload["roast_num"] == 3
    assert payload["mics"] == [
        {"mic_num": 1, "label": "USB PnP", "file": "mic1-ethiopia-roast3.wav"},
    ]


def test_stop_finalises_recorder_when_worker_blocked(tmp_path: Path) -> None:
    """Bug 2(b): if the capture worker is blocked in a read when stop() is called
    (e.g. the detector's first samples are pending during AST model load), stop()
    finalises the recorder itself so the WAV + sidecars are written, not lost."""
    from threading import Event

    released = Event()
    closed = Event()

    class BlockingInput:
        """read_samples blocks until stop() releases it."""

        def read_samples(self, sample_count: int) -> Sequence[float]:
            released.wait(timeout=2.0)
            return ()

        def close(self) -> None:
            return None

    class CloseTrackingRecorder(RoastAudioRecorder):
        def close(self) -> None:
            closed.set()
            super().close()

    recorder = CloseTrackingRecorder(
        wav_path=tmp_path / "mic1.wav",
        sidecar_path=tmp_path / "roast.recording.json",
        sample_rate=16,
        session_id="s",
    )
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device=None,
            sample_rate=16,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=BlockingInput(),
        recorder=recorder,
    )

    pipeline.start()
    # Give the worker a moment to call begin() and block in read_samples.
    _wait_for(lambda: recorder.started_monotonic_seconds is not None)
    # Stop with a short join timeout while the worker is still blocked → the stop
    # caller must finalise the recorder as a fallback.
    pipeline.stop(timeout_seconds=0.05)

    assert closed.is_set()
    # The mic1 WAV exists and is a valid (0-frame) finalised file, not 0 bytes.
    assert (tmp_path / "mic1.wav").exists()
    assert (tmp_path / "mic1.wav").stat().st_size >= 44
    # The recording sidecar was written despite zero frames.
    assert (tmp_path / "roast.recording.json").exists()

    # Release the still-blocked worker so it exits cleanly; its later close() is a
    # no-op (idempotent), proving no double-write.
    released.set()


def test_wav_writer_zero_frames_finalises_valid_header(tmp_path: Path) -> None:
    """Bug 2(a): a writer that opens but never receives frames still finalises a
    valid WAV (correct header, 0 frames), not a 0-byte file."""
    from coffee_roaster_mcp.audio import _WavStreamWriter  # pyright: ignore[reportPrivateUsage]

    writer = _WavStreamWriter(
        wav_path=tmp_path / "empty.wav",
        device_label="USB PnP",
        sample_rate=16_000,
        flush_sample_threshold=1,
    )
    writer.begin()
    writer.close()

    # After close the WAV is a valid, finalised file (>=44-byte header, 0 frames),
    # not a 0-byte file — even though no samples were ever written.
    assert (tmp_path / "empty.wav").stat().st_size >= 44
    with wave.open(str(tmp_path / "empty.wav"), "rb") as wav_file:
        assert wav_file.getnframes() == 0
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnchannels() == 1


# --- #178: live in-session mic-levels readout -----------------------------


def test_amplitude_to_dbfs_maps_levels_and_silence() -> None:
    """`amplitude_to_dbfs` maps full-scale to 0, half-scale to ~-6, silence to -inf."""
    import math

    assert amplitude_to_dbfs(1.0) == 0.0
    assert amplitude_to_dbfs(2.0) == 0.0  # clamped to full scale
    # Rounded to 2 dp, half-scale is exactly -6.02 dBFS.
    assert amplitude_to_dbfs(0.5) == -6.02
    assert amplitude_to_dbfs(0.0) == -math.inf
    assert amplitude_to_dbfs(-0.1) == -math.inf


def test_capture_snapshot_reports_none_levels_before_any_samples() -> None:
    """Before the worker reads a sample, the levels are `None` (not yet measured)."""
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=FiniteAudioInput(()),
    )
    snapshot = pipeline.snapshot()
    assert snapshot.peak_dbfs is None
    assert snapshot.rms_dbfs is None


def test_capture_snapshot_reports_live_peak_and_rms_levels() -> None:
    """The capture worker surfaces a non-trivial peak/RMS for a real signal (#178)."""
    audio_input = MutableAudioInput((0.5, -0.5, 0.5, -0.5))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    try:
        _wait_for(lambda: pipeline.snapshot().peak_dbfs is not None)
        snapshot = pipeline.snapshot()
    finally:
        pipeline.stop()

    # A constant 0.5 amplitude block is exactly -6.02 dBFS peak AND RMS
    # (the helper rounds to 2 dp).
    assert snapshot.peak_dbfs == -6.02
    assert snapshot.rms_dbfs == -6.02


def test_capture_snapshot_reports_silence_floor_for_a_dead_mic() -> None:
    """A device that opens but delivers zeros reports -inf dBFS, not None (#178)."""
    import math

    audio_input = MutableAudioInput((0.0, 0.0, 0.0, 0.0))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    try:
        _wait_for(lambda: pipeline.snapshot().peak_dbfs is not None)
        snapshot = pipeline.snapshot()
    finally:
        pipeline.stop()

    assert snapshot.peak_dbfs == -math.inf
    assert snapshot.rms_dbfs == -math.inf


def test_capture_levels_reset_on_pipeline_restart() -> None:
    """The level meter clears on restart so a new run does not show stale levels."""
    audio_input = MutableAudioInput((0.5, 0.5, 0.5, 0.5))
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(
            input_device="mock-mic",
            sample_rate=4,
            window_seconds=1.0,
            idle_sleep_seconds=0.001,
        ),
        audio_input=audio_input,
    )

    pipeline.start()
    _wait_for(lambda: pipeline.snapshot().peak_dbfs is not None)
    pipeline.stop()

    pipeline.start()
    try:
        # Immediately after restart, before the worker has read, levels are reset.
        assert pipeline.snapshot().peak_dbfs is None
    finally:
        pipeline.stop()


def test_rolling_level_meter_tracks_recent_window_only() -> None:
    """The meter reflects the recent window, dropping older quieter samples."""
    from coffee_roaster_mcp.audio import _RollingLevelMeter  # pyright: ignore[reportPrivateUsage]

    meter = _RollingLevelMeter(window_sample_count=4)
    assert meter.peak_dbfs is None
    # An empty observe is a no-op: levels stay unmeasured.
    meter.observe(())
    assert meter.peak_dbfs is None
    meter.observe((0.0, 0.0))
    assert meter.peak_dbfs == float("-inf")
    # A later loud block displaces the early silence within the 4-sample window.
    meter.observe((1.0, 1.0, 1.0, 1.0))
    assert meter.peak_dbfs == 0.0
    assert meter.rms_dbfs == 0.0
    meter.reset()
    assert meter.peak_dbfs is None
    assert meter.rms_dbfs is None


def test_rolling_level_meter_levels_is_a_coherent_pair() -> None:
    """`levels` returns the (peak, rms) pair as ONE reference (#178).

    The meter is written by the capture worker outside the pipeline state lock,
    so the snapshot reads the pair via this single accessor to avoid a torn
    (peak-from-one-update, rms-from-another) read. The peak/rms scalar
    properties derive from the same tuple.
    """
    from coffee_roaster_mcp.audio import _RollingLevelMeter  # pyright: ignore[reportPrivateUsage]

    meter = _RollingLevelMeter(window_sample_count=4)
    assert meter.levels is None
    meter.observe((0.5, -0.5, 0.5, -0.5))
    levels = meter.levels
    assert levels is not None
    assert levels == (meter.peak_dbfs, meter.rms_dbfs)
    # A constant half-scale block: peak == rms == -6.02 dBFS (2 dp).
    assert levels == (-6.02, -6.02)
    meter.reset()
    assert meter.levels is None


def test_overflow_tracker_accumulates_within_the_trailing_minute() -> None:
    """Events inside the trailing 60s all count toward the rolling snapshot (#190)."""
    from coffee_roaster_mcp.audio import _OverflowTracker  # pyright: ignore[reportPrivateUsage]

    clock = ClockHarness()
    clock.value = 0.0
    tracker = _OverflowTracker(monotonic_now=lambda: clock.value)

    snapshot = tracker.snapshot()
    assert snapshot.count_last_minute == 0
    assert snapshot.estimated_lost_audio_ms_last_minute == 0.0
    assert snapshot.total_count == 0

    clock.value = 10.0
    tracker.observe_overflow(estimated_lost_audio_ms=250.0)
    clock.value = 20.0
    tracker.observe_overflow(estimated_lost_audio_ms=125.0)

    snapshot = tracker.snapshot()
    assert snapshot.count_last_minute == 2
    assert snapshot.estimated_lost_audio_ms_last_minute == 375.0
    assert snapshot.total_count == 2


def test_overflow_tracker_evicts_events_older_than_sixty_seconds() -> None:
    """Events fall out of the rolling window at 60s, but the lifetime total keeps them (#190)."""
    from coffee_roaster_mcp.audio import _OverflowTracker  # pyright: ignore[reportPrivateUsage]

    clock = ClockHarness()
    clock.value = 0.0
    tracker = _OverflowTracker(monotonic_now=lambda: clock.value)

    tracker.observe_overflow(estimated_lost_audio_ms=100.0)
    clock.value = 61.0
    tracker.observe_overflow(estimated_lost_audio_ms=50.0)

    snapshot = tracker.snapshot()
    # The first event is now 61s old: evicted from the rolling window.
    assert snapshot.count_last_minute == 1
    assert snapshot.estimated_lost_audio_ms_last_minute == 50.0
    # The lifetime total is never evicted.
    assert snapshot.total_count == 2

    # Even with no new events, calling snapshot() re-filters against "now"
    # (read-only: it does not mutate _events, see the class docstring).
    clock.value = 200.0
    snapshot = tracker.snapshot()
    assert snapshot.count_last_minute == 0
    assert snapshot.estimated_lost_audio_ms_last_minute == 0.0
    assert snapshot.total_count == 2


def test_overflow_tracker_snapshot_is_read_only_and_thread_safe() -> None:
    """A reader thread calling snapshot() never races the writer's mutations (#190).

    snapshot() must never mutate `_events`: only `observe_overflow` does, and
    it always runs on the single audio-input read thread. This drives a writer
    thread appending events concurrently with a reader thread repeatedly
    snapshotting, and asserts no exception surfaces and the lifetime total
    matches the writer's actual append count exactly — a mutating snapshot()
    reading/popping the same deque from another thread would risk a missed or
    duplicated eviction under interleaving, this proves there is none.
    """
    from coffee_roaster_mcp.audio import _OverflowTracker  # pyright: ignore[reportPrivateUsage]

    tracker = _OverflowTracker(monotonic_now=time.monotonic)
    write_count = 500
    errors: list[BaseException] = []

    def writer() -> None:
        for _ in range(write_count):
            tracker.observe_overflow(estimated_lost_audio_ms=1.0)

    def reader() -> None:
        try:
            for _ in range(write_count):
                snapshot = tracker.snapshot()
                assert snapshot.count_last_minute >= 0
                assert snapshot.total_count >= 0
        except BaseException as exc:  # noqa: BLE001 - surfaced via the errors list
            errors.append(exc)

    writer_thread = Thread(target=writer)
    reader_thread = Thread(target=reader)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=5.0)
    reader_thread.join(timeout=5.0)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []
    assert tracker.snapshot().total_count == write_count


def test_microphone_audio_input_overflow_snapshot_tracks_overflows() -> None:
    """The mic input exposes overflow diagnostics fed by real overflowed reads (#190)."""
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    clock = ClockHarness()
    clock.value = 0.0
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=FakeSoundDeviceModule(),
        max_consecutive_overflows=10,
        monotonic_now=lambda: clock.value,
    )

    try:
        baseline = audio_input.overflow_snapshot
        assert baseline.count_last_minute == 0
        assert baseline.total_count == 0

        audio_input.read_samples(1)  # opens the stream; clean read at t=0.0
        stream = FakeRawInputStream.created[-1]
        # Advance the clock BEYOND the expected 250ms-per-sample duration
        # before each overflowed read, so the actual-gap-based estimate
        # (#190 review finding) has a genuine gap to measure — a clock that
        # never advances (the old test) always yields the fixed
        # single-read-duration estimate, which is exactly the semantics this
        # fix replaced.
        clock.value = 1.0  # gap since last read: 1000ms
        stream.overflowed = True
        audio_input.read_samples(1)  # one overflowed read of 1 sample @ 4Hz
        clock.value = 2.0  # gap since last read: another 1000ms
        stream.overflowed = True
        audio_input.read_samples(1)  # a second overflowed read

        snapshot = audio_input.overflow_snapshot
        assert snapshot.count_last_minute == 2
        assert snapshot.total_count == 2
        # Each read's expected duration is 250ms (1 sample @ 4Hz); the actual
        # gap was 1000ms, so each overflow's estimate is max(0, 1000-250) =
        # 750ms, for 1500ms total.
        assert snapshot.estimated_lost_audio_ms_last_minute == 1500.0
    finally:
        audio_input.close()


def test_audio_capture_pipeline_snapshot_surfaces_mic_overflow_stats() -> None:
    """AudioCapturePipeline.snapshot() reports the mic input's overflow stats (#190).

    Deliberately does NOT call pipeline.start() (#190 review finding: the
    real reader/processing worker thread(s) would race this test's own
    direct read_samples() calls, making the exact overflow count and
    estimated-lost-ms non-deterministic — since each background read also
    advances _last_read_returned_at and can itself land as a clean or
    overflowed read depending on scheduling). Driving the mic input directly
    and only using the pipeline for snapshot()'s duck-typed getattr lookup
    is enough to prove the plumbing works, deterministically.
    """
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    clock = ClockHarness()
    clock.value = 0.0
    audio_input = MicrophoneAudioInput(
        settings,
        sounddevice_module=FakeSoundDeviceModule(),
        max_consecutive_overflows=10,
        monotonic_now=lambda: clock.value,
    )
    pipeline = AudioCapturePipeline(settings=settings, audio_input=audio_input)

    audio_input.read_samples(1)  # clean read at t=0.0, opens the stream
    stream = FakeRawInputStream.created[-1]
    clock.value = 1.0  # 1000ms gap before the overflowed read
    stream.overflowed = True
    audio_input.read_samples(1)

    snapshot = pipeline.snapshot()
    assert snapshot.overflow_count_last_minute == 1
    assert snapshot.total_overflow_count == 1
    # Expected duration 250ms (1 sample @ 4Hz), actual gap 1000ms:
    # max(0, 1000-250) = 750ms.
    assert snapshot.estimated_lost_audio_ms_last_minute == 750.0


def test_audio_capture_pipeline_snapshot_zero_overflow_for_non_mic_input() -> None:
    """A non-microphone AudioInput (no overflow_snapshot) reports zero stats,
    not an error (#190)."""
    audio_input = FiniteAudioInput((0.1, 0.2, 0.3, 0.4))
    settings = audio_capture_settings_from_config(AudioConfig(sample_rate=4))
    pipeline = AudioCapturePipeline(settings=settings, audio_input=audio_input)

    snapshot = pipeline.snapshot()

    assert snapshot.overflow_count_last_minute == 0
    assert snapshot.estimated_lost_audio_ms_last_minute == 0.0
    assert snapshot.total_overflow_count == 0
