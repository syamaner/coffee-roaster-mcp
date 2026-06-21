"""Guarded Hottop validation harness coverage."""

from collections.abc import Callable
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
        self.drum_motor_on = False
        self.solenoid_open = False
        self.actions: list[str] = []
        self.disconnect_failed = False

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
                "last_command_write_size": 36 if len(self.actions) > 0 else 0,
                "command_loop_error_count": 0,
                "status_read_error_count": 0,
                "drum_motor_on": self.drum_motor_on,
                "solenoid_open": self.solenoid_open,
                "drop_triggered": self.drop_triggered,
                "emergency_stopped": self.emergency_stopped,
            },
        )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Record heat command."""
        self.actions.append(f"heat:{heat_level_percent}")
        self.heat_level_percent = heat_level_percent
        if heat_level_percent > 0:
            self.drum_motor_on = True
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
        self.heat_level_percent = 0
        # Drop keeps the drum running so beans eject through the open chute (#163).
        self.drum_motor_on = True
        self.solenoid_open = True
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
        self.solenoid_open = False
        # End-of-roast: stop the drum that drop_beans left running (#163).
        self.drum_motor_on = False
        return self.read_state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Record emergency stop."""
        _ = reason
        self.actions.append("emergency_stop")
        self.emergency_stopped = True
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        self.drum_motor_on = False
        self.solenoid_open = False
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
        )


class MissingTelemetryDriver(FakeValidationDriver):
    """Driver double with missing telemetry after connection."""

    def read_state(self) -> RoasterState:
        """Return state without temperatures or status packets."""
        state = super().read_state()
        return RoasterState(
            driver=state.driver,
            connected=state.connected,
            bean_temp_c=None,
            env_temp_c=None,
            heat_level_percent=state.heat_level_percent,
            fan_level_percent=state.fan_level_percent,
            cooling_on=state.cooling_on,
            raw_vendor_data={
                **state.raw_vendor_data,
                "status_packet_count": 0,
            },
        )


class FailingDisconnectDriver(FakeValidationDriver):
    """Driver double whose disconnect fails."""

    def disconnect(self) -> None:
        """Raise while disconnecting."""
        self.disconnect_failed = True
        raise RuntimeError("disconnect failed")


class ErrorDiagnosticDriver(FakeValidationDriver):
    """Driver double that reports command-loop errors."""

    def read_state(self) -> RoasterState:
        """Return state with a diagnostic error."""
        state = super().read_state()
        return RoasterState(
            driver=state.driver,
            connected=state.connected,
            bean_temp_c=state.bean_temp_c,
            env_temp_c=state.env_temp_c,
            heat_level_percent=state.heat_level_percent,
            fan_level_percent=state.fan_level_percent,
            cooling_on=state.cooling_on,
            raw_vendor_data={
                **state.raw_vendor_data,
                "command_loop_error_count": 1,
            },
        )


class StaleCommandWriteDriver(FakeValidationDriver):
    """Driver double that accepts commands but reports no fresh writes."""

    def read_state(self) -> RoasterState:
        """Return state with a stale command-write counter."""
        state = super().read_state()
        return RoasterState(
            driver=state.driver,
            connected=state.connected,
            bean_temp_c=state.bean_temp_c,
            env_temp_c=state.env_temp_c,
            heat_level_percent=state.heat_level_percent,
            fan_level_percent=state.fan_level_percent,
            cooling_on=state.cooling_on,
            raw_vendor_data={
                **state.raw_vendor_data,
                "command_write_count": 1,
            },
        )


class IgnoredHeatControlDriver(FakeValidationDriver):
    """Driver double that writes heat commands without updating heat state."""

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Record heat command but leave heat state unchanged."""
        self.actions.append(f"heat:{heat_level_percent}")
        return self.read_state()


class IgnoredFanControlDriver(FakeValidationDriver):
    """Driver double that writes fan commands without updating fan state."""

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Record fan command but leave fan state unchanged."""
        self.actions.append(f"fan:{fan_level_percent}")
        return self.read_state()


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


def test_hottop_validation_does_not_touch_output_before_config_preflight(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    output_path = tmp_path / "evidence" / "invalid-config.json"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ValueError, match="roaster.driver"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                output_path=output_path,
                hardware_acknowledged=True,
            ),
            driver_factory=_fake_driver_factory,
            sleeper=_no_sleep,
        )

    assert not output_path.exists()


def test_hottop_validation_rejects_invalid_control_percent_before_connect(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)
    driver = FakeValidationDriver()

    with pytest.raises(ValueError, match="heat_percent"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                hardware_acknowledged=True,
                heat_percent=101,
            ),
            driver_factory=_driver_factory(driver),
            sleeper=_no_sleep,
        )

    assert driver.actions == []


def test_hottop_validation_rejects_non_finite_durations_before_connect(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)
    driver = FakeValidationDriver()

    with pytest.raises(ValueError, match="telemetry_wait_seconds"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                hardware_acknowledged=True,
                telemetry_wait_seconds=float("inf"),
            ),
            driver_factory=_driver_factory(driver),
            sleeper=_no_sleep,
        )

    assert driver.actions == []


def test_hottop_validation_writes_evidence_with_skipped_destructive_steps(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)
    output_path = tmp_path / "evidence" / "hottop-validation.json"
    driver = FakeValidationDriver()

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            output_path=output_path,
            hardware_acknowledged=True,
        ),
        driver_factory=_driver_factory(driver),
        sleeper=_no_sleep,
    )

    assert output_path.read_text(encoding="utf-8") == report_to_json(report)
    assert report.hardware_ready_release_label_allowed is False
    assert report.port == "/dev/cu.test-hottop"
    assert report.temperature_unit == "auto"
    assert {step.name: step.status for step in report.steps}["drop"] == "skipped"
    assert {step.name: step.status for step in report.steps}["emergency_stop"] == "skipped"
    assert "emergency_stop" not in driver.actions
    assert "Do not apply a hardware-ready release label" in report.final_driver_decisions[-1]


def test_hottop_validation_can_capture_full_manual_sequence(tmp_path: Path) -> None:
    config_path = _write_hottop_config(tmp_path)
    driver = FakeValidationDriver()

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_driver_factory(driver),
        sleeper=_no_sleep,
    )

    statuses = {step.name: step.status for step in report.steps}
    assert statuses["stable_telemetry"] == "passed"
    assert statuses["drop"] == "passed"
    assert statuses["emergency_stop"] == "passed"
    assert report.hardware_ready_release_label_allowed is True
    assert "cooling:start" not in driver.actions


def test_hottop_validation_aborts_and_writes_evidence_when_telemetry_missing(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)
    output_path = tmp_path / "evidence" / "failure.json"
    driver = MissingTelemetryDriver()

    with pytest.raises(RuntimeError, match="Stable telemetry did not pass"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                output_path=output_path,
                hardware_acknowledged=True,
            ),
            driver_factory=_driver_factory(driver),
            sleeper=_no_sleep,
        )

    evidence = output_path.read_text(encoding="utf-8")
    assert '"error": "RuntimeError: Stable telemetry did not pass' in evidence
    assert '"name": "stable_telemetry"' in evidence
    assert '"name": "validation_error"' in evidence
    assert not any(action.startswith("heat:") for action in driver.actions)


def test_hottop_validation_records_disconnect_failure_evidence(tmp_path: Path) -> None:
    config_path = _write_hottop_config(tmp_path)
    output_path = tmp_path / "evidence" / "disconnect-failure.json"

    with pytest.raises(RuntimeError, match="disconnect failed"):
        run_hottop_validation(
            HottopValidationOptions(
                config_path=config_path,
                output_path=output_path,
                hardware_acknowledged=True,
            ),
            driver_factory=_driver_factory(FailingDisconnectDriver()),
            sleeper=_no_sleep,
        )

    evidence = output_path.read_text(encoding="utf-8")
    assert '"name": "disconnect"' in evidence
    assert '"error": "RuntimeError: disconnect failed"' in evidence


def test_hottop_validation_readiness_fails_on_raw_driver_errors(tmp_path: Path) -> None:
    config_path = _write_hottop_config(tmp_path)

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_driver_factory(ErrorDiagnosticDriver()),
        sleeper=_no_sleep,
    )

    assert report.hardware_ready_release_label_allowed is False
    assert any(step.status == "failed" for step in report.steps)


def test_hottop_validation_readiness_requires_fresh_command_writes(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_driver_factory(StaleCommandWriteDriver()),
        sleeper=_no_sleep,
    )

    statuses = {step.name: step.status for step in report.steps}
    assert statuses["heat"] == "failed"
    assert report.hardware_ready_release_label_allowed is False


def test_hottop_validation_readiness_requires_requested_heat_state(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_driver_factory(IgnoredHeatControlDriver()),
        sleeper=_no_sleep,
    )

    statuses = {step.name: step.status for step in report.steps}
    assert statuses["heat"] == "failed"
    assert report.hardware_ready_release_label_allowed is False


def test_hottop_validation_readiness_requires_requested_fan_state(
    tmp_path: Path,
) -> None:
    config_path = _write_hottop_config(tmp_path)

    report = run_hottop_validation(
        HottopValidationOptions(
            config_path=config_path,
            hardware_acknowledged=True,
            include_drop=True,
            include_emergency_stop=True,
        ),
        driver_factory=_driver_factory(IgnoredFanControlDriver()),
        sleeper=_no_sleep,
    )

    statuses = {step.name: step.status for step in report.steps}
    assert statuses["fan"] == "failed"
    assert report.hardware_ready_release_label_allowed is False


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


def _driver_factory(driver: RoasterDriver) -> Callable[..., RoasterDriver]:
    def factory(*args: object, **kwargs: object) -> RoasterDriver:
        _ = args
        _ = kwargs
        return driver

    return factory


def _no_sleep(seconds: float) -> None:
    _ = seconds
