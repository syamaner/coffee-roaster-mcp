"""Tests for the pre-roast microphone capture check."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from coffee_roaster_mcp.audio import AudioCaptureError
from coffee_roaster_mcp.mic_check import (
    MicCheckOptions,
    report_to_json,
    run_mic_check,
)


class ConstantAudioInput:
    """Audio input that returns a fixed-amplitude chunk on every read."""

    def __init__(self, amplitude: float) -> None:
        self._amplitude = amplitude
        self.closed = False

    def read_samples(self, sample_count: int) -> Sequence[float]:
        return tuple(self._amplitude for _ in range(sample_count))

    def close(self) -> None:
        self.closed = True


class FailingAudioInput:
    """Audio input that fails to open (device gone / OS-blocked)."""

    def read_samples(self, sample_count: int) -> Sequence[float]:
        raise AudioCaptureError("Could not open microphone audio source: boom")


def _config(tmp_path: Path, *, input_device: str = "USB PnP") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f'audio:\n  source: microphone\n  input_device: "{input_device}"\n  sample_rate: 16000\n',
        encoding="utf-8",
    )
    return path


_DEVICES = ("Built-in Microphone", "USB PnP Audio Device", "Aggregate Device")


def test_mic_check_passes_on_real_audio(tmp_path: Path) -> None:
    """A matched device delivering non-silent audio is a PASS."""
    report = run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.3),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.2),
        device_lister=lambda: _DEVICES,
    )
    assert report.passed is True
    assert report.audio_detected is True
    assert report.device_found is True
    assert report.matched_device == "USB PnP Audio Device"
    assert report.rms_max >= 0.19  # ~0.2 constant amplitude
    assert report.chunks_read == 3


def test_mic_check_fails_on_silence(tmp_path: Path) -> None:
    """A matched device delivering silence/zeros is a FAIL (the OS-blocked case)."""
    report = run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.3),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.0),
        device_lister=lambda: _DEVICES,
    )
    assert report.device_found is True  # device present + opened
    assert report.audio_detected is False  # but no real signal
    assert report.passed is False
    assert report.rms_max == 0.0


def test_mic_check_fails_when_device_not_found(tmp_path: Path) -> None:
    """The configured device not being among the inputs is a FAIL."""
    report = run_mic_check(
        MicCheckOptions(
            config_path=_config(tmp_path, input_device="Nonexistent"), duration_seconds=0.3
        ),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.2),
        device_lister=lambda: _DEVICES,
    )
    assert report.device_found is False
    assert report.matched_device is None
    assert report.passed is False


def test_mic_check_records_capture_error(tmp_path: Path) -> None:
    """A device that will not open surfaces as an error, not a crash."""
    report = run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.3),
        audio_input_factory=lambda _settings: FailingAudioInput(),
        device_lister=lambda: _DEVICES,
    )
    assert report.passed is False
    assert report.error is not None
    assert "Could not open microphone" in report.error


def test_mic_check_device_enumeration_error_is_reported(tmp_path: Path) -> None:
    """A missing sounddevice/PortAudio runtime is reported, not raised."""

    def _broken_lister() -> Sequence[str]:
        raise AudioCaptureError("Microphone enumeration requires sounddevice + PortAudio.")

    report = run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.3),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.2),
        device_lister=_broken_lister,
    )
    assert report.passed is False
    assert report.error is not None
    assert "PortAudio" in report.error


def test_on_chunk_callback_receives_levels(tmp_path: Path) -> None:
    """The live-meter callback fires once per captured chunk with (rms, peak)."""
    seen: list[tuple[float, float]] = []
    run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.3),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.2),
        device_lister=lambda: _DEVICES,
        on_chunk=lambda rms, peak: seen.append((rms, peak)),
    )
    assert len(seen) == 3
    assert all(rms > 0.0 for rms, _ in seen)


def test_report_to_json_round_trips(tmp_path: Path) -> None:
    """The report serializes to JSON evidence."""
    report = run_mic_check(
        MicCheckOptions(config_path=_config(tmp_path), duration_seconds=0.1),
        audio_input_factory=lambda _settings: ConstantAudioInput(0.2),
        device_lister=lambda: _DEVICES,
    )
    parsed = json.loads(report_to_json(report))
    assert parsed["passed"] is True
    assert parsed["matched_device"] == "USB PnP Audio Device"
    assert parsed["source"] == "microphone"
