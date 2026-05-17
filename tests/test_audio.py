from __future__ import annotations

import struct
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

import pytest

from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioCapturePipeline,
    AudioCaptureSettings,
    AudioInput,
    MicrophoneAudioInput,
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


def test_microphone_audio_input_reports_overflow() -> None:
    FakeRawInputStream.created.clear()
    settings = audio_capture_settings_from_config(AudioConfig(source="microphone", sample_rate=4))
    audio_input = MicrophoneAudioInput(settings, sounddevice_module=OverflowingSoundDeviceModule())

    try:
        with pytest.raises(AudioCaptureError, match="overflowed"):
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
