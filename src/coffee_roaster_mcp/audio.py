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
        replay_mode: WAV replay mode. `detector_paced` emits windows only when
            the detector drains them, without wall-clock sleeps or queue drops.
        window_seconds: Detector window duration in seconds.
        queue_limit: Maximum detector windows retained for downstream consumers.
        idle_sleep_seconds: Sleep duration when an input read returns no samples.
    """

    input_device: str | None
    sample_rate: int
    source: str = "microphone"
    wav_path: Path | None = None
    replay_mode: str = "realtime"
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
    window_seconds: float | None = None,
    queue_limit: int = DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT,
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS,
) -> AudioCaptureSettings:
    """Build validated audio capture settings from application config."""
    settings = AudioCaptureSettings(
        input_device=config.input_device,
        source=config.source,
        sample_rate=config.sample_rate,
        wav_path=config.wav_path,
        replay_mode=config.replay_mode,
        window_seconds=config.window_seconds if window_seconds is None else window_seconds,
        queue_limit=queue_limit,
        idle_sleep_seconds=idle_sleep_seconds,
    )
    _validate_settings(settings)
    return settings


def build_audio_capture_pipeline(
    config: AudioConfig,
    input_factory: AudioInputFactory | None = None,
    *,
    window_seconds: float | None = None,
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
    if settings.source == "wav" and settings.replay_mode == "detector_paced":
        if settings.wav_path is None:
            raise AudioCaptureError("audio.wav_path must be configured when audio.source is wav.")
        return DetectorPacedWavReplayPipeline(
            settings=settings,
            audio_input=WavAudioInput(settings.wav_path, sample_rate=settings.sample_rate),
            monotonic_now=monotonic_now,
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
        self._wav: Any | None = None
        self._frame_position = 0
        self._channel_count = 0
        self._sample_width = 0
        self._sample_rate = sample_rate
        self._open_wav()

    def _open_wav(self) -> None:
        try:
            wav_file = wave.open(str(self._path), "rb")  # noqa: SIM115 - input owns the handle.
        except (OSError, wave.Error) as exc:
            raise AudioCaptureError(f"Could not open WAV audio source {self._path}: {exc}") from exc

        self._wav = wav_file
        self._channel_count = wav_file.getnchannels()
        self._sample_width = wav_file.getsampwidth()
        wav_sample_rate = wav_file.getframerate()
        if self._channel_count < 1:
            self.close()
            raise AudioCaptureError("WAV audio source must have at least one channel.")
        if self._sample_width not in {1, 2, 3, 4}:
            self.close()
            raise AudioCaptureError("WAV audio source must use 8, 16, 24, or 32-bit PCM samples.")
        if wav_sample_rate != self._sample_rate:
            self.close()
            raise AudioCaptureError(
                "WAV audio source sample rate "
                f"{wav_sample_rate} does not match configured audio.sample_rate "
                f"{self._sample_rate}."
            )
        wav_file.setpos(self._frame_position)

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Read up to `sample_count` mono floating-point samples from the WAV file."""
        if sample_count <= 0:
            return ()
        if self._wav is None:
            self._open_wav()
        if self._wav is None:
            raise AudioCaptureError("WAV audio source is not open.")
        raw_frames = self._wav.readframes(sample_count)
        self._frame_position = self._wav.tell()
        if not raw_frames:
            return ()
        return _decode_pcm_frames(
            raw_frames,
            channel_count=self._channel_count,
            sample_width=self._sample_width,
        )

    def close(self) -> None:
        """Close the underlying WAV file."""
        if self._wav is None:
            return
        with suppress(Exception):
            self._frame_position = self._wav.tell()
        self._wav.close()
        self._wav = None

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
        """Configure a microphone input that opens lazily on first read.

        Args:
            settings: Validated audio capture settings.
            sounddevice_module: Optional injected sounddevice-compatible module for tests.
        """
        _validate_settings(settings)
        self._settings = settings
        self._sounddevice = sounddevice_module or _load_sounddevice()
        self._stream: Any | None = None

    def _ensure_stream(self) -> Any:
        if self._stream is not None:
            return self._stream
        stream: Any | None = None
        try:
            stream_factory = self._sounddevice.RawInputStream
            created_stream = stream_factory(
                samplerate=self._settings.sample_rate,
                device=self._settings.input_device,
                channels=1,
                dtype="float32",
                blocksize=0,
            )
            stream = created_stream
            self._stream = created_stream
            created_stream.start()
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary by platform.
            if stream is not None:
                with suppress(Exception):
                    stream.close()
            self._stream = None
            raise AudioCaptureError(f"Could not open microphone audio source: {exc}") from exc
        return self._stream

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Read up to `sample_count` mono floating-point samples from the microphone."""
        if sample_count <= 0:
            return ()
        stream = self._ensure_stream()
        try:
            raw_data, overflowed = stream.read(sample_count)
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary by platform.
            raise AudioCaptureError(f"Could not read microphone audio source: {exc}") from exc
        if overflowed:
            raise AudioCaptureError("Microphone audio input overflowed.")
        return tuple(float(sample[0]) for sample in struct.iter_unpack("f", bytes(raw_data)))

    def close(self) -> None:
        """Stop and close the microphone stream."""
        stream = self._stream
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()
            self._stream = None

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
        snapshot = self.snapshot()
        _close_audio_input_if_supported(self._audio_input)
        return snapshot

    def close(self) -> None:
        """Stop capture and close the underlying audio input when supported."""
        self.stop()

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


class DetectorPacedWavReplayPipeline(AudioCapturePipeline):
    """Synchronous WAV replay that advances only when detector windows drain."""

    def __init__(
        self,
        *,
        settings: AudioCaptureSettings,
        audio_input: WavAudioInput,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        """Initialize detector-paced WAV replay.

        Args:
            settings: Validated audio capture settings.
            audio_input: WAV input to replay.
            monotonic_now: Optional monotonic clock supplier for tests.
        """
        super().__init__(settings=settings, audio_input=audio_input, monotonic_now=monotonic_now)
        self._running = False
        self._exhausted = False
        self._timeline_start_monotonic_seconds: float | None = None
        self._replay_emitted_window_count = 0

    def start(self) -> AudioCaptureSnapshot:
        """Prepare synchronous replay without starting a background thread."""
        with self._state_lock:
            self._reset_run_state_locked()
            self._running = True
            self._exhausted = False
            self._timeline_start_monotonic_seconds = None
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        """Stop replay and close the WAV input."""
        if timeout_seconds < 0:
            raise AudioCaptureError("timeout_seconds must be >= 0.")
        with self._state_lock:
            self._running = False
        _close_audio_input_if_supported(self._audio_input)
        return self.snapshot()

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        """Read and return detector windows synchronously without queueing drops."""
        if max_windows is not None and max_windows < 0:
            raise AudioCaptureError("max_windows must be >= 0.")
        if max_windows == 0:
            return ()

        windows: list[AudioWindow] = []
        try:
            while self._should_read_next_window(max_windows=max_windows, windows=windows):
                window = self._read_replay_window()
                if window is None:
                    break
                windows.append(window)
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary.
            with self._state_lock:
                self._latest_error = str(exc)
                self._running = False
        return tuple(windows)

    def snapshot(self) -> AudioCaptureSnapshot:
        """Return detector-paced replay status."""
        with self._state_lock:
            return AudioCaptureSnapshot(
                running=self._running and not self._exhausted,
                queued_window_count=0,
                emitted_window_count=self._replay_emitted_window_count,
                dropped_window_count=0,
                latest_error=self._latest_error,
            )

    def _should_read_next_window(
        self,
        *,
        max_windows: int | None,
        windows: list[AudioWindow],
    ) -> bool:
        with self._state_lock:
            if not self._running or self._exhausted or self._latest_error is not None:
                return False
        return max_windows is None or len(windows) < max_windows

    def _read_replay_window(self) -> AudioWindow | None:
        window_sample_count = self._settings.window_sample_count
        while len(self._sample_buffer) < window_sample_count:
            samples = self._audio_input.read_samples(window_sample_count - len(self._sample_buffer))
            if not samples:
                with self._state_lock:
                    self._exhausted = True
                    self._running = False
                return None
            self._sample_buffer.extend(_normalize_sample(sample) for sample in samples)

        window_samples = tuple(self._sample_buffer[:window_sample_count])
        del self._sample_buffer[:window_sample_count]
        with self._state_lock:
            if self._timeline_start_monotonic_seconds is None:
                self._timeline_start_monotonic_seconds = self._monotonic_now()
            sequence_number = self._next_sequence_number
            self._next_sequence_number += 1
            self._replay_emitted_window_count += 1
            started_at_monotonic_seconds = (
                self._timeline_start_monotonic_seconds
                + sequence_number * self._settings.window_seconds
            )

        return AudioWindow(
            sequence_number=sequence_number,
            input_device=self._settings.input_device,
            sample_rate=self._settings.sample_rate,
            started_at_monotonic_seconds=round(started_at_monotonic_seconds, 6),
            duration_seconds=round(window_sample_count / self._settings.sample_rate, 6),
            samples=window_samples,
        )

    def _reset_run_state_locked(self) -> None:
        super()._reset_run_state_locked()
        self._timeline_start_monotonic_seconds = None
        self._replay_emitted_window_count = 0


def _validate_settings(settings: AudioCaptureSettings) -> None:
    if settings.source not in {"microphone", "wav"}:
        raise AudioCaptureError("audio.source must be one of: microphone, wav.")
    if settings.replay_mode not in {"realtime", "detector_paced"}:
        raise AudioCaptureError("audio.replay_mode must be one of: realtime, detector_paced.")
    if settings.source != "wav" and settings.replay_mode != "realtime":
        raise AudioCaptureError("audio.replay_mode can only be detector_paced for WAV sources.")
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
    except (ImportError, OSError) as exc:
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
