"""Audio capture windowing for first-crack detection."""

from __future__ import annotations

import importlib
import json
import logging
import math
import struct
import time
import wave
from collections.abc import Callable, Mapping, Sequence
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
        overlap: Fraction of each detector window shared with the next window.
        hop_seconds: Optional explicit interval between consecutive windows.
        queue_limit: Maximum detector windows retained for downstream consumers.
        idle_sleep_seconds: Sleep duration when an input read returns no samples.
    """

    input_device: str | None
    sample_rate: int
    source: str = "microphone"
    wav_path: Path | None = None
    replay_mode: str = "realtime"
    window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS
    overlap: float = 0.0
    hop_seconds: float | None = None
    queue_limit: int = DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS

    @property
    def window_sample_count(self) -> int:
        """Return the exact number of samples required for one detector window."""
        return max(1, round(self.sample_rate * self.window_seconds))

    @property
    def hop_sample_count(self) -> int:
        """Return the number of samples to advance between detector windows."""
        hop_seconds = (
            self.window_seconds * (1.0 - self.overlap)
            if self.hop_seconds is None
            else self.hop_seconds
        )
        return max(1, round(self.sample_rate * hop_seconds))

    @property
    def effective_hop_seconds(self) -> float:
        """Return the effective interval between detector windows."""
        return self.hop_sample_count / self.sample_rate


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
        overlap=config.overlap,
        hop_seconds=config.hop_seconds,
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
    recorder: RoastRecorder | None = None,
) -> AudioCapturePipeline:
    """Create an audio capture pipeline from configured audio input settings.

    Args:
        config: Audio capture configuration.
        input_factory: Optional configured-input factory override.
        window_seconds: Optional detector window override.
        queue_limit: Detector window queue limit.
        idle_sleep_seconds: Idle sleep when no samples are available.
        monotonic_now: Optional monotonic clock supplier for tests.
        recorder: Optional roast audio recorder teed onto the detector stream
            (#176). Detector-paced WAV replay never records, since it has no live
            capture stream to tee.

    Returns:
        A configured capture pipeline.
    """
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
        recorder=recorder,
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


_LOGGER = logging.getLogger(__name__)

#: A PortAudio input overflow is transient (the device dropped some input because
#: a read missed its deadline, e.g. under CPU contention); the read still returns
#: the samples it captured. Tolerate it and continue rather than killing capture,
#: faulting only when overflows persist for this many *consecutive* reads, which
#: signals the device genuinely cannot keep up rather than a momentary hiccup.
_DEFAULT_MAX_CONSECUTIVE_OVERFLOWS = 30


class MicrophoneAudioInput:
    """Read mono float samples from the configured system microphone."""

    def __init__(
        self,
        settings: AudioCaptureSettings,
        *,
        sounddevice_module: Any | None = None,
        max_consecutive_overflows: int = _DEFAULT_MAX_CONSECUTIVE_OVERFLOWS,
    ) -> None:
        """Configure a microphone input that opens lazily on first read.

        Args:
            settings: Validated audio capture settings.
            sounddevice_module: Optional injected sounddevice-compatible module for tests.
            max_consecutive_overflows: Consecutive input overflows tolerated before
                capture faults. A transient overflow is logged and the captured
                samples are still returned; only a sustained run (the device cannot
                keep up at all) raises ``AudioCaptureError``.
        """
        _validate_settings(settings)
        self._settings = settings
        self._sounddevice = sounddevice_module or _load_sounddevice()
        self._stream: Any | None = None
        self._max_consecutive_overflows = max_consecutive_overflows
        self._consecutive_overflows = 0
        self._close_lock = Lock()

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
            # A genuine read failure (device gone, backend error) is still fatal.
            raise AudioCaptureError(f"Could not read microphone audio source: {exc}") from exc
        if overflowed:
            # Transient: the device dropped some input but `raw_data` still holds
            # the samples it captured. Log, count, and keep going rather than
            # killing capture for the whole roast; only a sustained run of
            # consecutive overflows (the device cannot keep up at all) faults.
            self._consecutive_overflows += 1
            _LOGGER.warning(
                "Microphone audio input overflowed (%d consecutive); continuing.",
                self._consecutive_overflows,
            )
            if self._consecutive_overflows >= self._max_consecutive_overflows:
                raise AudioCaptureError(
                    "Microphone audio input overflowed on "
                    f"{self._consecutive_overflows} consecutive reads; the device "
                    "cannot keep up with the configured sample rate."
                )
        else:
            self._consecutive_overflows = 0
        return tuple(float(sample[0]) for sample in struct.iter_unpack("f", bytes(raw_data)))

    def close(self) -> None:
        """Stop and close the microphone stream.

        Idempotent and thread-safe: closing the underlying PortAudio stream frees
        its native ring buffer, so a repeated close (capture worker plus stop
        caller) must never race into a double free. Callers must ensure no read is
        in flight on another thread before closing; the capture pipeline
        guarantees this by closing only from the worker thread that owns reads.
        """
        with self._close_lock:
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


#: 16-bit signed PCM is the recorder's on-disk format: it is the Label-Studio /
#: training-corpus convention reused from coffee-first-crack-detection, keeps the
#: WAV roughly half the size of 32-bit float, and the detector samples are already
#: in the [-1, 1] float range so the conversion is a single multiply per sample.
_RECORDER_SAMPLE_WIDTH_BYTES = 2
_RECORDER_PCM16_MAX = 32767


class RecorderMilestonesProvider(Protocol):
    """Supplies roast milestone offsets for the recording sidecar at close."""

    def __call__(self) -> Mapping[str, float | None]:
        """Return milestone name to recording-relative seconds (or `None`)."""
        ...


class RoastRecorder(Protocol):
    """Recorder interface driven by the capture pipeline.

    The pipeline calls ``begin`` once at capture start, ``write_samples`` for
    every block of detector samples (the teed stream), and ``close`` once at
    stop or fault. Single-device and multi-device recorders both satisfy it.
    """

    @property
    def started_monotonic_seconds(self) -> float | None:
        """Return the absolute monotonic instant captured at recording start."""
        ...

    def begin(self) -> None:
        """Open writers and capture the recording-start time."""
        ...

    def write_samples(self, samples: Sequence[float]) -> None:
        """Append the teed detector samples to the detector-device WAV."""
        ...

    def close(self) -> None:
        """Flush, finalize every WAV, and write the sidecar."""
        ...


def device_label_to_filename(label: str) -> str:
    """Derive a filesystem-safe WAV stem fragment from a device label.

    Lower-cases the label and replaces every run of non-alphanumeric characters
    with a single hyphen, so ``"USB PnP"`` becomes ``"usb-pnp"`` and the WAV is
    ``roast.usb-pnp.wav``. An empty result falls back to ``"device"``.

    Args:
        label: Device-name substring from `recording.devices`.

    Returns:
        A hyphenated, lower-cased label fragment safe for a filename.
    """
    slug_chars = [char.lower() if char.isalnum() else "-" for char in label]
    slug = "".join(slug_chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "device"


class _WavStreamWriter:
    """Buffered 16-bit mono PCM WAV writer for one recording stream.

    Writes are buffered in memory and flushed to disk in blocks so the calling
    thread is not stalled by a per-sample syscall. One writer owns one WAV file
    and is driven by a single thread (the detector capture worker for the teed
    stream, or that stream's own daemon thread for an independent device).
    """

    def __init__(
        self,
        *,
        wav_path: Path,
        device_label: str | None,
        sample_rate: int,
        flush_sample_threshold: int,
    ) -> None:
        """Configure one WAV stream writer.

        Args:
            wav_path: Destination WAV path. Parent directories are created.
            device_label: Configured device selector for this stream, or `None`
                for the system-default detector device.
            sample_rate: WAV sample rate in Hz.
            flush_sample_threshold: Buffered sample count that triggers a flush.

        Raises:
            AudioCaptureError: If sample_rate or the flush threshold is invalid.
        """
        if sample_rate <= 0:
            raise AudioCaptureError("recording sample_rate must be > 0.")
        if flush_sample_threshold < 1:
            raise AudioCaptureError("recording flush_sample_threshold must be >= 1.")
        self._wav_path = Path(wav_path)
        self._device_label = device_label
        self._sample_rate = sample_rate
        self._flush_sample_threshold = flush_sample_threshold
        self._wav: wave.Wave_write | None = None
        self._pending: list[float] = []
        self._frames_written = 0
        self._closed = False

    @property
    def wav_path(self) -> Path:
        """Return the destination WAV path."""
        return self._wav_path

    @property
    def device_label(self) -> str | None:
        """Return the configured device selector for this stream."""
        return self._device_label

    @property
    def sample_rate(self) -> int:
        """Return the WAV sample rate in Hz."""
        return self._sample_rate

    @property
    def frames_written(self) -> int:
        """Return the number of audio frames written to the WAV so far."""
        return self._frames_written

    @property
    def is_open(self) -> bool:
        """Return whether the WAV writer is currently open."""
        return self._wav is not None

    def begin(self) -> None:
        """Open the WAV writer.

        Raises:
            AudioCaptureError: If the WAV file cannot be opened.
        """
        if self._wav is not None or self._closed:
            return
        self._wav_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            wav_file = wave.open(  # noqa: SIM115 - writer owns the handle until close().
                str(self._wav_path), "wb"
            )
        except (OSError, wave.Error) as exc:
            raise AudioCaptureError(
                f"Could not open roast recording WAV {self._wav_path}: {exc}"
            ) from exc
        wav_file.setnchannels(1)
        wav_file.setsampwidth(_RECORDER_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(self._sample_rate)
        self._wav = wav_file

    def write_samples(self, samples: Sequence[float]) -> None:
        """Append mono float samples to the buffer, flushing on threshold."""
        if self._wav is None or self._closed or not samples:
            return
        self._pending.extend(float(sample) for sample in samples)
        if len(self._pending) >= self._flush_sample_threshold:
            self._flush()

    def close(self) -> None:
        """Flush remaining samples and finalize the WAV. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._wav is None:
            return
        try:
            self._flush()
        finally:
            with suppress(Exception):
                self._wav.close()
            self._wav = None

    def sidecar_entry(self) -> dict[str, object]:
        """Return this stream's descriptor for the recording sidecar JSON."""
        return {
            "device": self._device_label,
            "wav_filename": self._wav_path.name,
            "sample_rate": self._sample_rate,
            "channels": 1,
            "sample_width_bytes": _RECORDER_SAMPLE_WIDTH_BYTES,
            "frame_count": self._frames_written,
            "duration_seconds": round(self._frames_written / self._sample_rate, 6),
        }

    def _flush(self) -> None:
        if self._wav is None or not self._pending:
            return
        frames = bytearray()
        for sample in self._pending:
            clamped = -1.0 if sample < -1.0 else (1.0 if sample > 1.0 else sample)
            frames += struct.pack("<h", int(round(clamped * _RECORDER_PCM16_MAX)))
        self._wav.writeframes(bytes(frames))
        self._frames_written += len(self._pending)
        self._pending.clear()


def _write_recording_sidecar(
    *,
    sidecar_path: Path,
    session_id: str,
    started_monotonic_seconds: float | None,
    streams: Sequence[_WavStreamWriter],
    milestones_provider: RecorderMilestonesProvider | None,
) -> None:
    """Write the per-roast recording sidecar JSON listing every WAV stream."""
    milestones: Mapping[str, float | None] = {}
    if milestones_provider is not None:
        with suppress(Exception):
            milestones = milestones_provider()
    stream_entries = [stream.sidecar_entry() for stream in streams]
    sidecar: dict[str, object] = {
        "schema_version": 2,
        "session_id": session_id,
        "recording_started_monotonic_seconds": started_monotonic_seconds,
        "milestones": {key: milestones.get(key) for key in milestones},
        "streams": stream_entries,
    }
    if stream_entries:
        # Back-compat convenience: surface the FIRST (detector) stream's fields at
        # the top level so v1 sidecar consumers keep reading without the `streams`
        # list. The detector stream is always index 0.
        primary = stream_entries[0]
        sidecar["wav_filename"] = primary["wav_filename"]
        sidecar["sample_rate"] = primary["sample_rate"]
        sidecar["channels"] = primary["channels"]
        sidecar["sample_width_bytes"] = primary["sample_width_bytes"]
        sidecar["frame_count"] = primary["frame_count"]
        sidecar["duration_seconds"] = primary["duration_seconds"]
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with sidecar_path.open("w", encoding="utf-8") as output:
        json.dump(sidecar, output, sort_keys=True, indent=2)
        output.write("\n")


class _IndependentCaptureStream:
    """Capture one additional device on its own thread into its own WAV.

    Unlike the detector's teed stream, this opens a SEPARATE `MicrophoneAudioInput`
    (its own PortAudio stream) and reads it in its own daemon thread. The stream
    runs on an independent clock — it is NOT sample-locked to the detector or to
    any other independent stream — which is acceptable for FC training data
    (operator decision, option A). A read/open error on this stream is logged and
    drops only this stream: detection and every other WAV keep running.
    """

    def __init__(
        self,
        *,
        writer: _WavStreamWriter,
        audio_input: AudioInput,
        read_sample_count: int,
        idle_sleep_seconds: float,
    ) -> None:
        """Configure one independent capture stream.

        Args:
            writer: WAV writer for this device's samples.
            audio_input: Freshly-opened audio input for this device.
            read_sample_count: Samples to request per read.
            idle_sleep_seconds: Sleep when a read returns no samples.
        """
        self._writer = writer
        self._audio_input = audio_input
        self._read_sample_count = max(1, read_sample_count)
        self._idle_sleep_seconds = max(0.0, idle_sleep_seconds)
        self._stop_requested = Event()
        self._thread: Thread | None = None

    @property
    def writer(self) -> _WavStreamWriter:
        """Return this stream's WAV writer."""
        return self._writer

    def start(self) -> None:
        """Open the WAV and spawn the capture thread."""
        self._writer.begin()
        self._stop_requested.clear()
        thread = Thread(
            target=self._run,
            name=f"coffee-roaster-recording-{self._writer.wav_path.stem}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout_seconds: float = 1.0) -> None:
        """Signal the capture thread to stop and join it briefly."""
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)

    def _run(self) -> None:
        try:
            while not self._stop_requested.is_set():
                samples = self._audio_input.read_samples(self._read_sample_count)
                if not samples:
                    time.sleep(self._idle_sleep_seconds)
                    continue
                self._writer.write_samples(samples)
        except Exception as exc:  # noqa: BLE001 - one stream failing must not kill others.
            _LOGGER.warning(
                "Independent recording stream %s failed; dropping it: %s",
                self._writer.wav_path.name,
                exc,
            )
        finally:
            # Close the audio input on the same thread that owns its reads, so its
            # native PortAudio ring buffer is never freed mid-read (the same
            # discipline the detector capture loop uses).
            _close_audio_input_if_supported(self._audio_input)


class RoastAudioRecorder:
    """Tee the detector's mono capture stream into one per-roast WAV (#176).

    This recorder is owned by the MCP audio pipeline, which already owns the
    audio device for detection. It does NOT open a second stream: the capture
    loop hands it the same float samples the detector consumes (the verified
    non-disturbing tee point), so there is no extra device contention.

    This is the single-stream recorder (the `recording.devices` unset fallback,
    and the teed-only case). Multi-device capture (option A) is handled by
    :class:`MultiDeviceRoastRecorder`, which composes this teed stream with extra
    independently-captured device streams.

    Writes are buffered in memory and flushed to disk in blocks so the capture
    worker is not stalled by a per-sample syscall; the actual disk write still
    happens on the capture thread, which is acceptable for the mono stream on the
    Pi-CPU budget (D27).

    The recorder is not internally locked: the capture pipeline drives all of
    ``begin``, ``write_samples``, and ``close`` from its single worker thread, so
    no two of them run concurrently.
    """

    def __init__(
        self,
        *,
        wav_path: Path,
        sidecar_path: Path,
        sample_rate: int,
        session_id: str,
        device_label: str | None = None,
        milestones_provider: RecorderMilestonesProvider | None = None,
        monotonic_now: Callable[[], float] | None = None,
        flush_sample_threshold: int = 16_000,
    ) -> None:
        """Configure a per-roast single-stream audio recorder.

        Args:
            wav_path: Destination WAV path. Parent directories are created.
            sidecar_path: Destination sidecar JSON path written at close.
            sample_rate: WAV sample rate in Hz.
            session_id: Roast session identifier recorded in the sidecar.
            device_label: Configured detector device selector, recorded in the
                sidecar stream descriptor.
            milestones_provider: Optional callable returning roast milestone
                offsets in recording-relative seconds, evaluated at close.
            monotonic_now: Optional monotonic clock supplier for tests.
            flush_sample_threshold: Buffered sample count that triggers a disk
                flush. Larger values reduce syscalls at the cost of memory.

        Raises:
            AudioCaptureError: If sample_rate or the flush threshold is invalid.
        """
        self._sidecar_path = Path(sidecar_path)
        self._session_id = session_id
        self._milestones_provider = milestones_provider
        self._monotonic_now = monotonic_now or time.monotonic
        self._writer = _WavStreamWriter(
            wav_path=wav_path,
            device_label=device_label,
            sample_rate=sample_rate,
            flush_sample_threshold=flush_sample_threshold,
        )
        self._started_monotonic_seconds: float | None = None
        self._closed = False

    @property
    def wav_path(self) -> Path:
        """Return the destination WAV path."""
        return self._writer.wav_path

    @property
    def sidecar_path(self) -> Path:
        """Return the destination sidecar JSON path."""
        return self._sidecar_path

    @property
    def sample_rate(self) -> int:
        """Return the WAV sample rate in Hz."""
        return self._writer.sample_rate

    @property
    def frames_written(self) -> int:
        """Return the number of audio frames written to the WAV so far."""
        return self._writer.frames_written

    @property
    def started_monotonic_seconds(self) -> float | None:
        """Return the absolute monotonic timestamp captured at recording start."""
        return self._started_monotonic_seconds

    def begin(self) -> None:
        """Open the WAV writer and capture the recording-start monotonic time."""
        if self._writer.is_open or self._closed:
            return
        self._started_monotonic_seconds = self._monotonic_now()
        self._writer.begin()

    def write_samples(self, samples: Sequence[float]) -> None:
        """Append the detector's mono float samples to the recording buffer."""
        self._writer.write_samples(samples)

    def close(self) -> None:
        """Flush remaining samples, finalize the WAV, and write the sidecar JSON."""
        if self._closed:
            return
        self._closed = True
        if self._started_monotonic_seconds is None:
            # begin() was never called: nothing to finalize and no sidecar.
            return
        self._writer.close()
        _write_recording_sidecar(
            sidecar_path=self._sidecar_path,
            session_id=self._session_id,
            started_monotonic_seconds=self._started_monotonic_seconds,
            streams=(self._writer,),
            milestones_provider=self._milestones_provider,
        )


@dataclass(frozen=True)
class AdditionalRecordingDevice:
    """One additional, independently-captured recording device (#176 option A).

    Attributes:
        device_label: Device-name substring (matched like `audio.input_device`).
        wav_path: Destination WAV path for this device's independent stream.
        sample_rate: Capture/WAV sample rate in Hz for this device.
    """

    device_label: str
    wav_path: Path
    sample_rate: int


#: Factory that opens a fresh audio input for an independent recording device.
#: Injected in tests so multi-device capture is exercised without PortAudio.
AdditionalAudioInputFactory = Callable[[AdditionalRecordingDevice], AudioInput]


def _build_additional_microphone_input(device: AdditionalRecordingDevice) -> AudioInput:
    """Open a fresh mono microphone input for an additional recording device."""
    return MicrophoneAudioInput(
        AudioCaptureSettings(
            input_device=device.device_label,
            sample_rate=device.sample_rate,
        )
    )


class MultiDeviceRoastRecorder:
    """Record several independent USB-mic streams per roast — one WAV each (#176).

    Option A (operator decision): instead of an aggregate device or JACK, each
    configured device is captured as its OWN stream. By convention the FIRST
    device is the FC detector's device, so its WAV is TEED from the existing
    detector capture stream (no second open, no contention) exactly like
    :class:`RoastAudioRecorder`. Each ADDITIONAL device is opened as its own
    independent :class:`MicrophoneAudioInput` running in its own daemon thread,
    written to its own WAV.

    The streams run on INDEPENDENT clocks — they are NOT sample-locked to each
    other or to the detector — and may drift over a long roast. That is expected
    and acceptable for FC training data; the per-stream WAVs are labelled and
    each carries its own sample rate in the sidecar, and the recording-start
    monotonic timestamp anchors them to the roast clock for offline alignment.

    Fail-soft per stream: opening or reading an additional device that errors
    logs and drops only THAT stream; detection (the teed stream) and every other
    additional WAV keep recording. The teed stream shares the detector capture
    worker, so it cannot contend with itself.

    The pipeline drives ``begin``/``write_samples``/``close`` from its single
    worker thread; only the additional streams add threads, and each owns its own
    writer, so no two threads write the same WAV.
    """

    def __init__(
        self,
        *,
        detector_wav_path: Path,
        detector_device_label: str | None,
        sidecar_path: Path,
        sample_rate: int,
        session_id: str,
        additional_devices: Sequence[AdditionalRecordingDevice] = (),
        milestones_provider: RecorderMilestonesProvider | None = None,
        monotonic_now: Callable[[], float] | None = None,
        flush_sample_threshold: int = 16_000,
        additional_input_factory: AdditionalAudioInputFactory | None = None,
        additional_read_seconds: float = 0.25,
        idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS,
        stop_timeout_seconds: float = 1.0,
    ) -> None:
        """Configure a multi-device per-roast recorder.

        Args:
            detector_wav_path: WAV path for the teed detector-device stream.
            detector_device_label: Detector device selector for the sidecar.
            sidecar_path: Destination sidecar JSON path written at close.
            sample_rate: WAV sample rate for the teed detector stream.
            session_id: Roast session identifier recorded in the sidecar.
            additional_devices: Independently-captured devices (one WAV each).
            milestones_provider: Optional roast-milestone offsets supplier.
            monotonic_now: Optional monotonic clock supplier for tests.
            flush_sample_threshold: Buffered sample count that triggers a flush.
            additional_input_factory: Opens a fresh input per additional device;
                defaults to the real microphone input. Injected in tests.
            additional_read_seconds: Read-block duration for additional streams.
            idle_sleep_seconds: Sleep when an additional read returns no samples.
            stop_timeout_seconds: Per-stream join timeout at close.

        Raises:
            AudioCaptureError: If a sample rate or the flush threshold is invalid.
        """
        self._sidecar_path = Path(sidecar_path)
        self._session_id = session_id
        self._milestones_provider = milestones_provider
        self._monotonic_now = monotonic_now or time.monotonic
        self._stop_timeout_seconds = stop_timeout_seconds
        self._started_monotonic_seconds: float | None = None
        self._closed = False
        self._detector_writer = _WavStreamWriter(
            wav_path=detector_wav_path,
            device_label=detector_device_label,
            sample_rate=sample_rate,
            flush_sample_threshold=flush_sample_threshold,
        )
        factory = additional_input_factory or _build_additional_microphone_input
        self._additional_streams: list[_IndependentCaptureStream] = []
        for device in additional_devices:
            writer = _WavStreamWriter(
                wav_path=device.wav_path,
                device_label=device.device_label,
                sample_rate=device.sample_rate,
                flush_sample_threshold=flush_sample_threshold,
            )
            self._additional_streams.append(
                _IndependentCaptureStream(
                    writer=writer,
                    audio_input=_LazyAdditionalAudioInput(device, factory),
                    read_sample_count=max(1, round(device.sample_rate * additional_read_seconds)),
                    idle_sleep_seconds=idle_sleep_seconds,
                )
            )

    @property
    def wav_path(self) -> Path:
        """Return the teed detector-stream WAV path."""
        return self._detector_writer.wav_path

    @property
    def sidecar_path(self) -> Path:
        """Return the destination sidecar JSON path."""
        return self._sidecar_path

    @property
    def started_monotonic_seconds(self) -> float | None:
        """Return the absolute monotonic timestamp captured at recording start."""
        return self._started_monotonic_seconds

    @property
    def additional_wav_paths(self) -> tuple[Path, ...]:
        """Return the WAV paths for the independently-captured devices."""
        return tuple(stream.writer.wav_path for stream in self._additional_streams)

    def begin(self) -> None:
        """Open the teed WAV and start every independent capture stream.

        Starting an additional stream that fails to open drops only that stream;
        the teed detector stream and the other additional streams continue.
        """
        if self._closed or self._started_monotonic_seconds is not None:
            return
        self._started_monotonic_seconds = self._monotonic_now()
        self._detector_writer.begin()
        for stream in self._additional_streams:
            try:
                stream.start()
            except Exception as exc:  # noqa: BLE001 - one stream must not block the others.
                _LOGGER.warning(
                    "Could not start independent recording stream %s; dropping it: %s",
                    stream.writer.wav_path.name,
                    exc,
                )

    def write_samples(self, samples: Sequence[float]) -> None:
        """Append the teed detector samples to the detector-device WAV."""
        self._detector_writer.write_samples(samples)

    def close(self) -> None:
        """Stop additional streams, finalize every WAV, and write the sidecar."""
        if self._closed:
            return
        self._closed = True
        if self._started_monotonic_seconds is None:
            return
        for stream in self._additional_streams:
            with suppress(Exception):
                stream.stop(timeout_seconds=self._stop_timeout_seconds)
        self._detector_writer.close()
        for stream in self._additional_streams:
            with suppress(Exception):
                stream.writer.close()
        streams = [self._detector_writer, *(s.writer for s in self._additional_streams)]
        _write_recording_sidecar(
            sidecar_path=self._sidecar_path,
            session_id=self._session_id,
            started_monotonic_seconds=self._started_monotonic_seconds,
            streams=streams,
            milestones_provider=self._milestones_provider,
        )


@dataclass(frozen=True)
class IndependentCaptureResult:
    """Outcome of one device in an independent multi-device capture.

    Attributes:
        device_label: Configured device-name substring.
        wav_path: WAV file written for this device.
        sample_rate: Capture/WAV sample rate in Hz.
        frame_count: Frames written for this device.
    """

    device_label: str
    wav_path: Path
    sample_rate: int
    frame_count: int


def capture_devices_independently(
    devices: Sequence[AdditionalRecordingDevice],
    *,
    record_seconds: float,
    sidecar_path: Path,
    session_id: str,
    input_factory: AdditionalAudioInputFactory | None = None,
    sleep: Callable[[float], None] | None = None,
    flush_sample_threshold: int | None = None,
    read_seconds: float = 0.1,
    idle_sleep_seconds: float = DEFAULT_AUDIO_IDLE_SLEEP_SECONDS,
    stop_timeout_seconds: float = 1.0,
) -> list[IndependentCaptureResult]:
    """Capture every device as its own independent stream for `record_seconds`.

    Each device is opened on its own daemon thread (no teeing, no detector), one
    WAV each, and a recording sidecar listing them is written. A device that
    fails to open/read is dropped (logged on its thread) and reports a zero-frame
    result; the others keep capturing. This is the roast-free building block used
    by the recording smoke test.

    Args:
        devices: Devices to capture independently.
        record_seconds: How long to capture from every device.
        sidecar_path: Sidecar JSON path written after capture.
        session_id: Identifier recorded in the sidecar.
        input_factory: Opens a fresh input per device; defaults to the real
            microphone input. Injected in tests.
        sleep: Sleep function for the capture window; defaults to `time.sleep`.
        flush_sample_threshold: Buffer flush threshold; defaults to one read
            block per device.
        read_seconds: Per-read block duration.
        idle_sleep_seconds: Sleep when a read returns no samples.
        stop_timeout_seconds: Per-stream join timeout.

    Returns:
        One :class:`IndependentCaptureResult` per device, in input order.
    """
    factory = input_factory or _build_additional_microphone_input
    sleep_fn = sleep or time.sleep
    writers = [
        _WavStreamWriter(
            wav_path=device.wav_path,
            device_label=device.device_label,
            sample_rate=device.sample_rate,
            flush_sample_threshold=(
                flush_sample_threshold
                if flush_sample_threshold is not None
                else max(1, round(device.sample_rate * read_seconds))
            ),
        )
        for device in devices
    ]
    streams = [
        _IndependentCaptureStream(
            writer=writer,
            audio_input=_LazyAdditionalAudioInput(device, factory),
            read_sample_count=max(1, round(device.sample_rate * read_seconds)),
            idle_sleep_seconds=idle_sleep_seconds,
        )
        for device, writer in zip(devices, writers, strict=True)
    ]
    for stream in streams:
        stream.start()
    sleep_fn(record_seconds)
    for stream in streams:
        with suppress(Exception):
            stream.stop(timeout_seconds=stop_timeout_seconds)
    for writer in writers:
        with suppress(Exception):
            writer.close()
    _write_recording_sidecar(
        sidecar_path=sidecar_path,
        session_id=session_id,
        started_monotonic_seconds=None,
        streams=writers,
        milestones_provider=None,
    )
    return [
        IndependentCaptureResult(
            device_label=device.device_label,
            wav_path=writer.wav_path,
            sample_rate=device.sample_rate,
            frame_count=writer.frames_written,
        )
        for device, writer in zip(devices, writers, strict=True)
    ]


class _LazyAdditionalAudioInput:
    """Open an additional device's audio input lazily on its capture thread.

    The factory call happens inside the capture thread's first ``read_samples``
    so a slow or failing device open does not block ``begin`` (and thus the
    roast start) on the caller thread; an open error is raised on the stream's
    own thread, where it is caught and drops only that stream.
    """

    def __init__(
        self,
        device: AdditionalRecordingDevice,
        factory: AdditionalAudioInputFactory,
    ) -> None:
        """Wrap one additional device with a lazily-opened input.

        Args:
            device: The additional recording device to open.
            factory: Factory that opens a fresh input for the device.
        """
        self._device = device
        self._factory = factory
        self._input: AudioInput | None = None

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Open the input on first call, then read up to `sample_count` samples."""
        if self._input is None:
            self._input = self._factory(self._device)
        return self._input.read_samples(sample_count)

    def close(self) -> None:
        """Close the underlying input if it was opened."""
        if self._input is not None:
            _close_audio_input_if_supported(self._input)
            self._input = None


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
        recorder: RoastRecorder | None = None,
    ) -> None:
        """Initialize an audio capture pipeline.

        Args:
            settings: Validated audio capture settings.
            audio_input: Configured readable audio source.
            monotonic_now: Optional monotonic clock supplier for tests.
            recorder: Optional roast audio recorder (#176). When supplied, the
                capture worker tees the same float samples the detector consumes
                into a per-roast WAV without opening a second device stream.
        """
        _validate_settings(settings)
        self._settings = settings
        self._audio_input = audio_input
        self._monotonic_now = monotonic_now or time.monotonic
        self._recorder = recorder
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
        """Request capture stop and wait briefly for the worker to finish.

        The audio input is closed by the worker thread itself once its read loop
        exits, so its native (PortAudio) stream is never freed while a read is in
        flight on the worker. This method only closes the input directly as a
        fallback when no worker thread is (still) running — for example, when
        ``start`` failed or the worker already finished without closing. Closing
        from this caller thread while the worker is still alive would free the
        ring buffer under an in-flight ``read`` and crash the process.

        Args:
            timeout_seconds: Maximum time to wait for the worker thread to exit.

        Returns:
            The capture status snapshot taken after the stop request.

        Raises:
            AudioCaptureError: If ``timeout_seconds`` is negative.
        """
        if timeout_seconds < 0:
            raise AudioCaptureError("timeout_seconds must be >= 0.")
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        snapshot = self.snapshot()
        if thread is None:
            # No worker thread ever ran (e.g. start() failed before spawning the
            # worker): close the input here as a fallback. Whenever a worker DID
            # run it closes the input itself in its loop finally — on the thread
            # that owns reads — whether it has already exited or is still draining
            # a blocking read. So stop() must not also close it: that would double
            # free / free under an in-flight read. (close() is idempotent anyway,
            # but correctness here does not depend on that.)
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
        # Recording is strictly best-effort: a recorder error (including a
        # recording-START failure here) must NEVER kill detection. begin() runs
        # outside the main try below, so it gets its own fail-soft guard: on
        # failure the recorder is dropped and capture/detection continue.
        if self._recorder is not None:
            try:
                self._recorder.begin()
            except Exception as exc:  # noqa: BLE001 - recording is best effort.
                _LOGGER.warning("Roast audio recording start failed; continuing: %s", exc)
                self._disable_recorder()
        try:
            while not self._stop_requested.is_set():
                samples = self._read_next_samples()
                if not samples:
                    time.sleep(self._settings.idle_sleep_seconds)
                    continue
                self._sample_buffer.extend(samples)
                # Tee the SAME samples the detector consumes into the recording
                # WAV right after the buffer extend (#176). This is the verified
                # non-disturbing tee point: the detector still windows the buffer
                # below, untouched. Recording failures must never kill detection,
                # so a recorder error is logged and capture continues.
                if self._recorder is not None:
                    try:
                        self._recorder.write_samples(samples)
                    except Exception as exc:  # noqa: BLE001 - recording is best effort.
                        _LOGGER.warning("Roast audio recording write failed; continuing: %s", exc)
                        # Finalize what was captured (flush the WAV + write the
                        # sidecar) BEFORE dropping the recorder, so the partial
                        # recording is not leaked or lost.
                        self._disable_recorder()
                self._emit_complete_windows()
        except Exception as exc:  # noqa: BLE001 - worker stores error for caller inspection.
            with self._state_lock:
                self._latest_error = str(exc)
            self._stop_requested.set()
        finally:
            # Close the audio input on the same thread that owns its reads. This
            # guarantees the underlying PortAudio stream's ring buffer is never
            # freed while a read is in flight, which would otherwise segfault the
            # process at end of roast (worker mid-read while the caller closes).
            _close_audio_input_if_supported(self._audio_input)
            if self._recorder is not None:
                with suppress(Exception):
                    self._recorder.close()

    def _disable_recorder(self) -> None:
        """Finalize and drop the recorder after a best-effort recording failure.

        Closes the recorder first so a partial recording is flushed and its
        sidecar written, then clears the reference so the capture worker performs
        no further recording work. Closing is itself suppressed: the recorder is
        already in a failure path and must not propagate into detection.
        """
        recorder = self._recorder
        self._recorder = None
        if recorder is not None:
            with suppress(Exception):
                recorder.close()

    def _read_next_samples(self) -> tuple[float, ...]:
        needed_samples = self._settings.window_sample_count - len(self._sample_buffer)
        raw_samples = self._audio_input.read_samples(max(1, needed_samples))
        return tuple(_normalize_sample(sample) for sample in raw_samples)

    def _emit_complete_windows(self) -> None:
        window_sample_count = self._settings.window_sample_count
        while len(self._sample_buffer) >= window_sample_count:
            window_samples = tuple(self._sample_buffer[:window_sample_count])
            del self._sample_buffer[: self._settings.hop_sample_count]
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
        del self._sample_buffer[: self._settings.hop_sample_count]
        with self._state_lock:
            if self._timeline_start_monotonic_seconds is None:
                self._timeline_start_monotonic_seconds = self._monotonic_now()
            sequence_number = self._next_sequence_number
            self._next_sequence_number += 1
            self._replay_emitted_window_count += 1
            started_at_monotonic_seconds = (
                self._timeline_start_monotonic_seconds
                + sequence_number * self._settings.effective_hop_seconds
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
    if not math.isfinite(settings.overlap) or not 0.0 <= settings.overlap < 1.0:
        raise AudioCaptureError("audio overlap must be greater than or equal to 0 and less than 1.")
    if settings.hop_seconds is not None:
        if not math.isfinite(settings.hop_seconds) or settings.hop_seconds <= 0:
            raise AudioCaptureError("audio hop_seconds must be > 0.")
        if settings.hop_seconds > settings.window_seconds:
            raise AudioCaptureError(
                "audio hop_seconds must be less than or equal to window_seconds."
            )
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
