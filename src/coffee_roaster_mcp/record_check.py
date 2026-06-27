"""Roast-free multi-device recording smoke test (#176, option A).

The multi-device recorder only runs inside a roast (the detector owns the teed
stream; the additional devices are opened by the recorder). That makes it hard
to validate real capture on a laptop without starting a roast. This module
closes that gap: it opens EVERY configured `recording.devices` entry as its own
independent capture stream, records a few seconds to a temp directory, writes the
WAVs plus the recording sidecar, and reports each WAV's peak / RMS dBFS so a
silent or dead mic is obvious (a flat floor means the device opened but delivered
silence — muted, OS-permission-blocked, or grabbed by another process).

Unlike a real roast, the smoke test captures the FIRST (detector) device
independently too, because there is no detector loop to tee. It reuses the same
capture primitives as the roast path (`_WavStreamWriter`,
`_IndependentCaptureStream`, the configured `MicrophoneAudioInput`), so a pass
here exercises the real device-open + read path. It is injectable end to end so
the logic is unit-tested without hardware; the decisive proof is the operator's
real run on their MacBook.
"""

from __future__ import annotations

import dataclasses
import json
import math
import struct
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import cast

from coffee_roaster_mcp.audio import (
    AdditionalAudioInputFactory,
    AdditionalRecordingDevice,
    capture_devices_independently,
    device_label_to_filename,
)
from coffee_roaster_mcp.config import load_config

#: Default smoke-test capture duration.
DEFAULT_RECORD_SECONDS = 5.0
#: dBFS at or above which a stream is treated as carrying real signal (not
#: silence). -50 dBFS is comfortably above a quiet room floor but well below
#: speech / a snap; a muted or blocked device sits at or below it.
DEFAULT_SIGNAL_FLOOR_DBFS = -50.0
_READ_SECONDS = 0.1


@dataclass(frozen=True)
class RecordCheckStreamResult:
    """Per-device result of the recording smoke test.

    Attributes:
        device: Configured device-name substring.
        wav_filename: WAV file written for this device.
        sample_rate: Capture/WAV sample rate in Hz.
        frame_count: Frames written for this device.
        duration_seconds: Recorded duration in seconds.
        peak_dbfs: Peak amplitude in dBFS (`-inf` for pure silence).
        rms_dbfs: RMS level in dBFS (`-inf` for pure silence).
        has_signal: Whether `peak_dbfs` cleared the signal floor.
        error: Capture/open error for this device, if any (the stream is dropped
            but the others continue).
    """

    device: str
    wav_filename: str
    sample_rate: int
    frame_count: int
    duration_seconds: float
    peak_dbfs: float
    rms_dbfs: float
    has_signal: bool
    error: str | None = None


@dataclass(frozen=True)
class RecordCheckReport:
    """Result of a multi-device recording smoke test.

    Attributes:
        output_dir: Directory the WAVs and sidecar were written to.
        sidecar_filename: Recording sidecar JSON filename.
        record_seconds: Requested capture duration.
        signal_floor_dbfs: dBFS floor used to decide `has_signal`.
        streams: Per-device results.
        error: Top-level error (e.g. no devices configured), if any.
        passed: Every configured stream captured real signal above the floor.
    """

    output_dir: Path
    sidecar_filename: str
    record_seconds: float
    signal_floor_dbfs: float
    streams: list[RecordCheckStreamResult] = field(
        default_factory=lambda: cast(list[RecordCheckStreamResult], [])
    )
    error: str | None = None
    passed: bool = False


@dataclass(frozen=True)
class RecordCheckOptions:
    """Options for the recording smoke test.

    Attributes:
        config_path: Optional coffee-roaster-mcp YAML config path; its
            `recording.devices` (or `audio.input_device` fallback) selects the
            devices and `recording.sample_rate` (or `audio.sample_rate`) the rate.
        record_seconds: How long to capture from each device.
        signal_floor_dbfs: dBFS floor above which a stream counts as real signal.
        output_dir: Optional output directory; a temp directory is used when unset.
    """

    config_path: Path | None = None
    record_seconds: float = DEFAULT_RECORD_SECONDS
    signal_floor_dbfs: float = DEFAULT_SIGNAL_FLOOR_DBFS
    output_dir: Path | None = None


def _amplitude_to_dbfs(amplitude: float) -> float:
    """Convert a 0..1 amplitude to dBFS (`-inf` at zero)."""
    if amplitude <= 0.0:
        return -math.inf
    return round(20.0 * math.log10(min(1.0, amplitude)), 2)


def _read_wav_levels(path: Path) -> tuple[float, float]:
    """Return `(peak_dbfs, rms_dbfs)` for a 16-bit mono PCM WAV."""
    with wave.open(str(path), "rb") as wav_file:
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)
    if not raw:
        return -math.inf, -math.inf
    peak = 0.0
    sum_squares = 0.0
    count = 0
    for (value,) in struct.iter_unpack("<h", raw):
        amplitude = abs(value) / 32768.0
        peak = max(peak, amplitude)
        sum_squares += amplitude * amplitude
        count += 1
    rms = math.sqrt(sum_squares / count) if count else 0.0
    return _amplitude_to_dbfs(peak), _amplitude_to_dbfs(rms)


def run_record_check(
    options: RecordCheckOptions,
    *,
    additional_input_factory: AdditionalAudioInputFactory | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RecordCheckReport:
    """Record a few seconds from every configured device and report levels.

    Args:
        options: Smoke-test options (config, duration, floor, output dir).
        additional_input_factory: Opens a fresh input per device; defaults to the
            real configured microphone input. Injected in tests.
        sleep: Sleep function for the capture window; defaults to `time.sleep`.
            Injected in tests to avoid real waits.

    Returns:
        A :class:`RecordCheckReport`. Never raises for capture/config problems —
        they are recorded in ``error`` / per-stream ``error`` with
        ``passed=False``.
    """
    config = load_config(path=options.config_path)
    recording = config.recording
    sample_rate = recording.sample_rate or config.audio.sample_rate
    devices = list(recording.devices or ())
    if not devices and config.audio.input_device is not None:
        # Fall back to the single detector device so the smoke test still works
        # before `recording.devices` is configured.
        devices = [config.audio.input_device]

    output_dir = options.output_dir or Path(mkdtemp(prefix="roast-record-check-"))
    sidecar_path = output_dir / "record-check.json"

    if not devices:
        return RecordCheckReport(
            output_dir=output_dir,
            sidecar_filename=sidecar_path.name,
            record_seconds=options.record_seconds,
            signal_floor_dbfs=options.signal_floor_dbfs,
            error=(
                "No devices configured: set recording.devices (or audio.input_device) "
                "to smoke-test capture."
            ),
        )

    specs = [
        AdditionalRecordingDevice(
            device_label=label,
            wav_path=output_dir / f"record-check.{device_label_to_filename(label)}.wav",
            sample_rate=sample_rate,
        )
        for label in devices
    ]
    captures = capture_devices_independently(
        specs,
        record_seconds=options.record_seconds,
        sidecar_path=sidecar_path,
        session_id="record-check",
        input_factory=additional_input_factory,
        sleep=sleep,
        read_seconds=_READ_SECONDS,
    )

    results: list[RecordCheckStreamResult] = []
    for capture in captures:
        peak_dbfs = -math.inf
        rms_dbfs = -math.inf
        error: str | None = None
        if capture.wav_path.exists():
            try:
                peak_dbfs, rms_dbfs = _read_wav_levels(capture.wav_path)
            except (OSError, wave.Error) as exc:
                error = f"Could not read back {capture.wav_path.name}: {exc}"
        else:
            error = "No WAV was written (device open/read failed)."
        if capture.frame_count == 0 and error is None:
            error = "Captured no audio frames (silent or failed stream)."
        has_signal = peak_dbfs >= options.signal_floor_dbfs
        results.append(
            RecordCheckStreamResult(
                device=capture.device_label,
                wav_filename=capture.wav_path.name,
                sample_rate=capture.sample_rate,
                frame_count=capture.frame_count,
                duration_seconds=round(capture.frame_count / capture.sample_rate, 6),
                peak_dbfs=peak_dbfs,
                rms_dbfs=rms_dbfs,
                has_signal=has_signal,
                error=error,
            )
        )

    passed = bool(results) and all(r.has_signal and r.error is None for r in results)
    return RecordCheckReport(
        output_dir=output_dir,
        sidecar_filename=sidecar_path.name,
        record_seconds=options.record_seconds,
        signal_floor_dbfs=options.signal_floor_dbfs,
        streams=results,
        passed=passed,
    )


def _json_safe(value: object) -> object:
    """Recursively make a value JSON-safe.

    Paths become strings and non-finite floats (e.g. `-inf` dBFS for pure
    silence) become the strings ``"-inf"`` / ``"inf"`` / ``"nan"`` so the JSON
    stays valid.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "nan"
        return "-inf" if value < 0 else "inf"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in cast(dict[object, object], value).items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in cast(list[object], value)]
    return value


def report_to_json(report: RecordCheckReport) -> str:
    """Serialize a :class:`RecordCheckReport` to pretty JSON.

    `-inf` dBFS (pure silence) is rendered as the string ``"-inf"`` so the JSON
    stays valid.
    """
    payload = _json_safe(dataclasses.asdict(report))
    return json.dumps(payload, indent=2)


def main() -> int:  # pragma: no cover - thin CLI wiring exercised via cli.py.
    """Run the recording smoke test from `python -m coffee_roaster_mcp.record_check`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m coffee_roaster_mcp.record_check",
        description="Smoke-test multi-device roast audio capture without a roast.",
    )
    parser.add_argument("--config", type=Path, default=None, help="coffee-roaster-mcp YAML config.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_RECORD_SECONDS,
        help="Capture duration per device.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write the WAVs + sidecar (a temp dir is used when unset).",
    )
    args = parser.parse_args()
    report = run_record_check(
        RecordCheckOptions(
            config_path=args.config,
            record_seconds=args.seconds,
            output_dir=args.output_dir,
        )
    )
    print(report_to_json(report))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - module entrypoint.
    raise SystemExit(main())
