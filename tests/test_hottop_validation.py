"""Guarded Hottop validation harness coverage."""

from pathlib import Path

import pytest

from coffee_roaster_mcp.drivers import (
    CommandStreaming,
    ControlRange,
    EmergencyStopResult,
    RoasterCapabilities,
    RoasterDriver,
    RoasterState,
    SensorUnits,
    SupportedActions,
)
from coffee_roaster_mcp.hottop_validation import (
    HottopValidationOptions,
    report_to_json,
    run_hottop_validation,
)


class FakeValidationDriver:
    """Driver double that records validation commands without hardware."""

    name = "hottop_kn8828b_2k_plus"

    def __init__(self) -> None:
        self.connected = False
        self.heat_level_percent = 0
        self.fan_level_percent = 0
        self.cooling_on = False
        self.drop_triggered = False
        self.emergency_stopped = False
        self.actions: list[str] = []

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return Hottop-like capabilities for protocol completeness."""
        return RoasterCapabilities(
            driver=self.name,
            heat=ControlRange(minimum=0, maximum=100, step=1),
            fan=ControlRange(minimum=0, maximum=100, step=1),
            actions=SupportedActions(
                heat_control=True,
                fan_control=True,
                bean_drop=True,
                cooling_control=True,
                emergency_stop=True,
            ),
            sensor_units=SensorUnits(
                bean_temperature="celsius",
                environment_temperature="celsius",
            ),
            command_streaming=CommandStreaming(required=True, interval_seconds=0.3),
        )

    def connect(self) -> None:
        """Record connection."""
        self.actions.append("connect")
        self.connected = True

    def disconnect(self) -> None:
        """Record disconnection."""
        self.actions.append("disconnect")
        self.connected = False

    def read_state(self) -> RoasterState:
        """Return deterministic validation state."""
        return RoasterState(
            driver=self.name,
            connected=self.connected,
            bean_temp_c=25.0,
            env_temp_c=26.0,
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
            raw_vendor_data={
                "status_packet_count": 1,
                "command_write_count": len(self.actions),
                "drop_triggered": self.drop_triggered,
                "emergency_stopped": self.emergency_stopped,
            },
        )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Record heat command."""
        self.actions.append(f"heat:{heat_level_percent}")
        self.heat_level_percent = heat_level_percent
        return self.read_state()

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Record fan command."""
        self.actions.append(f"fan:{fan_level_percent}")
        self.fan_level_percent = fan_level_percent
        return self.read_state()

    def drop_beans(self) -> RoasterState:
        """Record drop command."""
        self.actions.append("drop")
        self.drop_triggered = True
        self.cooling_on = True
        self.fan_level_percent = 100
        return self.read_state()

    def start_cooling(self) -> RoasterState:
        """Record cooling start."""
        self.actions.append("cooling:start")
        self.cooling_on = True
        self.fan_level_percent = 100
        return self.read_state()

    def stop_cooling(self) -> RoasterState:
        """Record cooling stop."""
        self.actions.append("cooling:stop")
        self.cooling_on = False
        self.fan_level_percent = 0
        return self.read_state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Record emergency stop."""
        _ = reason
        self.actions.append("emergency_stop")
        self.emergency_stopped = True
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
        )


def test_hottop_validation_requires_hardware_acknowledgement(tmp_path: Path) -> None:
    config_path = _write_hottop_config(tmp_path)

    with pytest.raises(ValueError, match="hardware acknowledgement"):
        run_hottop_validation(HottopValidationOptions(config_path=config_path))


def test_hottop_validation_requires_hottop_config(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ValueError, match="roaster.driver"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                hardware_acknowledged=True,
            ),
            driver_factory=_fake_driver_factory,
            sleeper=_no_sleep,
        )


def test_hottop_validation_writes_evidence_with_skipped_destructive_steps(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)
    output_path = tmp_path / "evidence" / "hottop-validation.json"

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            output_path=output_path,
            hardware_acknowledged=True,
        ),
        driver_factory=_fake_driver_factory,
        sleeper=_no_sleep,
    )

    assert output_path.read_text(encoding="utf-8") == report_to_json(report)
    assert report.hardware_ready_release_label_allowed is False
    assert report.port == "/dev/cu.test-hottop"
    assert report.temperature_unit == "auto"
    assert {step.name: step.status for step in report.steps}["drop"] == "skipped"
    assert {step.name: step.status for step in report.steps}["emergency_stop"] == "skipped"
    assert "Do not apply a hardware-ready release label" in report.final_driver_decisions[-1]


def test_hottop_validation_can_capture_full_manual_sequence(tmp_path: Path) -> None:
    config_path = _write_hottop_config(tmp_path)

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_fake_driver_factory,
        sleeper=_no_sleep,
    )

    statuses = {step.name: step.status for step in report.steps}
    assert statuses["stable_telemetry"] == "passed"
    assert statuses["drop"] == "passed"
    assert statuses["emergency_stop"] == "passed"
    assert report.hardware_ready_release_label_allowed is True


def _write_hottop_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "roaster:",
                "  driver: hottop_kn8828b_2k_plus",
                "  port: /dev/cu.test-hottop",
                "  temperature_unit: auto",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _fake_driver_factory(*args: object, **kwargs: object) -> RoasterDriver:
    _ = args
    _ = kwargs
    return FakeValidationDriver()


def _no_sleep(seconds: float) -> None:
    _ = seconds
