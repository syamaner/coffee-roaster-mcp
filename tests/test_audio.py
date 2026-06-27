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
    RoastAudioRecorder,
    WavAudioInput,
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
