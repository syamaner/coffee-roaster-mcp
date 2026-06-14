"""Live-microphone pre-roast proof.

The first-crack detector is only as good as the audio reaching it. A device that
will not open surfaces as `unavailable`/`faulted`, but a device that *opens and
delivers silence* (OS microphone permission blocked, the device muted, or grabbed
by another process) is indistinguishable from "no crack yet" via the pipeline's
window counters — they advance on silence too, and per-window confidence is only
emitted on a detection candidate. So nothing in the normal run proves *real audio
is flowing*.

This module closes that gap with a guarded, operator-run check that uses the same
capture path as `serve` (the configured device, sample rate) and reports a signal
level that **responds to sound**: the operator runs it, makes noise (snap / speak /
tap), and a non-trivial RMS proves capture is live and unblocked. A flat-floor
level means silence (blocked/muted); a capture error means the device would not
open. It is injectable end to end (audio input + device lister) so the logic is
unit-tested without hardware; the decisive proof is the operator's real run.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from coffee_roaster_mcp.audio import (
    AudioCaptureError,
    AudioInput,
    audio_capture_settings_from_config,
    build_configured_audio_input,
)
from coffee_roaster_mcp.config import load_config

#: RMS above which captured audio is treated as real signal (not silence/zeros).
#: Deliberately equal to the first-crack detector's own energy noise gate: the
#: coffee-first-crack-detection inference gates any window with
#: ``sqrt(mean(window**2)) < 0.01`` to probability 0.0 (silence). So audio below
#: this floor would be ignored by detection regardless — proving the mic clears
#: it proves the detector will actually use the signal. Float samples are in
#: [-1, 1]; a snap / speech goes well over it, a blocked/muted device stays under.
DEFAULT_RMS_FLOOR = 0.01
#: Per-read chunk duration; short enough for a responsive live meter.
_CHUNK_SECONDS = 0.1

AudioInputFactory = Callable[[Any], AudioInput]
DeviceLister = Callable[[], Sequence[str]]


@dataclass(frozen=True)
class MicCheckOptions:
    """Options for a microphone capture check.

    Attributes:
        config_path: Optional coffee-roaster-mcp YAML config path; the audio
            section selects the device and sample rate exactly as `serve` would.
        duration_seconds: How long to capture before reporting.
        rms_floor: RMS level above which captured audio counts as real signal.
        output_path: Optional JSON evidence file to write.
    """

    config_path: Path | None = None
    duration_seconds: float = 5.0
    rms_floor: float = DEFAULT_RMS_FLOOR
    output_path: Path | None = None


@dataclass(frozen=True)
class MicCheckReport:
    """Evidence that the configured microphone is capturing real audio.

    Attributes:
        source: Configured audio source (`microphone` / `wav`).
        configured_device: The configured input-device selector, or `None` for
            the system default.
        matched_device: The available input device whose name matched the
            selector, or `None` if no match / system default.
        available_input_devices: Names of all input-capable devices found.
        device_found: Whether the configured device (or a default) is available.
        chunks_read: Number of audio chunks captured.
        rms_max: Peak RMS across the captured chunks.
        rms_mean: Mean RMS across the captured chunks.
        peak_amplitude: Largest absolute sample seen.
        audio_detected: Whether `rms_max` cleared `rms_floor` (real signal).
        error: Capture/enumeration error message, if any.
        passed: Device available AND opened AND real audio detected.
    """

    source: str
    configured_device: str | None
    matched_device: str | None
    available_input_devices: list[str] = field(default_factory=lambda: cast(list[str], []))
    device_found: bool = False
    chunks_read: int = 0
    rms_max: float = 0.0
    rms_mean: float = 0.0
    peak_amplitude: float = 0.0
    audio_detected: bool = False
    error: str | None = None
    passed: bool = False


def _default_device_lister() -> Sequence[str]:
    """Return the names of all input-capable audio devices via sounddevice."""
    try:
        sounddevice = importlib.import_module("sounddevice")
    except (ImportError, OSError) as exc:
        raise AudioCaptureError(
            "Microphone enumeration requires the sounddevice package and PortAudio runtime."
        ) from exc
    devices: Any = sounddevice.query_devices()
    return [
        str(device["name"]) for device in devices if int(device.get("max_input_channels", 0)) > 0
    ]


def _match_device(selector: str | None, available: Sequence[str]) -> str | None:
    """Match the configured selector (substring, case-insensitive) to a device."""
    if selector is None:
        return None
    needle = selector.casefold()
    for name in available:
        if needle in name.casefold():
            return name
    return None


def _rms(samples: Sequence[float]) -> float:
    """Root-mean-square of a sample chunk (0.0 for an empty chunk)."""
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def run_mic_check(
    options: MicCheckOptions,
    *,
    audio_input_factory: AudioInputFactory | None = None,
    device_lister: DeviceLister | None = None,
    on_chunk: Callable[[float, float], None] | None = None,
) -> MicCheckReport:
    """Capture from the configured microphone and report whether real audio flows.

    Args:
        options: Check options (config, duration, floor).
        audio_input_factory: Builds the :class:`AudioInput` from capture settings;
            defaults to the real configured input. Injected in tests.
        device_lister: Returns available input-device names; defaults to the real
            sounddevice enumeration. Injected in tests.
        on_chunk: Optional callback invoked per chunk with `(rms, peak)` for a
            live meter.

    Returns:
        A :class:`MicCheckReport`. Never raises for capture/enumeration problems —
        they are recorded in ``error`` with ``passed=False``.
    """
    config = load_config(path=options.config_path)
    settings = audio_capture_settings_from_config(config.audio)
    factory = audio_input_factory or build_configured_audio_input
    lister = device_lister or _default_device_lister

    available: list[str] = []
    matched: str | None = None
    device_found = False
    try:
        available = list(lister())
        matched = _match_device(settings.input_device, available)
        device_found = (matched is not None) or (
            settings.input_device is None and len(available) > 0
        )
    except AudioCaptureError as exc:
        return MicCheckReport(
            source=settings.source,
            configured_device=settings.input_device,
            matched_device=None,
            available_input_devices=available,
            error=str(exc),
        )

    chunk_samples = max(1, round(settings.sample_rate * _CHUNK_SECONDS))
    chunk_target = max(1, round(options.duration_seconds / _CHUNK_SECONDS))
    rms_values: list[float] = []
    peak = 0.0
    try:
        audio_input = factory(settings)
        try:
            for _ in range(chunk_target):
                samples = audio_input.read_samples(chunk_samples)
                chunk_rms = _rms(samples)
                chunk_peak = max((abs(sample) for sample in samples), default=0.0)
                rms_values.append(chunk_rms)
                peak = max(peak, chunk_peak)
                if on_chunk is not None:
                    on_chunk(chunk_rms, chunk_peak)
        finally:
            close = getattr(audio_input, "close", None)
            if callable(close):
                close()
    except AudioCaptureError as exc:
        return MicCheckReport(
            source=settings.source,
            configured_device=settings.input_device,
            matched_device=matched,
            available_input_devices=available,
            device_found=device_found,
            chunks_read=len(rms_values),
            error=str(exc),
        )

    rms_max = max(rms_values, default=0.0)
    rms_mean = (sum(rms_values) / len(rms_values)) if rms_values else 0.0
    audio_detected = rms_max >= options.rms_floor
    return MicCheckReport(
        source=settings.source,
        configured_device=settings.input_device,
        matched_device=matched,
        available_input_devices=available,
        device_found=device_found,
        chunks_read=len(rms_values),
        rms_max=round(rms_max, 6),
        rms_mean=round(rms_mean, 6),
        peak_amplitude=round(peak, 6),
        audio_detected=audio_detected,
        passed=device_found and audio_detected,
    )


def report_to_json(report: MicCheckReport) -> str:
    """Serialize a :class:`MicCheckReport` to pretty JSON."""
    return json.dumps(dataclasses.asdict(report), indent=2)
