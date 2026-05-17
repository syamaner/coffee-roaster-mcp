"""Audio capture windowing for first-crack detection."""

from __future__ import annotations

import importlib
import math
import struct
import time
import wave
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Any, Protocol, Self, cast

from coffee_roaster_mcp.config import AudioConfig

DEFAULT_AUDIO_WINDOW_SECONDS = 1.0
DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT = 8
DEFAULT_AUDIO_IDLE_SLEEP_SECONDS = 0.01


class AudioCaptureError(RuntimeError):
    """Raised when audio capture cannot be configured or run."""


class AudioInput(Protocol):
    """Readable audio input for detector-window capture."""

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Read up to `sample_count` mono floating-point samples."""
        ...


class AudioInputFactory(Protocol):
    """Factory for configured audio inputs."""

    def __call__(self, settings: AudioCaptureSettings) -> AudioInput:
        """Create an audio input for the supplied capture settings."""
        ...


@dataclass(frozen=True)
class AudioCaptureSettings:
    """Runtime audio capture settings.

    Attributes:
        input_device: Optional configured audio input identifier.
        sample_rate: Audio sample rate in Hz.
        source: Configured audio source type.
        wav_path: Optional WAV source path when source is `wav`.
        window_seconds: Detector window duration in seconds.
        queue_limit: Maximum detector windows retained for downstream consumers.
        idle_sleep_seconds: Sleep duration when an input read returns no samples.
    """

    input_device: str | None
    sample_rate: int
    source: str = "microphone"
    wav_path: Path | None = None
    window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS
    queue_limit: int = DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS

    @property
    def window_sample_count(self) -> int:
        """Return the exact number of samples required for one detector window."""
        return max(1, round(self.sample_rate * self.window_seconds))


@dataclass(frozen=True)
class AudioWindow:
    """Complete mono audio window ready for detector inference.

    Attributes:
        sequence_number: Monotonic window sequence number for one pipeline run.
        input_device: Optional configured audio input identifier.
        sample_rate: Audio sample rate in Hz.
        started_at_monotonic_seconds: Monotonic timestamp when the window was emitted.
        duration_seconds: Window duration derived from sample count and sample rate.
        samples: Mono floating-point samples.
    """

    sequence_number: int
    input_device: str | None
    sample_rate: int
    started_at_monotonic_seconds: float
    duration_seconds: float
    samples: tuple[float, ...]


@dataclass(frozen=True)
class AudioCaptureSnapshot:
    """Current audio capture pipeline status.

    Attributes:
        running: Whether the capture worker is currently alive.
        queued_window_count: Detector windows currently waiting for consumption.
        emitted_window_count: Windows successfully queued for detector consumption.
        dropped_window_count: Windows dropped because the detector queue was full.
        latest_error: Last capture error message, if the worker stopped on error.
    """

    running: bool
    queued_window_count: int
    emitted_window_count: int
    dropped_window_count: int
    latest_error: str | None


def audio_capture_settings_from_config(
    config: AudioConfig,
    *,
    window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS,
    queue_limit: int = DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT,
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS,
) -> AudioCaptureSettings:
    """Build validated audio capture settings from application config."""
    settings = AudioCaptureSettings(
        input_device=config.input_device,
        source=config.source,
        sample_rate=config.sample_rate,
        wav_path=config.wav_path,
        window_seconds=window_seconds,
        queue_limit=queue_limit,
        idle_sleep_seconds=idle_sleep_seconds,
    )
    _validate_settings(settings)
    return settings


def build_audio_capture_pipeline(
    config: AudioConfig,
    input_factory: AudioInputFactory | None = None,
    *,
    window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS,
    queue_limit: int = DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT,
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS,
    monotonic_now: Callable[[], float] | None = None,
) -> AudioCapturePipeline:
    """Create an audio capture pipeline from configured audio input settings."""
    resolved_input_factory = input_factory or build_configured_audio_input
    settings = audio_capture_settings_from_config(
        config,
        window_seconds=window_seconds,
        queue_limit=queue_limit,
        idle_sleep_seconds=idle_sleep_seconds,
    )
    return AudioCapturePipeline(
        settings=settings,
        audio_input=resolved_input_factory(settings),
        monotonic_now=monotonic_now,
    )


def build_configured_audio_input(settings: AudioCaptureSettings) -> AudioInput:
    """Create the concrete configured audio input."""
    _validate_settings(settings)
    if settings.source == "microphone":
        return MicrophoneAudioInput(settings)
    if settings.source == "wav":
        if settings.wav_path is None:
            raise AudioCaptureError("audio.wav_path must be configured when audio.source is wav.")
        return WavAudioInput(settings.wav_path, sample_rate=settings.sample_rate)
    raise AudioCaptureError("audio.source must be one of: microphone, wav.")


class WavAudioInput:
    """Read mono float samples from a PCM WAV file."""

    def __init__(self, path: str | Path, *, sample_rate: int) -> None:
        """Open a PCM WAV file for detector replay.

        Args:
            path: Path to a WAV file.
            sample_rate: Expected sample rate in Hz.
        """
        if sample_rate <= 0:
            raise AudioCaptureError("audio.sample_rate must be > 0.")
        self._path = Path(path)
        try:
            self._wav = wave.open(str(self._path), "rb")  # noqa: SIM115 - input owns the handle.
        except (OSError, wave.Error) as exc:
            raise AudioCaptureError(f"Could not open WAV audio source {self._path}: {exc}") from exc

        self._channel_count = self._wav.getnchannels()
        self._sample_width = self._wav.getsampwidth()
        wav_sample_rate = self._wav.getframerate()
        if self._channel_count < 1:
            self.close()
            raise AudioCaptureError("WAV audio source must have at least one channel.")
        if self._sample_width not in {1, 2, 3, 4}:
            self.close()
            raise AudioCaptureError("WAV audio source must use 8, 16, 24, or 32-bit PCM samples.")
        if wav_sample_rate != sample_rate:
            self.close()
            raise AudioCaptureError(
                "WAV audio source sample rate "
                f"{wav_sample_rate} does not match configured audio.sample_rate {sample_rate}."
            )

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Read up to `sample_count` mono floating-point samples from the WAV file."""
        if sample_count <= 0:
            return ()
        raw_frames = self._wav.readframes(sample_count)
        if not raw_frames:
            return ()
        return _decode_pcm_frames(
            raw_frames,
            channel_count=self._channel_count,
            sample_width=self._sample_width,
        )

    def close(self) -> None:
        """Close the underlying WAV file."""
        self._wav.close()

    def __enter__(self) -> Self:
        """Return this WAV input as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the WAV input when leaving a context manager."""
        self.close()


class MicrophoneAudioInput:
    """Read mono float samples from the configured system microphone."""

    def __init__(
        self,
        settings: AudioCaptureSettings,
        *,
        sounddevice_module: Any | None = None,
    ) -> None:
        """Open a PortAudio-backed microphone stream.

        Args:
            settings: Validated audio capture settings.
            sounddevice_module: Optional injected sounddevice-compatible module for tests.
        """
        _validate_settings(settings)
        self._settings = settings
        sounddevice = sounddevice_module or _load_sounddevice()
        stream: Any | None = None
        try:
            stream_factory = sounddevice.RawInputStream
            created_stream = stream_factory(
                samplerate=settings.sample_rate,
                device=settings.input_device,
                channels=1,
                dtype="float32",
                blocksize=0,
            )
            stream = created_stream
            self._stream: Any = created_stream
            created_stream.start()
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary by platform.
            if stream is not None:
                with suppress(Exception):
                    stream.close()
            raise AudioCaptureError(f"Could not open microphone audio source: {exc}") from exc

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Read up to `sample_count` mono floating-point samples from the microphone."""
        if sample_count <= 0:
            return ()
        try:
            raw_data, overflowed = self._stream.read(sample_count)
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary by platform.
            raise AudioCaptureError(f"Could not read microphone audio source: {exc}") from exc
        if overflowed:
            raise AudioCaptureError("Microphone audio input overflowed.")
        return tuple(float(sample[0]) for sample in struct.iter_unpack("f", bytes(raw_data)))

    def close(self) -> None:
        """Stop and close the microphone stream."""
        try:
            self._stream.stop()
        finally:
            self._stream.close()

    def __enter__(self) -> Self:
        """Return this microphone input as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the microphone input when leaving a context manager."""
        self.close()


class AudioCapturePipeline:
    """Background audio capture pipeline that emits detector windows.

    The worker thread owns potentially blocking audio reads. Complete windows
    are offered to a bounded queue without waiting, so a slow detector consumer
    cannot stall the capture worker or any roaster telemetry loop running
    elsewhere in the process.
    """

    def __init__(
        self,
        *,
        settings: AudioCaptureSettings,
        audio_input: AudioInput,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        """Initialize an audio capture pipeline.

        Args:
            settings: Validated audio capture settings.
            audio_input: Configured readable audio source.
            monotonic_now: Optional monotonic clock supplier for tests.
        """
        _validate_settings(settings)
        self._settings = settings
        self._audio_input = audio_input
        self._monotonic_now = monotonic_now or time.monotonic
        self._windows: Queue[AudioWindow] = Queue(maxsize=settings.queue_limit)
        self._sample_buffer: list[float] = []
        self._stop_requested = Event()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._next_sequence_number = 0
        self._emitted_window_count = 0
        self._dropped_window_count = 0
        self._latest_error: str | None = None

    @property
    def settings(self) -> AudioCaptureSettings:
        """Return the immutable capture settings."""
        return self._settings

    def start(self) -> AudioCaptureSnapshot:
        """Start background audio capture and return the current status snapshot."""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise AudioCaptureError("Audio capture pipeline is already running.")
            self._reset_run_state_locked()
            self._stop_requested.clear()
            self._thread = Thread(
                target=self._run_capture_loop,
                name="coffee-roaster-audio-capture",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        """Request capture stop and wait briefly for the worker to finish."""
        if timeout_seconds < 0:
            raise AudioCaptureError("timeout_seconds must be >= 0.")
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        return self.snapshot()

    def close(self) -> None:
        """Stop capture and close the underlying audio input when supported."""
        self.stop()
        _close_audio_input_if_supported(self._audio_input)

    def get_window(
        self,
        *,
        block: bool = False,
        timeout_seconds: float | None = None,
    ) -> AudioWindow | None:
        """Return one queued detector window, if available."""
        try:
            return self._windows.get(block=block, timeout=timeout_seconds)
        except Empty:
            return None

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        """Return all currently queued detector windows without blocking."""
        if max_windows is not None and max_windows < 0:
            raise AudioCaptureError("max_windows must be >= 0.")
        windows: list[AudioWindow] = []
        while max_windows is None or len(windows) < max_windows:
            window = self.get_window()
            if window is None:
                break
            windows.append(window)
        return tuple(windows)

    def snapshot(self) -> AudioCaptureSnapshot:
        """Return a thread-safe capture status snapshot."""
        thread = self._thread
        with self._state_lock:
            return AudioCaptureSnapshot(
                running=thread is not None and thread.is_alive(),
                queued_window_count=self._windows.qsize(),
                emitted_window_count=self._emitted_window_count,
                dropped_window_count=self._dropped_window_count,
                latest_error=self._latest_error,
            )

    def _run_capture_loop(self) -> None:
        try:
            while not self._stop_requested.is_set():
                samples = self._read_next_samples()
                if not samples:
                    time.sleep(self._settings.idle_sleep_seconds)
                    continue
                self._sample_buffer.extend(samples)
                self._emit_complete_windows()
        except Exception as exc:  # noqa: BLE001 - worker stores error for caller inspection.
            with self._state_lock:
                self._latest_error = str(exc)
            self._stop_requested.set()

    def _read_next_samples(self) -> tuple[float, ...]:
        needed_samples = self._settings.window_sample_count - len(self._sample_buffer)
        raw_samples = self._audio_input.read_samples(max(1, needed_samples))
        return tuple(_normalize_sample(sample) for sample in raw_samples)

    def _emit_complete_windows(self) -> None:
        window_sample_count = self._settings.window_sample_count
        while len(self._sample_buffer) >= window_sample_count:
            window_samples = tuple(self._sample_buffer[:window_sample_count])
            del self._sample_buffer[:window_sample_count]
            window = AudioWindow(
                sequence_number=self._next_sequence_number,
                input_device=self._settings.input_device,
                sample_rate=self._settings.sample_rate,
                started_at_monotonic_seconds=self._monotonic_now(),
                duration_seconds=round(window_sample_count / self._settings.sample_rate, 6),
                samples=window_samples,
            )
            self._next_sequence_number += 1
            self._publish_window(window)

    def _publish_window(self, window: AudioWindow) -> None:
        try:
            self._windows.put_nowait(window)
        except Full:
            with self._state_lock:
                self._dropped_window_count += 1
            return
        with self._state_lock:
            self._emitted_window_count += 1

    def _reset_run_state_locked(self) -> None:
        while True:
            try:
                self._windows.get_nowait()
            except Empty:
                break
        self._sample_buffer.clear()
        self._next_sequence_number = 0
        self._emitted_window_count = 0
        self._dropped_window_count = 0
        self._latest_error = None


def _validate_settings(settings: AudioCaptureSettings) -> None:
    if settings.source not in {"microphone", "wav"}:
        raise AudioCaptureError("audio.source must be one of: microphone, wav.")
    if settings.sample_rate <= 0:
        raise AudioCaptureError("audio.sample_rate must be > 0.")
    if not math.isfinite(settings.window_seconds) or settings.window_seconds <= 0:
        raise AudioCaptureError("audio window_seconds must be > 0.")
    if settings.queue_limit < 1:
        raise AudioCaptureError("audio queue_limit must be >= 1.")
    if not math.isfinite(settings.idle_sleep_seconds) or settings.idle_sleep_seconds < 0:
        raise AudioCaptureError("audio idle_sleep_seconds must be >= 0.")


def _normalize_sample(sample: float) -> float:
    normalized = float(sample)
    if not math.isfinite(normalized):
        raise AudioCaptureError("audio samples must be finite numbers.")
    return normalized


def _load_sounddevice() -> Any:
    try:
        return importlib.import_module("sounddevice")
    except ImportError as exc:
        raise AudioCaptureError(
            "Microphone audio input requires the sounddevice package and PortAudio runtime."
        ) from exc


def _decode_pcm_frames(
    raw_frames: bytes,
    *,
    channel_count: int,
    sample_width: int,
) -> tuple[float, ...]:
    sample_values = _decode_pcm_samples(raw_frames, sample_width=sample_width)
    if channel_count == 1:
        return sample_values

    mono_samples: list[float] = []
    frame_count = len(sample_values) // channel_count
    for frame_index in range(frame_count):
        frame_start = frame_index * channel_count
        frame = sample_values[frame_start : frame_start + channel_count]
        mono_samples.append(sum(frame) / channel_count)
    return tuple(mono_samples)


def _decode_pcm_samples(raw_frames: bytes, *, sample_width: int) -> tuple[float, ...]:
    if sample_width == 1:
        return tuple((sample - 128) / 128.0 for sample in raw_frames)
    if sample_width == 2:
        return tuple(sample[0] / 32768.0 for sample in struct.iter_unpack("<h", raw_frames))
    if sample_width == 3:
        return tuple(
            _decode_signed_24bit(raw_frames[index : index + 3]) / 8388608.0
            for index in range(0, len(raw_frames), 3)
        )
    if sample_width == 4:
        return tuple(sample[0] / 2147483648.0 for sample in struct.iter_unpack("<i", raw_frames))
    raise AudioCaptureError("WAV audio source must use 8, 16, 24, or 32-bit PCM samples.")


def _decode_signed_24bit(raw_sample: bytes) -> int:
    if len(raw_sample) != 3:
        raise AudioCaptureError("WAV audio source contained a partial 24-bit sample.")
    value = int.from_bytes(raw_sample, byteorder="little", signed=False)
    if value & 0x800000:
        value -= 0x1000000
    return value


def _close_audio_input_if_supported(audio_input: AudioInput) -> None:
    close = getattr(audio_input, "close", None)
    if close is None:
        return
    close_method = cast(Callable[[], None], close)
    close_method()
