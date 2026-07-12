"""Audio capture windowing for first-crack detection."""

from __future__ import annotations

import importlib
import json
import logging
import math
import struct
import time
import wave
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Any, Protocol, Self, cast

import numpy as np

from coffee_roaster_mcp.config import AudioConfig

DEFAULT_AUDIO_WINDOW_SECONDS = 1.0
DEFAULT_AUDIO_WINDOW_QUEUE_LIMIT = 8
DEFAULT_AUDIO_IDLE_SLEEP_SECONDS = 0.01

#: Bounded queue between the dedicated reader thread and the processing
#: thread (#190). ~3s of headroom at the default 100ms reader chunk size —
#: enough to absorb a genuine processing stall without unbounded memory
#: growth; a reader that outpaces processing this much for this long
#: indicates the processing thread itself is starved for a different reason.
_READER_QUEUE_MAXSIZE = 30


@dataclass(frozen=True)
class _CapturedChunk:
    """One raw sample block plus the monotonic instant it was CAPTURED.

    coffee-roaster-mcp#190 review finding: under reader/processing backlog
    (the ``_raw_reads`` queue can hold up to ``_READER_QUEUE_MAXSIZE``
    chunks), a chunk's samples can sit queued for up to several seconds
    before the processing thread ever sees them. Stamping capture time in
    the READER thread — at the ``stream.read()`` call itself, not at
    eventual window emission — is what lets
    :class:`AudioWindow`.started_at_monotonic_seconds reflect when the
    audio was actually captured off the device rather than when the
    processing thread got around to assembling a window from it. This is
    the timestamp provenance the coffee-roaster-mcp#191 post-drop drain's
    end-time bound (and every other consumer of window timestamps) depends
    on being accurate.

    Attributes:
        samples: Normalized mono float samples for this chunk.
        captured_at_monotonic_seconds: Monotonic instant the reader thread's
            ``stream.read()`` call returned this chunk.
    """

    samples: tuple[float, ...]
    captured_at_monotonic_seconds: float


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
        peak_dbfs: Rolling peak level of the captured stream in dBFS over the most
            recent measurement window, or ``None`` before any samples are read
            (#178). ``-inf`` for pure silence. A live in-session mic-levels signal
            so a mis-gained or dead mic is visible under real roasting conditions.
        rms_dbfs: Rolling RMS level of the captured stream in dBFS over the most
            recent measurement window, or ``None`` before any samples are read
            (#178). ``-inf`` for pure silence.
        overflow_count_last_minute: Microphone input-overflow events (#190) in
            the trailing 60 seconds of wall-clock time, or ``0`` when the audio
            input does not report overflows (e.g. WAV replay). Surfaces
            sustained degradation as an operator-visible diagnostic instead of
            stderr-only warning logs.
        estimated_lost_audio_ms_last_minute: Estimated milliseconds of audio at
            risk from overflow events in the trailing 60 seconds. Each
            overflow event contributes ``max(0, actual_gap - expected_duration)``
            — the ACTUAL wall-clock gap since the previous read attempt minus
            what that read's requested sample count should have taken (#190
            review finding: a fixed single-read-duration estimate can
            UNDERESTIMATE, not over-, when PortAudio's overflow flag — which
            covers all loss since the previous read — spans several
            consecutive overflowed reads during one sustained stall). This is
            still an estimate, not an exact lost-sample count: PortAudio
            never reports how many samples were actually dropped.
        total_overflow_count: Lifetime overflow event count for the current
            capture run, for a whole-roast severity view alongside the rolling
            per-minute figures.
    """

    running: bool
    queued_window_count: int
    emitted_window_count: int
    dropped_window_count: int
    latest_error: str | None
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    overflow_count_last_minute: int = 0
    estimated_lost_audio_ms_last_minute: float = 0.0
    total_overflow_count: int = 0


def amplitude_to_dbfs(amplitude: float) -> float:
    """Convert a 0..1 normalized amplitude to dBFS.

    Returns ``-inf`` for an amplitude at or below zero (pure silence) so a dead
    or muted mic is unambiguous in the levels readout (#178). Amplitudes above
    1.0 are clamped to 0 dBFS.

    Args:
        amplitude: Absolute sample amplitude in the normalized [0, 1] range.

    Returns:
        The level in dBFS, ``-inf`` at silence.
    """
    if amplitude <= 0.0:
        return -math.inf
    return round(20.0 * math.log10(min(1.0, amplitude)), 2)


class _RollingLevelMeter:
    """Track peak / RMS of the captured stream over a recent sample window (#178).

    Fed the same float blocks the detector consumes, it keeps a bounded ring of
    the most recent samples and reports the peak and RMS of that ring in dBFS.
    The bound keeps the meter responsive (it reflects the live signal, not the
    whole-roast average) and the work is vectorised in C via numpy so it never
    stalls the capture worker on the Pi CPU budget (D27). The meter is written
    only by the single capture worker thread, but a snapshot reader runs on
    another thread WITHOUT holding the writer (``observe`` is outside the
    pipeline's state lock). To keep the reader from seeing a torn pair (a peak
    from one update with an RMS from another), the two dBFS levels are stored as
    one ``(peak, rms)`` tuple assigned atomically; ``levels`` reads that single
    reference, so a reader always sees a coherent pair.
    """

    def __init__(self, *, window_sample_count: int) -> None:
        """Configure the rolling meter.

        Args:
            window_sample_count: Maximum recent samples retained for the level
                measurement. At least one sample is always kept.
        """
        self._window_sample_count = max(1, window_sample_count)
        self._samples: np.ndarray = np.empty(0, dtype=np.float64)
        #: The coherent ``(peak_dbfs, rms_dbfs)`` pair, or ``None`` before any
        #: samples. Assigned as one reference so a cross-thread reader never sees
        #: a peak and RMS from different updates.
        self._levels: tuple[float, float] | None = None

    def observe(self, samples: Sequence[float]) -> None:
        """Append captured samples and recompute the rolling peak / RMS."""
        if not samples:
            return
        block = np.abs(np.asarray(samples, dtype=np.float64))
        combined = np.concatenate((self._samples, block))
        if combined.shape[0] > self._window_sample_count:
            combined = combined[-self._window_sample_count :]
        self._samples = combined
        peak = float(combined.max()) if combined.shape[0] else 0.0
        rms = float(np.sqrt(np.mean(np.square(combined)))) if combined.shape[0] else 0.0
        # Single atomic assignment of the coherent pair (see class docstring):
        # the reader sees both new values or both old, never a mix.
        self._levels = (amplitude_to_dbfs(peak), amplitude_to_dbfs(rms))

    @property
    def levels(self) -> tuple[float, float] | None:
        """Return the coherent ``(peak_dbfs, rms_dbfs)`` pair, or ``None``.

        A single read of the atomically-assigned tuple, so a cross-thread reader
        never sees a peak and RMS drawn from different updates. ``None`` before
        any samples have been observed.
        """
        return self._levels

    @property
    def peak_dbfs(self) -> float | None:
        """Return the rolling peak in dBFS, or ``None`` before any samples."""
        levels = self._levels
        return None if levels is None else levels[0]

    @property
    def rms_dbfs(self) -> float | None:
        """Return the rolling RMS in dBFS, or ``None`` before any samples."""
        levels = self._levels
        return None if levels is None else levels[1]

    def reset(self) -> None:
        """Clear the retained samples and reported levels for a fresh run."""
        self._samples = np.empty(0, dtype=np.float64)
        self._levels = None


@dataclass(frozen=True)
class OverflowSnapshot:
    """Rolling and lifetime microphone input-overflow stats (#190).

    Attributes:
        count_last_minute: Overflow events in the trailing 60 seconds.
        estimated_lost_audio_ms_last_minute: Estimated at-risk audio in the
            trailing 60 seconds; see :class:`AudioCaptureSnapshot` for the
            estimation method (derived from the actual inter-read gap, not a
            fixed per-read duration — #190 review finding).
        total_count: Lifetime overflow event count for the capture run.
    """

    count_last_minute: int
    estimated_lost_audio_ms_last_minute: float
    total_count: int


def _merge_overflow_snapshots(
    primary: OverflowSnapshot | None,
    secondary: OverflowSnapshot | None,
) -> OverflowSnapshot | None:
    """Additively combine two overflow snapshots from independent streams.

    coffee-roaster-mcp#193 review finding: the detector device's own
    overflow tracker and an additional-device aggregate (from
    `MultiDeviceRoastRecorder`) are two INDEPENDENT streams — neither
    double-counts the other's events, so their counts/estimated-ms/lifetime
    totals simply sum. `None` when neither side has anything to report,
    matching the existing "no overflow-capable input" convention.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    return OverflowSnapshot(
        count_last_minute=primary.count_last_minute + secondary.count_last_minute,
        estimated_lost_audio_ms_last_minute=round(
            primary.estimated_lost_audio_ms_last_minute
            + secondary.estimated_lost_audio_ms_last_minute,
            3,
        ),
        total_count=primary.total_count + secondary.total_count,
    )


class _OverflowTracker:
    """Track microphone input-overflow events over a trailing 60-second window.

    A PortAudio blocking-read overflow (``stream.read()``'s ``overflowed``
    flag) reports only that input was lost since the previous read, never how
    many samples — so each event's "lost audio" contribution is estimated
    from the ACTUAL wall-clock gap since the previous read attempt (see
    :class:`AudioCaptureSnapshot` for the estimation method — #190 review
    finding: a fixed single-read-duration estimate can underestimate a
    multi-interval stall). Events older than 60 seconds of WALL-CLOCK time
    (not roast-elapsed time) are excluded from the rolling figures so they
    reflect genuinely recent degradation, matching the #190 report's
    "per-minute" framing.

    WRITER/READER SPLIT (#190 safety review) — CORRECTED: ``observe_overflow``
    runs only on the audio-input's own read call (the single capture-owning
    thread); ``snapshot`` is called from other threads (the processing
    thread's periodic status reads). A first pass at this class reasoned that
    ``snapshot`` never *mutating* ``_events`` made it safe to read
    lock-free — but that missed that ``snapshot`` still *iterates* over
    ``_events`` (to filter the trailing-60s window), and ``deque`` iteration
    is NOT safe against a concurrent mutation on another thread even when
    each individual append/popleft is itself atomic: Python explicitly raises
    ``RuntimeError: deque mutated during iteration`` if a writer's
    ``popleft()`` lands mid-iteration on the reader thread — a genuine crash
    risk under exactly the sustained-overflow conditions #190 is about
    (frequent ``observe_overflow`` calls racing a concurrent ``snapshot``
    poll). Both methods now hold ``_lock`` for their full body, which is
    correct AND still cheap: the critical section is a handful of `deque`
    operations, never I/O or anything GIL-releasing, so contention is brief
    even under a real overflow storm.
    """

    def __init__(self, *, monotonic_now: Callable[[], float] | None = None) -> None:
        """Configure the tracker.

        Args:
            monotonic_now: Optional monotonic clock supplier for tests.
        """
        self._monotonic_now = monotonic_now or time.monotonic
        self._events: deque[tuple[float, float]] = deque()
        self._total_count = 0
        self._lock = Lock()

    def observe_overflow(self, *, estimated_lost_audio_ms: float) -> None:
        """Record one overflow event with its estimated lost-audio duration.

        Thread-safe: holds the tracker's lock for its full body (see the
        class docstring for why a mutation-only guarantee isn't enough).
        """
        now = self._monotonic_now()
        with self._lock:
            self._events.append((now, estimated_lost_audio_ms))
            self._total_count += 1
            cutoff = now - 60.0
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def reset(self) -> None:
        """Clear all tracked overflow events and the lifetime count.

        coffee-roaster-mcp#193 review finding: a `MicrophoneAudioInput` can
        outlive a single roast (the owning `AudioCapturePipeline` can be
        `start()`ed more than once against the same input), so
        `total_overflow_count` — documented as "lifetime... for the CURRENT
        capture run" — must never carry a prior run's overflow history into
        a fresh one. Thread-safe for the same reason `observe_overflow`/
        `snapshot` are: called from `_reset_run_state_locked`, which itself
        runs under the pipeline's own state lock, but this lock is held too
        for defence in depth and symmetry with the other mutating method.
        """
        with self._lock:
            self._events.clear()
            self._total_count = 0

    def snapshot(self) -> OverflowSnapshot:
        """Return the current rolling and lifetime overflow stats.

        Thread-safe: holds the tracker's lock for its full body — including
        the iteration over ``_events`` — so this can never race a concurrent
        ``observe_overflow`` mutation on another thread (see the class
        docstring).
        """
        cutoff = self._monotonic_now() - 60.0
        with self._lock:
            recent = [lost for observed_at, lost in self._events if observed_at >= cutoff]
            total_count = self._total_count
        return OverflowSnapshot(
            count_last_minute=len(recent),
            estimated_lost_audio_ms_last_minute=round(sum(recent), 3),
            total_count=total_count,
        )


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
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        """Configure a microphone input that opens lazily on first read.

        Args:
            settings: Validated audio capture settings.
            sounddevice_module: Optional injected sounddevice-compatible module for tests.
            max_consecutive_overflows: Consecutive input overflows tolerated before
                capture faults. A transient overflow is logged and the captured
                samples are still returned; only a sustained run (the device cannot
                keep up at all) raises ``AudioCaptureError``.
            monotonic_now: Optional monotonic clock supplier for tests (#190
                overflow-tracker rolling window).
        """
        _validate_settings(settings)
        self._settings = settings
        self._sounddevice = sounddevice_module or _load_sounddevice()
        self._stream: Any | None = None
        self._max_consecutive_overflows = max_consecutive_overflows
        self._consecutive_overflows = 0
        self._close_lock = Lock()
        self._monotonic_now = monotonic_now or time.monotonic
        #: Per-minute overflow diagnostics (#190), read by the capture pipeline
        #: each loop tick and surfaced in AudioCaptureSnapshot / fc_status so
        #: sustained degradation is operator-visible, not just stderr.
        self._overflow_tracker = _OverflowTracker(monotonic_now=monotonic_now)
        #: Wall-clock instant the previous read_samples() call returned, or
        #: None before the first read. Used to estimate lost audio from the
        #: ACTUAL inter-read gap (#190 review finding), not just this read's
        #: nominal requested duration — PortAudio's overflowed flag can cover
        #: loss accumulated across MULTIPLE consecutive overflowed reads, so a
        #: single-read-duration estimate can genuinely underestimate for a
        #: multi-interval stall (the opposite of "upper bound").
        self._last_read_returned_at: float | None = None

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
        read_returned_at = self._monotonic_now()
        if overflowed:
            # Transient: the device dropped some input but `raw_data` still holds
            # the samples it captured. Log, count, and keep going rather than
            # killing capture for the whole roast; only a sustained run of
            # consecutive overflows (the device cannot keep up at all) faults.
            self._consecutive_overflows += 1
            # PortAudio's overflowed flag reports only that input was lost
            # SINCE THE PREVIOUS READ, which can span multiple consecutive
            # overflowed reads during a genuine multi-second stall — so a
            # single-read-duration estimate can UNDERESTIMATE for a
            # multi-interval gap (#190 review finding: the prior "upper
            # bound" framing was backwards). Estimate instead from the
            # ACTUAL wall-clock gap since the previous read attempt: the
            # portion of that gap beyond what this read's requested sample
            # count should have taken is exactly the time PortAudio's ring
            # buffer was filling unread. Clamped to >= 0 and falls back to
            # this read's nominal duration when there is no prior read to
            # compare against (the first read of a run) — see
            # OverflowSnapshot's docstring for the estimate's honest bounds.
            expected_duration_ms = 1000.0 * sample_count / self._settings.sample_rate
            if self._last_read_returned_at is None:
                estimated_lost_audio_ms = expected_duration_ms
            else:
                actual_gap_ms = 1000.0 * (read_returned_at - self._last_read_returned_at)
                estimated_lost_audio_ms = max(0.0, actual_gap_ms - expected_duration_ms)
            self._overflow_tracker.observe_overflow(estimated_lost_audio_ms=estimated_lost_audio_ms)
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
        # Update on EVERY read (overflowed or not) so the next call's gap
        # estimate is always measured from the most recent read, not a stale
        # earlier one.
        self._last_read_returned_at = read_returned_at
        return tuple(float(sample[0]) for sample in struct.iter_unpack("f", bytes(raw_data)))

    @property
    def overflow_snapshot(self) -> OverflowSnapshot:
        """Return current rolling and lifetime overflow stats (#190)."""
        return self._overflow_tracker.snapshot()

    def reset_overflow_tracking(self) -> None:
        """Clear overflow history so a reused input starts each run fresh.

        coffee-roaster-mcp#193 review finding: this input's lifetime spans
        as long as it is referenced, which can be more than one capture
        run if the owning pipeline is `start()`ed again on the same
        instance. Duck-typed by `AudioCapturePipeline._reset_run_state_locked`
        (only microphone inputs report overflows at all).

        Also resets `_last_read_returned_at` (coffee-roaster-mcp#193 review
        finding, round 2): without this, the first overflowed read of a
        restarted run computes its gap against the PREVIOUS run's last
        read — which can be an arbitrarily long real-world pause between
        roasts — producing one phantom, wildly inflated lost-audio spike
        that has nothing to do with this run's actual capture behaviour.
        `None` restores the "no prior read" branch in `read_samples`, which
        falls back to the read's own nominal duration for that first
        estimate, exactly like a freshly-constructed input would.
        """
        self._overflow_tracker.reset()
        self._last_read_returned_at = None

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
        # Serialises write_samples / close so the capture worker and a stop-caller
        # fallback finalisation (#176 hardware bug 2) never touch the wave handle
        # concurrently.
        self._lock = Lock()

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

        The WAV header (sample rate / channels / width) is committed by the
        stdlib ``wave`` module at ``close()``, so a stream that captures no frames
        — e.g. the teed detector stream while the AST model loads (#176 hardware
        bug 2) — still finalises as a valid 0-frame WAV as long as close() runs
        (which the capture worker and stop-caller fallback both guarantee).

        Raises:
            AudioCaptureError: If the WAV file cannot be opened.
        """
        with self._lock:
            if self._wav is not None or self._closed:
                return
            self._wav_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                wav_file = wave.open(  # noqa: SIM115 - writer owns the handle until close().
                    str(self._wav_path), "wb"
                )
                wav_file.setnchannels(1)
                wav_file.setsampwidth(_RECORDER_SAMPLE_WIDTH_BYTES)
                wav_file.setframerate(self._sample_rate)
            except (OSError, wave.Error) as exc:
                raise AudioCaptureError(
                    f"Could not open roast recording WAV {self._wav_path}: {exc}"
                ) from exc
            self._wav = wav_file

    def write_samples(self, samples: Sequence[float]) -> None:
        """Append mono float samples to the buffer, flushing on threshold."""
        with self._lock:
            if self._wav is None or self._closed or not samples:
                return
            self._pending.extend(float(sample) for sample in samples)
            if len(self._pending) >= self._flush_sample_threshold:
                self._flush_locked()

    def close(self) -> None:
        """Flush remaining samples and finalize the WAV. Idempotent + thread-safe."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._wav is None:
                return
            try:
                self._flush_locked()
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

    def _flush_locked(self) -> None:
        if self._wav is None or not self._pending:
            return
        # Vectorised PCM16 conversion (coffee-roaster-mcp#180): the previous
        # per-sample Python struct.pack loop packed the full flush block (default
        # 16k samples) one sample at a time while holding the GIL, stalling the
        # detector capture worker (and the ATR2100x thread) long enough to
        # overflow the mic input on a live roast (roast 5). numpy does the
        # clamp/scale/round in C and releases the GIL, so the flush no longer
        # starves detection.
        samples = np.asarray(self._pending, dtype=np.float64)
        np.clip(samples, -1.0, 1.0, out=samples)
        pcm16 = np.rint(samples * _RECORDER_PCM16_MAX).astype("<i2")
        self._wav.writeframes(pcm16.tobytes())
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


@dataclass(frozen=True)
class AnnotationSessionSpec:
    """Annotation-pipeline session descriptor for the captured WAVs (#176).

    Drives the ``{origin}-roast{N}-session.json`` written alongside the recording
    sidecar so the WAVs plug straight into the coffee-first-crack-detection
    pipeline (``record_mics.py`` → ``propagate_annotations.py`` → ``chunk_audio``),
    which keys on ``origin`` / ``roast_num`` and a ``mics`` list of
    ``{mic_num, label, file}``.

    Attributes:
        path: Destination ``{origin}-roast{N}-session.json`` path.
        origin: Bean origin slug (e.g. ``"brazil"``). Falls back to the session
            id when ``set_recording_metadata`` was never called.
        roast_num: 1-based roast number, or ``0`` for the no-metadata fallback.
        mic_labels: Per-stream label in device order (index 0 = detector / mic1).
    """

    path: Path
    origin: str
    roast_num: int
    mic_labels: tuple[str, ...]


def _write_annotation_session(
    spec: AnnotationSessionSpec,
    streams: Sequence[_WavStreamWriter],
) -> None:
    """Write the ``{origin}-roast{N}-session.json`` for the annotation pipeline.

    The shape matches ``record_mics.py``: ``origin`` / ``roast_num`` /
    ``sample_rate`` / ``mics`` where each mic entry carries ``mic_num`` (1-based,
    in device order), ``label``, and ``file`` (the WAV filename). ``mic1`` is the
    detector/teed stream by convention.
    """
    mics: list[dict[str, object]] = []
    for index, stream in enumerate(streams):
        label = spec.mic_labels[index] if index < len(spec.mic_labels) else f"mic{index + 1}"
        mics.append(
            {
                "mic_num": index + 1,
                "label": label,
                "file": stream.wav_path.name,
            }
        )
    payload: dict[str, object] = {
        "origin": spec.origin,
        "roast_num": spec.roast_num,
        "sample_rate": streams[0].sample_rate if streams else 0,
        "mics": mics,
    }
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    with spec.path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
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

    @property
    def overflow_snapshot(self) -> OverflowSnapshot | None:
        """Return this stream's overflow diagnostics, if its input reports them.

        coffee-roaster-mcp#193 review finding: each additional device opens
        its OWN `MicrophoneAudioInput` with its own overflow tracker — duck-
        typed the same way `AudioCapturePipeline.snapshot()` reads the
        detector device's, since only microphone inputs report overflows.
        """
        return cast(
            "OverflowSnapshot | None",
            getattr(self._audio_input, "overflow_snapshot", None),
        )

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
        annotation_session: AnnotationSessionSpec | None = None,
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
            annotation_session: Optional annotation-pipeline session descriptor
                (#176). When set, a ``{origin}-roast{N}-session.json`` is written
                alongside the sidecar so the WAV plugs into the FC annotation
                pipeline.
            monotonic_now: Optional monotonic clock supplier for tests.
            flush_sample_threshold: Buffered sample count that triggers a disk
                flush. Larger values reduce syscalls at the cost of memory.

        Raises:
            AudioCaptureError: If sample_rate or the flush threshold is invalid.
        """
        self._sidecar_path = Path(sidecar_path)
        self._session_id = session_id
        self._milestones_provider = milestones_provider
        self._annotation_session = annotation_session
        self._monotonic_now = monotonic_now or time.monotonic
        self._writer = _WavStreamWriter(
            wav_path=wav_path,
            device_label=device_label,
            sample_rate=sample_rate,
            flush_sample_threshold=flush_sample_threshold,
        )
        self._started_monotonic_seconds: float | None = None
        self._closed = False
        # Guards the one-shot close so a worker finally-close and a stop-caller
        # fallback close (#176 hardware bug 2) can't both finalise/write sidecars.
        self._close_lock = Lock()

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
        """Flush remaining samples, finalize the WAV, and write the sidecar JSON.

        Idempotent and thread-safe: a worker finally-close and a stop-caller
        fallback close (#176 hardware bug 2) race here, and only the first
        finalises and writes the sidecars.
        """
        with self._close_lock:
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
            if self._annotation_session is not None:
                with suppress(Exception):
                    _write_annotation_session(self._annotation_session, (self._writer,))


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
        annotation_session: AnnotationSessionSpec | None = None,
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
            annotation_session: Optional annotation-pipeline session descriptor
                (#176). When set, a ``{origin}-roast{N}-session.json`` listing
                every device (mic1 = detector) is written alongside the sidecar.
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
        self._annotation_session = annotation_session
        self._monotonic_now = monotonic_now or time.monotonic
        self._stop_timeout_seconds = stop_timeout_seconds
        self._started_monotonic_seconds: float | None = None
        self._closed = False
        # Guards the one-shot close (#176 hardware bug 2): a worker finally-close
        # and a stop-caller fallback close must not both finalise/write sidecars.
        self._close_lock = Lock()
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
    def overflow_snapshot(self) -> OverflowSnapshot | None:
        """Return overflow diagnostics AGGREGATED across additional devices.

        coffee-roaster-mcp#193 review finding: the pipeline's own
        `overflow_snapshot` (surfaced in fc_status) only covers the
        detector device — this recorder's additional independently-captured
        streams (#176 option A) each have their own `MicrophoneAudioInput`
        and were previously invisible to any diagnostic. Additive fix:
        `AudioCapturePipeline.snapshot()` duck-types this property on the
        recorder and folds it into the detector device's own figures, so no
        existing caller/contract changes shape — a recorder with no
        additional devices (or none reporting overflows) returns `None`,
        which the pipeline already treats as "nothing to add".

        The detector device's OWN overflow stats are NOT included here —
        they belong to the pipeline's audio input, not this recorder, so
        summing them at both layers would double-count. This aggregates
        only the streams THIS recorder independently owns.

        Counts and estimated-lost-ms are summed (additive across streams);
        the lifetime total is summed too. `None` when there are no
        additional devices, or none of them report overflows (e.g. all
        fake/non-microphone inputs in tests).
        """
        snapshots = [
            snapshot
            for stream in self._additional_streams
            if (snapshot := stream.overflow_snapshot) is not None
        ]
        if not snapshots:
            return None
        return OverflowSnapshot(
            count_last_minute=sum(snapshot.count_last_minute for snapshot in snapshots),
            estimated_lost_audio_ms_last_minute=round(
                sum(snapshot.estimated_lost_audio_ms_last_minute for snapshot in snapshots), 3
            ),
            total_count=sum(snapshot.total_count for snapshot in snapshots),
        )

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
        """Stop additional streams, finalize every WAV, and write the sidecars.

        Idempotent and thread-safe: a worker finally-close and a stop-caller
        fallback close (#176 hardware bug 2) race here, and only the first wins.
        """
        with self._close_lock:
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
            if self._annotation_session is not None:
                with suppress(Exception):
                    _write_annotation_session(self._annotation_session, streams)


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
        #: Last observed overflow snapshot, cached at close() (#193 review
        #: finding). close() drops `_input` so a post-close read of a
        #: LIVE property would silently go None — this stream's overflow
        #: history must survive close the same way the pipeline's own
        #: overflow stats survive stop() (task #59), so an operator
        #: reviewing a roast right after it ends still sees every device's
        #: figures, not just the detector's.
        self._closed_overflow_snapshot: OverflowSnapshot | None = None

    def read_samples(self, sample_count: int) -> Sequence[float]:
        """Open the input on first call, then read up to `sample_count` samples."""
        if self._input is None:
            self._input = self._factory(self._device)
        return self._input.read_samples(sample_count)

    def close(self) -> None:
        """Close the underlying input if it was opened."""
        if self._input is not None:
            self._closed_overflow_snapshot = cast(
                "OverflowSnapshot | None",
                getattr(self._input, "overflow_snapshot", None),
            )
            _close_audio_input_if_supported(self._input)
            self._input = None

    @property
    def overflow_snapshot(self) -> OverflowSnapshot | None:
        """Return the wrapped input's overflow diagnostics, if any (#193).

        Live while the input is open; the last-observed value once closed
        (see `_closed_overflow_snapshot`). `None` before the input has ever
        been opened, or when it does not report overflows at all — duck-
        typed identically to every other `overflow_snapshot` accessor in
        this module.
        """
        if self._input is None:
            return self._closed_overflow_snapshot
        return cast("OverflowSnapshot | None", getattr(self._input, "overflow_snapshot", None))


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
        # coffee-roaster-mcp#195 CI follow-up: a runtime-boundary discard
        # (charge/T0) must drop audio buffered ANYWHERE in the pipeline, not
        # just already-emitted windows. _sample_buffer/_buffered_chunk_bounds
        # are single-writer state owned by the processing thread — this flag
        # asks that thread to clear them itself on its next iteration rather
        # than mutating them from the caller's thread, which would race the
        # same way #193's un-locked deque did. The ack Event lets the caller
        # wait deterministically instead of guessing a sleep duration.
        self._discard_pending_audio_requested = Event()
        self._discard_pending_audio_acknowledged = Event()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._next_sequence_number = 0
        self._emitted_window_count = 0
        self._dropped_window_count = 0
        self._latest_error: str | None = None
        # Live mic-levels meter (#178): a rolling peak/RMS over roughly the most
        # recent measurement window of captured samples, fed the same blocks the
        # detector consumes. Surfaced in the snapshot so a mis-gained / dead mic
        # is visible under real roasting conditions, where the quiet pre-roast
        # floor differs from the in-roast level.
        self._level_meter = _RollingLevelMeter(window_sample_count=settings.window_sample_count)
        # Dedicated capture thread (#190): the ONLY job of this thread is
        # stream.read() + enqueue. Metering, recording (numpy PCM16 encode),
        # windowing, and detector-queue publish all run on the SEPARATE
        # processing thread below, draining this queue. Before this split, a
        # single thread did all of that work between successive read() calls,
        # and PortAudio's input ring buffer has no slack for that — under CPU
        # contention (concurrent ONNX inference + dual recording writers) the
        # gap between reads exceeded the buffer and the OS reported overflow.
        # A bounded queue here (not unbounded) still lets read() run far more
        # often than the processing work does, while capping memory if the
        # processing thread ever falls meaningfully behind.
        self._raw_reads: Queue[_CapturedChunk | None] = Queue(maxsize=_READER_QUEUE_MAXSIZE)
        self._reader_thread: Thread | None = None
        # Fixed small read chunk (independent of the processing thread's
        # buffer state) so read() cadence never depends on how much work the
        # processing thread still has queued up — the two loops are fully
        # decoupled. ~100ms at the configured sample rate: small enough to
        # keep PortAudio's buffer well-drained, large enough not to spin on
        # syscalls.
        self._reader_chunk_sample_count = max(1, round(settings.sample_rate * 0.1))
        # Chunk-boundary metadata for samples currently sitting in
        # _sample_buffer, oldest first (#190 review finding). Each entry
        # covers a contiguous run of samples with a known capture instant;
        # _emit_complete_windows consults this to derive a window's true
        # capture-time start instead of stamping it at emission time, which
        # would drift from reality by however long the chunk sat queued in
        # _raw_reads under processing backlog.
        self._buffered_chunk_bounds: deque[tuple[float, int]] = deque()

    @property
    def settings(self) -> AudioCaptureSettings:
        """Return the immutable capture settings."""
        return self._settings

    def start(self) -> AudioCaptureSnapshot:
        """Start background audio capture and return the current status snapshot.

        Raises:
            AudioCaptureError: If the processing thread is still running, OR
                if the reader thread is still running (coffee-roaster-mcp#195
                review finding, final fold). `stop()` joins each thread
                against its own `timeout_seconds` budget — the processing
                thread is never blocked in a syscall so it exits quickly,
                but the reader thread CAN still be mid-blocking-read and
                genuinely time out. Checking only the processing thread
                here would let a caller start a SECOND reader thread
                against the same still-alive `_audio_input` — two threads
                racing the same native PortAudio stream, which is exactly
                the class of corruption the single-reader-owns-reads
                invariant (#190) exists to prevent.
        """
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise AudioCaptureError("Audio capture pipeline is already running.")
            if self._reader_thread is not None and self._reader_thread.is_alive():
                raise AudioCaptureError(
                    "Audio capture pipeline's reader thread from a previous "
                    "stop() is still running (it likely timed out mid-read); "
                    "cannot start a second reader against the same input."
                )
            self._reset_run_state_locked()
            self._stop_requested.clear()
            # Two threads (#190): the reader does ONLY stream.read() + enqueue,
            # so PortAudio's ring buffer is drained on a cadence that never
            # depends on metering/recording/windowing work. The processing
            # thread does everything else, pulling from the reader's queue.
            self._reader_thread = Thread(
                target=self._run_reader_loop,
                name="coffee-roaster-audio-reader",
                daemon=True,
            )
            self._thread = Thread(
                target=self._run_processing_loop,
                name="coffee-roaster-audio-capture",
                daemon=True,
            )
            self._reader_thread.start()
            self._thread.start()
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        """Request capture stop and wait briefly for both threads to finish.

        The audio input is closed by the READER thread itself once its read
        loop exits, so its native (PortAudio) stream is never freed while a
        read is in flight (#190: reading is now the reader thread's ONLY
        job). This method only closes the input directly as a fallback when
        no reader thread is (still) running — for example, when ``start``
        failed before spawning it. Closing from this caller thread while the
        reader is still alive would free the ring buffer under an in-flight
        ``read`` and crash the process.

        Join order: the PROCESSING thread first (it is never blocked in a
        syscall — once it observes the stop signal it drains whatever the
        reader already enqueued and exits quickly), then the READER thread
        (which may still be mid-blocking-read and can take up to
        ``timeout_seconds`` to notice the stop signal on its next loop
        iteration). Each gets its own ``timeout_seconds`` budget rather than
        splitting one budget between them, so a slow reader join never
        starves the processing thread's join of time it needs.

        Args:
            timeout_seconds: Maximum time to wait for EACH thread to exit.

        Returns:
            The capture status snapshot taken after the stop request.

        Raises:
            AudioCaptureError: If ``timeout_seconds`` is negative.
        """
        if timeout_seconds < 0:
            raise AudioCaptureError("timeout_seconds must be >= 0.")
        self._stop_requested.set()
        processing_thread = self._thread
        reader_thread = self._reader_thread
        if processing_thread is not None:
            processing_thread.join(timeout=timeout_seconds)
        if reader_thread is not None:
            reader_thread.join(timeout=timeout_seconds)
        # coffee-roaster-mcp#195 review finding (final fold): the processing
        # thread's stop-drain above uses timeout=0 once _stop_requested is
        # set, so it can observe an EMPTY _raw_reads and exit while the
        # reader thread is still mid-blocking-read — the reader's one
        # remaining chunk (queued in its own finally, or via the shutdown
        # sentinel put) then has no consumer, the same class of gap the
        # earlier drain-on-stop fix closed for the processing thread's OWN
        # queue drain, just one chunk narrower. By this point both threads
        # have joined (or the join timed out, in which case a thread still
        # racing this drain is itself the documented fallback-finalize
        # case below), so draining and processing whatever landed in
        # _raw_reads after the processing thread's own loop exited is safe:
        # no other thread is writing to _sample_buffer/_buffered_chunk_bounds
        # or the recorder at this point.
        while True:
            try:
                chunk = self._raw_reads.get_nowait()
            except Empty:
                break
            if chunk is not None:
                with suppress(Exception):
                    self._process_captured_chunk(chunk)
        snapshot = self.snapshot()
        if reader_thread is None:
            # No reader thread ever ran (e.g. start() failed before spawning
            # it): close the input here as a fallback. Whenever a reader DID
            # run it closes the input itself in its loop finally — on the
            # thread that owns reads — whether it has already exited or is
            # still draining a blocking read. So stop() must not also close
            # it: that would double free / free under an in-flight read.
            # (close() is idempotent anyway, but correctness here does not
            # depend on that.)
            _close_audio_input_if_supported(self._audio_input)
        if processing_thread is not None and processing_thread.is_alive():
            # The processing thread did not exit within the join timeout —
            # e.g. a genuinely slow recorder write (disk contention) still in
            # flight at the exact moment stop() was called; #190's split
            # means it is never blocked in stream.read() itself anymore, so
            # this should now be rare. Its `finally` (which closes the
            # recorder) will not run before this caller returns, and on
            # process/lifespan shutdown the daemon thread can be killed
            # before it ever does, leaving the WAV at 0 bytes and no
            # sidecars (#176 hardware bug 2). Finalise the recorder HERE as a
            # fallback: the recorder is thread-safe (its writer lock
            # serialises this close against any in-flight processing-thread
            # write), and close() is idempotent, so the processing thread's
            # later close() is a no-op. We do NOT touch the audio input —
            # only the reader thread may free its native ring buffer.
            self._finalize_recorder()
        return snapshot

    def _finalize_recorder(self) -> None:
        """Finalise the recorder (flush WAV + write sidecars), idempotent + safe.

        Safe to call from the stop caller as a fallback when the worker thread is
        still alive: the recorder's writer lock serialises this close against any
        in-flight worker write, and close() is idempotent so the worker's own
        finally-close becomes a no-op.
        """
        recorder = self._recorder
        if recorder is not None:
            with suppress(Exception):
                recorder.close()

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

    def discard_pending_audio(self, *, timeout_seconds: float = 1.0) -> None:
        """Drop every window/chunk/sample buffered anywhere in the pipeline.

        coffee-roaster-mcp#195 CI follow-up: `drain_windows()` alone only
        clears already-emitted windows. Audio captured before a runtime
        boundary (e.g. `beans_added`) can still be sitting unprocessed in
        the reader thread's backlog or the processing thread's partial
        sample buffer at the moment the boundary is recorded — and with
        accurate capture-time window stamps (#190/#195), that stale audio
        can later be assembled into a window whose `started_at_monotonic_seconds`
        predates the boundary. Call this immediately after recording the
        boundary event to guarantee no pre-boundary audio survives into a
        later window.

        When the processing thread is running, the discard is requested and
        this method blocks (bounded by `timeout_seconds`) until that thread
        acknowledges having cleared its own single-writer state — mutating
        `_sample_buffer` from this thread instead would race the processing
        thread's own writes to it. When the thread is not running, there is
        no concurrent writer, so the buffers are cleared directly.

        Args:
            timeout_seconds: Maximum time to wait for the processing thread
                to acknowledge the discard. A timeout is a defensive
                fallback only (e.g. a wedged worker) — normal operation
                acknowledges in well under a millisecond since the discard
                check runs at the top of every loop iteration.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._clear_buffered_audio()
            return
        self._discard_pending_audio_acknowledged.clear()
        self._discard_pending_audio_requested.set()
        if not self._discard_pending_audio_acknowledged.wait(timeout=timeout_seconds):
            _LOGGER.warning(
                "Audio capture discard request was not acknowledged within "
                "%.1fs; the processing thread may be wedged. Falling back to "
                "a direct clear, which races that thread's writes but is "
                "safer than leaving stale pre-boundary audio buffered.",
                timeout_seconds,
            )
            self._clear_buffered_audio()
            self._discard_pending_audio_requested.clear()

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
        reader_thread = self._reader_thread
        # Read the coherent (peak, rms) pair in a single access so the snapshot
        # never mixes a peak and RMS from different meter updates (#178). The
        # meter is written outside _state_lock by the capture worker, so two
        # separate property reads could tear; one tuple read cannot.
        levels = self._level_meter.levels
        # Overflow diagnostics (#190) are only reported by microphone inputs;
        # duck-type the optional property so WAV replay and other AudioInput
        # implementations (including test doubles) need not carry it.
        overflow = cast(
            "OverflowSnapshot | None",
            getattr(self._audio_input, "overflow_snapshot", None),
        )
        # coffee-roaster-mcp#193 review finding: independently-captured
        # additional recording devices (#176 option A) each own a separate
        # microphone input whose overflow stats were previously invisible —
        # duck-type the recorder's own aggregate (MultiDeviceRoastRecorder
        # exposes one; a plain RoastAudioRecorder or test double does not,
        # which _merge_overflow_snapshots treats identically to "nothing to
        # add") and fold it in additively alongside the detector device's.
        recorder_overflow = cast(
            "OverflowSnapshot | None",
            getattr(self._recorder, "overflow_snapshot", None),
        )
        overflow = _merge_overflow_snapshots(overflow, recorder_overflow)
        with self._state_lock:
            # Both threads must be alive for capture to be genuinely running
            # (#190): if the reader thread has died (e.g. a fatal overflow
            # AudioCaptureError) while the processing thread is still
            # draining a now-stale queue, reporting running=True would hide
            # a real capture failure.
            running = (
                thread is not None
                and thread.is_alive()
                and reader_thread is not None
                and reader_thread.is_alive()
            )
            return AudioCaptureSnapshot(
                running=running,
                queued_window_count=self._windows.qsize(),
                emitted_window_count=self._emitted_window_count,
                dropped_window_count=self._dropped_window_count,
                latest_error=self._latest_error,
                peak_dbfs=None if levels is None else levels[0],
                rms_dbfs=None if levels is None else levels[1],
                overflow_count_last_minute=0 if overflow is None else overflow.count_last_minute,
                estimated_lost_audio_ms_last_minute=(
                    0.0 if overflow is None else overflow.estimated_lost_audio_ms_last_minute
                ),
                total_overflow_count=0 if overflow is None else overflow.total_count,
            )

    def _run_reader_loop(self) -> None:
        """Drain the audio input as fast as possible; do nothing else (#190).

        This thread's ONLY job is ``stream.read()`` + enqueue, so PortAudio's
        input ring buffer is drained on a cadence independent of whatever the
        processing thread (metering, recording, windowing) is doing. It is
        also the thread that owns the audio input's lifecycle: it opens the
        input implicitly on first read and closes it in ``finally``, exactly
        preserving the pre-#190 invariant that only the thread performing
        reads may free the native ring buffer — just moved to this thread
        instead of the (former, now-processing-only) capture loop.
        """
        try:
            while not self._stop_requested.is_set():
                raw_samples = self._audio_input.read_samples(self._reader_chunk_sample_count)
                # Stamp IMMEDIATELY on read() returning — before
                # normalize/queue work — so it reflects when the device
                # actually produced this audio, not when the processing
                # thread eventually gets to it (#190 review finding: under
                # backlog the two can differ by seconds).
                #
                # read_samples() is a BLOCKING call that returns once the
                # requested samples have been captured, so `monotonic_now()`
                # here is the instant capture of this chunk FINISHED (block
                # END), not when it started (coffee-roaster-mcp#195 review
                # finding, final fold) — a systematic ~one-chunk-duration
                # late bias (~100ms at the default 0.1s reader chunk) on
                # every capture timestamp in the system, since every
                # downstream window/drop/T0 bound derives from this stamp.
                # Back-date by the ACTUAL returned sample count's duration
                # (not the requested count — a short/partial read genuinely
                # took less time) to recover the true block-START instant.
                completed_at = self._monotonic_now()
                # Explicit len() check, not `if not raw_samples:` (#190
                # review finding P3) — see _normalize_samples_block's
                # docstring for why a numpy-array-returning AudioInput would
                # otherwise crash this thread on a truthiness check.
                if len(raw_samples) == 0:
                    time.sleep(self._settings.idle_sleep_seconds)
                    continue
                captured_at = round(completed_at - len(raw_samples) / self._settings.sample_rate, 6)
                samples = _normalize_samples_block(raw_samples)
                # Blocking put with no timeout is intentional: a full queue
                # means the processing thread has fallen far behind (30
                # chunks / ~3s at the default chunk size), at which point
                # slowing the reader down is correct back-pressure rather
                # than silently dropping raw audio the recorder/detector
                # would otherwise never see at all.
                self._raw_reads.put(
                    _CapturedChunk(samples=samples, captured_at_monotonic_seconds=captured_at)
                )
        except Exception as exc:  # noqa: BLE001 - backend/validation exceptions vary.
            # Covers both a genuine read failure (device gone, backend error)
            # and a validation failure (_normalize_sample rejecting a
            # non-finite sample) — both are fatal to this capture run,
            # exactly matching the pre-#190 single-loop error handling.
            with self._state_lock:
                self._latest_error = str(exc)
            self._stop_requested.set()
        finally:
            # Close the audio input on the same thread that owns its reads —
            # see the docstring above; this is the load-bearing invariant
            # #190 must not disturb.
            _close_audio_input_if_supported(self._audio_input)
            # Sentinel: wake a processing thread that is blocked waiting for
            # the next chunk so it notices shutdown promptly even if
            # _stop_requested was set for a reason other than the caller's
            # normal stop() (e.g. a read error above).
            with suppress(Full):
                self._raw_reads.put_nowait(None)

    def _run_processing_loop(self) -> None:
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
            while True:
                if self._discard_pending_audio_requested.is_set():
                    # coffee-roaster-mcp#195 CI follow-up: a runtime boundary
                    # (e.g. beans_added) was just recorded and the caller
                    # wants every window/chunk/sample buffered anywhere in
                    # this pipeline dropped — not just the already-emitted
                    # windows drain_windows() can see. Clear from THIS
                    # thread (the single writer of _sample_buffer /
                    # _buffered_chunk_bounds) so no pre-boundary audio can
                    # survive into the next emitted window with an
                    # accurate-but-stale capture-time stamp.
                    self._clear_buffered_audio()
                    self._discard_pending_audio_requested.clear()
                    self._discard_pending_audio_acknowledged.set()
                    continue
                stop_was_requested = self._stop_requested.is_set()
                try:
                    # #190 review finding (P1): once stop() sets
                    # _stop_requested, still drain whatever the reader
                    # already queued rather than exiting immediately —
                    # otherwise up to _READER_QUEUE_MAXSIZE chunks (~3s of
                    # audio, e.g. the drop clatter / final crack the
                    # annotation workflow cares about) are silently
                    # abandoned. Once stop is requested, poll non-blockingly
                    # (timeout=0) instead of the normal idle-sleep wait, so
                    # a genuinely empty queue exits promptly rather than
                    # waiting out one more idle_sleep_seconds for nothing.
                    chunk = self._raw_reads.get(
                        timeout=0 if stop_was_requested else self._settings.idle_sleep_seconds
                    )
                except Empty:
                    if stop_was_requested:
                        break
                    continue
                if chunk is None:
                    # Reader-thread shutdown sentinel (see _run_reader_loop):
                    # the reader is done producing chunks. Keep draining any
                    # chunks that arrived before the sentinel — put_nowait
                    # from the reader's finally can still land after a
                    # backlog, so this is not necessarily the last item.
                    continue
                self._process_captured_chunk(chunk)
        except Exception as exc:  # noqa: BLE001 - worker stores error for caller inspection.
            with self._state_lock:
                self._latest_error = str(exc)
            self._stop_requested.set()
        finally:
            # Unlike before #190, this thread never reads from the audio
            # input, so it must NOT close it — only the reader thread may
            # free the native ring buffer (see _run_reader_loop).
            if self._recorder is not None:
                with suppress(Exception):
                    self._recorder.close()

    def _process_captured_chunk(self, chunk: _CapturedChunk) -> None:
        """Buffer, meter, record, and window one chunk from the reader.

        Extracted from the processing loop's body (#190 review finding P1)
        so it runs identically whether called from the normal draining loop
        or from the post-stop drain that empties any backlog left in
        ``_raw_reads`` before the recorder finalizes.
        """
        samples = chunk.samples
        self._sample_buffer.extend(samples)
        if samples:
            # Track this chunk's capture-time provenance alongside the
            # samples it contributed (#190 review finding), so
            # _emit_complete_windows can derive a window's TRUE capture-time
            # start instead of stamping emission time.
            self._buffered_chunk_bounds.append((chunk.captured_at_monotonic_seconds, len(samples)))
        # Update the live mic-levels meter from the SAME samples the
        # detector consumes (#178). Cheap, vectorised, and never raises;
        # purely observational so it cannot affect detection.
        self._level_meter.observe(samples)
        # Tee the SAME samples the detector consumes into the recording WAV
        # right after the buffer extend (#176). This is the verified
        # non-disturbing tee point: the detector still windows the buffer
        # below, untouched. Recording failures must never kill detection, so
        # a recorder error is logged and capture continues.
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

    def _emit_complete_windows(self) -> None:
        window_sample_count = self._settings.window_sample_count
        while len(self._sample_buffer) >= window_sample_count:
            window_samples = tuple(self._sample_buffer[:window_sample_count])
            # Derive the window's TRUE capture-time start from the first
            # buffered chunk's capture instant, BEFORE trimming the buffer
            # (#190 review finding) — not self._monotonic_now(), which would
            # be the emission instant and can drift from actual capture time
            # by however long this window's samples sat queued in
            # _raw_reads under processing backlog.
            window_started_at = self._chunk_capture_time_at_offset(0)
            del self._sample_buffer[: self._settings.hop_sample_count]
            self._advance_buffered_chunk_bounds(self._settings.hop_sample_count)
            window = AudioWindow(
                sequence_number=self._next_sequence_number,
                input_device=self._settings.input_device,
                sample_rate=self._settings.sample_rate,
                started_at_monotonic_seconds=window_started_at,
                duration_seconds=round(window_sample_count / self._settings.sample_rate, 6),
                samples=window_samples,
            )
            self._next_sequence_number += 1
            self._publish_window(window)

    def _chunk_capture_time_at_offset(self, sample_offset: int) -> float:
        """Return the capture-time instant of the sample at ``sample_offset``.

        ``_buffered_chunk_bounds`` holds ``(captured_at, chunk_length)`` pairs
        oldest-first, covering the samples currently in ``_sample_buffer`` in
        order. Walks forward to find which chunk contains ``sample_offset``,
        then adds the in-chunk offset (converted to seconds at the configured
        sample rate) to that chunk's capture instant — an exact answer when
        one chunk's samples were all captured back-to-back by one
        ``stream.read()`` call, and a reasonable linear estimate otherwise.

        Falls back to ``self._monotonic_now()`` (the pre-#190-review-fix
        behavior) only if the bounds tracking is empty — defensive, since
        every code path that extends ``_sample_buffer`` also appends here.

        Args:
            sample_offset: Zero-based index into the current sample buffer.

        Returns:
            The estimated monotonic capture instant for that sample.
        """
        remaining_offset = sample_offset
        for captured_at, chunk_length in self._buffered_chunk_bounds:
            if remaining_offset < chunk_length:
                return round(
                    captured_at + remaining_offset / self._settings.sample_rate,
                    6,
                )
            remaining_offset -= chunk_length
        return self._monotonic_now()  # pragma: no cover - defensive, see docstring

    def _advance_buffered_chunk_bounds(self, sample_count: int) -> None:
        """Trim ``_buffered_chunk_bounds`` by ``sample_count`` samples.

        Mirrors ``del self._sample_buffer[:sample_count]`` so the two stay in
        lockstep: full chunks entirely consumed by the trim are dropped, and
        a chunk only partially consumed has its recorded length reduced AND
        its capture instant advanced by the trimmed portion's duration
        (coffee-roaster-mcp#195 review finding, final fold) — samples within
        one chunk were captured back-to-back at the configured sample rate
        (the same assumption `_chunk_capture_time_at_offset` already makes),
        so the remainder's first sample was captured
        ``trimmed_samples / sample_rate`` seconds AFTER the chunk's original
        ``captured_at``, not at the same instant as the whole original
        chunk. Leaving the timestamp unadvanced understated every
        subsequent window's start by up to one chunk's duration whenever a
        partial trim landed inside it (the overlapping-windows case, since a
        non-overlapping hop always trims whole chunks or nothing).
        """
        remaining = sample_count
        while remaining > 0 and self._buffered_chunk_bounds:
            captured_at, chunk_length = self._buffered_chunk_bounds[0]
            if chunk_length <= remaining:
                self._buffered_chunk_bounds.popleft()
                remaining -= chunk_length
            else:
                advanced_captured_at = round(
                    captured_at + remaining / self._settings.sample_rate, 6
                )
                self._buffered_chunk_bounds[0] = (advanced_captured_at, chunk_length - remaining)
                remaining = 0

    def _publish_window(self, window: AudioWindow) -> None:
        try:
            self._windows.put_nowait(window)
        except Full:
            with self._state_lock:
                self._dropped_window_count += 1
            return
        with self._state_lock:
            self._emitted_window_count += 1

    def _clear_buffered_audio(self) -> None:
        """Drop every window/chunk/sample currently buffered anywhere in the
        pipeline: the emitted-window queue, the reader's raw-chunk backlog,
        and the processing thread's own partial sample buffer.

        Safe to call from `start()` before either worker thread exists. Once
        the processing thread is running, `_sample_buffer` and
        `_buffered_chunk_bounds` are its single-writer state — only that
        thread may call this (see `_run_processing_loop`'s discard check);
        `_windows` and `_raw_reads` are thread-safe `Queue`s so draining them
        from any thread is fine.
        """
        while True:
            try:
                self._windows.get_nowait()
            except Empty:
                break
        while True:
            try:
                self._raw_reads.get_nowait()
            except Empty:
                break
        self._sample_buffer.clear()
        self._buffered_chunk_bounds.clear()

    def _reset_run_state_locked(self) -> None:
        # Also drain any raw sample blocks left over from a prior run (#190):
        # a fresh start() must never process stale audio queued before this
        # restart.
        self._clear_buffered_audio()
        self._next_sequence_number = 0
        self._emitted_window_count = 0
        self._dropped_window_count = 0
        self._latest_error = None
        self._level_meter.reset()
        # coffee-roaster-mcp#193 review finding: a pipeline instance can be
        # start()ed more than once against the SAME audio input (see
        # test_audio_capture_pipeline_resets_run_state_on_restart) — without
        # this, overflow diagnostics from a PRIOR run leak into a fresh
        # roast's total_overflow_count and rolling last-minute figures,
        # exactly the stale-state class this method already guards against
        # for windows/buffers/the level meter. Duck-typed the same way
        # `AudioCapturePipeline.snapshot()` reads `overflow_snapshot` —
        # only microphone inputs report overflows.
        reset_overflow_tracking = getattr(self._audio_input, "reset_overflow_tracking", None)
        if reset_overflow_tracking is not None:
            reset_overflow_tracking()


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


def _normalize_samples_block(raw_samples: Sequence[float]) -> tuple[float, ...]:
    """Validate and normalize a full block of samples in one vectorised pass.

    coffee-roaster-mcp#190: the per-sample Python-level ``_normalize_sample``
    generator this replaces on the reader thread's hot path held the GIL for
    every sample between successive ``stream.read()`` calls — at 16kHz with a
    ~100ms reader chunk that is ~1,600 Python function calls per read,
    exactly the class of per-read overhead that caused the original overflow
    streaks, just relocated onto the reader thread instead of eliminated.
    numpy does the finiteness check and float64 cast in C and releases the
    GIL, so this no longer competes with concurrent inference/recording work
    for read cadence.

    Args:
        raw_samples: Raw samples as returned by an ``AudioInput``.

    Returns:
        The validated samples as a tuple of Python floats.

    Raises:
        AudioCaptureError: If any sample is not finite.
    """
    # Explicit len() check, not `if not raw_samples:` (#190 review finding
    # P3): AudioInput.read_samples is typed Sequence[float], and a numpy
    # array satisfies that Protocol structurally — bool(array) raises
    # ValueError for any multi-element array ("truth value of an array...is
    # ambiguous"), so a test double or future AudioInput implementation that
    # legitimately returns samples as a numpy array would crash the reader
    # thread here instead of failing cleanly (or working at all).
    if len(raw_samples) == 0:
        return ()
    array = np.asarray(raw_samples, dtype=np.float64)
    if not np.isfinite(array).all():
        raise AudioCaptureError("audio samples must be finite numbers.")
    return tuple(array.tolist())


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
