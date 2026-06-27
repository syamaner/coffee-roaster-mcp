from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from coffee_roaster_mcp.audio import AdditionalRecordingDevice
from coffee_roaster_mcp.record_check import (
    RecordCheckOptions,
    report_to_json,
    run_record_check,
)


class _BoundedInput:
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


def _write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(body, encoding="utf-8")
    return config_path


def test_record_check_two_devices_one_silent(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
recording:
  enabled: true
  autocapture: true
  sample_rate: 8
  devices:
    - USB PnP
    - ATR2100x
audio:
  sample_rate: 8
""",
    )
    amplitudes = {"USB PnP": 0.5, "ATR2100x": 0.0}

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=amplitudes[device.device_label], reads=4)

    report = run_record_check(
        RecordCheckOptions(
            config_path=config_path,
            record_seconds=0.05,
            output_dir=tmp_path / "out",
        ),
        additional_input_factory=factory,
        sleep=lambda _: None,
    )

    assert [stream.device for stream in report.streams] == ["USB PnP", "ATR2100x"]
    # Two WAVs written.
    assert (tmp_path / "out" / "record-check.usb-pnp.wav").exists()
    assert (tmp_path / "out" / "record-check.atr2100x.wav").exists()
    assert (tmp_path / "out" / "record-check.json").exists()
    # The live device cleared the floor; the silent device did not.
    live = next(s for s in report.streams if s.device == "USB PnP")
    silent = next(s for s in report.streams if s.device == "ATR2100x")
    assert live.has_signal is True
    assert live.peak_dbfs > report.signal_floor_dbfs
    assert silent.has_signal is False
    assert silent.peak_dbfs == -math.inf
    # One silent device fails the overall smoke test.
    assert report.passed is False


def test_record_check_all_live_passes(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
recording:
  enabled: true
  autocapture: true
  sample_rate: 8
  devices:
    - USB PnP
    - ATR2100x
audio:
  sample_rate: 8
""",
    )

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=0.5, reads=4)

    report = run_record_check(
        RecordCheckOptions(config_path=config_path, record_seconds=0.05, output_dir=tmp_path / "o"),
        additional_input_factory=factory,
        sleep=lambda _: None,
    )

    assert report.passed is True
    assert all(stream.has_signal for stream in report.streams)
    # report_to_json keeps -inf JSON-safe and is parseable.
    import json

    parsed = json.loads(report_to_json(report))
    assert parsed["passed"] is True
    assert len(parsed["streams"]) == 2


def test_record_check_falls_back_to_audio_input_device(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
recording:
  enabled: true
  autocapture: true
audio:
  input_device: USB PnP
  sample_rate: 8
""",
    )

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=0.5, reads=4)

    report = run_record_check(
        RecordCheckOptions(config_path=config_path, record_seconds=0.05, output_dir=tmp_path / "o"),
        additional_input_factory=factory,
        sleep=lambda _: None,
    )

    assert [stream.device for stream in report.streams] == ["USB PnP"]
    assert report.passed is True


def test_record_check_no_devices_errors(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        "recording:\n  enabled: true\n  autocapture: true\naudio:\n  sample_rate: 8\n",
    )

    report = run_record_check(
        RecordCheckOptions(config_path=config_path, output_dir=tmp_path / "o"),
        additional_input_factory=lambda _device: _BoundedInput(0.5, 1),
        sleep=lambda _: None,
    )

    assert report.error is not None
    assert "No devices configured" in report.error
    assert report.passed is False
    assert report.streams == []


def test_record_check_silent_zero_frame_flagged(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
recording:
  enabled: true
  autocapture: true
  sample_rate: 8
  devices:
    - DEAD
audio:
  sample_rate: 8
""",
    )

    def factory(device: AdditionalRecordingDevice) -> _BoundedInput:
        return _BoundedInput(amplitude=0.0, reads=0)  # opens but delivers nothing

    report = run_record_check(
        RecordCheckOptions(config_path=config_path, record_seconds=0.02, output_dir=tmp_path / "o"),
        additional_input_factory=factory,
        sleep=lambda _: None,
    )

    assert len(report.streams) == 1
    stream = report.streams[0]
    assert stream.frame_count == 0
    assert stream.error is not None
    assert report.passed is False


def test_record_check_catches_capture_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #3: a capture-layer error becomes a report, never a raised crash."""
    import coffee_roaster_mcp.record_check as record_check_module
    from coffee_roaster_mcp.audio import AudioCaptureError

    config_path = _write_config(
        tmp_path,
        """
recording:
  enabled: true
  autocapture: true
  sample_rate: 8
  devices:
    - USB PnP
audio:
  sample_rate: 8
""",
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AudioCaptureError("WAV open failed")

    monkeypatch.setattr(record_check_module, "capture_devices_independently", boom)

    report = run_record_check(
        RecordCheckOptions(config_path=config_path, record_seconds=0.0, output_dir=tmp_path / "o"),
        additional_input_factory=lambda _device: _BoundedInput(0.5, 1),
        sleep=lambda _: None,
    )

    assert report.error is not None
    assert "Recording failed" in report.error
    assert report.passed is False
    assert report.streams == []
