"""Guarded manual validation harness for Hottop integration checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import sleep
from typing import Literal

from coffee_roaster_mcp.config import load_config
from coffee_roaster_mcp.controls import validate_control_percent
from coffee_roaster_mcp.drivers import (
    HOTTOP_DRIVER_NAME,
    RoasterDriver,
    RoasterState,
    create_roaster_driver,
)
from coffee_roaster_mcp.session import EventPayloadValue

ValidationStatus = Literal["passed", "skipped", "needs_review", "failed"]


@dataclass(frozen=True)
class HottopValidationOptions:
    """Operator-selected options for one Hottop validation run.

    Attributes:
        config_path: Optional config file path.
        output_path: Optional JSON evidence file path.
        hardware_acknowledged: Whether the operator confirmed hardware control.
        heat_percent: Conservative heat percentage used for the heat step.
        fan_percent: Conservative fan percentage used for the fan step.
        step_duration_seconds: Delay after each hardware command.
        telemetry_wait_seconds: Initial delay used to collect status packets.
        include_drop: Whether to run the irreversible bean-drop command.
        include_emergency_stop: Whether to run the emergency-stop command.
    """

    config_path: Path | None = None
    output_path: Path | None = None
    hardware_acknowledged: bool = False
    heat_percent: int = 10
    fan_percent: int = 30
    step_duration_seconds: float = 2.0
    telemetry_wait_seconds: float = 5.0
    include_drop: bool = False
    include_emergency_stop: bool = False


@dataclass(frozen=True)
class HottopValidationStep:
    """Evidence captured for one manual Hottop validation step."""

    name: str
    status: ValidationStatus
    recorded_at_utc: str
    note: str
    state: dict[str, EventPayloadValue]


@dataclass(frozen=True)
class HottopValidationReport:
    """JSON-serializable report for one guarded Hottop validation run."""

    started_at_utc: str
    completed_at_utc: str
    config_source: str | None
    driver: str
    port: str
    baudrate: int
    temperature_unit: str
    command_interval_seconds: float
    heat_percent: int
    fan_percent: int
    step_duration_seconds: float
    telemetry_wait_seconds: float
    steps: tuple[HottopValidationStep, ...]
    final_driver_decisions: tuple[str, ...]
    hardware_ready_release_label_allowed: bool
    error: str | None = None


def run_hottop_validation(
    options: HottopValidationOptions,
    *,
    driver_factory: Callable[..., RoasterDriver] = create_roaster_driver,
    sleeper: Callable[[float], None] = sleep,
) -> HottopValidationReport:
    """Run a guarded Hottop validation sequence and optionally write evidence.

    Args:
        options: Operator-selected validation options.
        driver_factory: Driver factory override for tests.
        sleeper: Delay function override for tests.

    Returns:
        The validation report.

    Raises:
        ValueError: If required Hottop config or hardware acknowledgement is missing.
    """
    if not options.hardware_acknowledged:
        raise ValueError("Hottop validation requires explicit hardware acknowledgement.")
    _validate_duration(options.step_duration_seconds, label="step_duration_seconds")
    _validate_duration(options.telemetry_wait_seconds, label="telemetry_wait_seconds")
    heat_percent = validate_control_percent(options.heat_percent, label="heat_percent")
    fan_percent = validate_control_percent(options.fan_percent, label="fan_percent")
    config = load_config(path=options.config_path)
    if config.roaster.driver != HOTTOP_DRIVER_NAME:
        raise ValueError(f"roaster.driver must be {HOTTOP_DRIVER_NAME!r}.")
    if config.roaster.port is None:
        raise ValueError("roaster.port is required for Hottop validation.")
    _preflight_output_path(options.output_path)

    driver = driver_factory(
        config.roaster.driver,
        port=config.roaster.port,
        baudrate=config.roaster.baudrate,
        temperature_unit=config.roaster.temperature_unit,
        command_interval_seconds=config.roaster.command_interval_seconds,
    )

    started_at_utc = _utc_now()
    steps: list[HottopValidationStep] = []
    error: str | None = None
    disconnect_error: str | None = None
    try:
        driver.connect()
        steps.append(_capture_step("connect", "passed", "Driver connected.", driver.read_state()))

        sleeper(options.telemetry_wait_seconds)
        telemetry_state = driver.read_state()
        telemetry_status = _telemetry_status(telemetry_state)
        steps.append(
            _capture_step(
                "stable_telemetry",
                telemetry_status,
                "Captured post-connect telemetry and raw diagnostics.",
                telemetry_state,
            )
        )
        if telemetry_status != "passed":
            raise RuntimeError("Stable telemetry did not pass; aborting before control commands.")

        previous_command_write_count = _command_write_count(driver.read_state())
        driver.set_heat(heat_level_percent=heat_percent)
        sleeper(options.step_duration_seconds)
        heat_state = driver.read_state()
        steps.append(
            _capture_step(
                "heat",
                _control_step_status(
                    heat_state,
                    previous_command_write_count=previous_command_write_count,
                    expected_heat_percent=heat_percent,
                ),
                f"Set heat to {heat_percent} percent.",
                heat_state,
            )
        )

        previous_command_write_count = _command_write_count(driver.read_state())
        driver.set_heat(heat_level_percent=0)
        sleeper(options.step_duration_seconds)
        heat_off_state = driver.read_state()
        steps.append(
            _capture_step(
                "heat_off",
                _control_step_status(
                    heat_off_state,
                    previous_command_write_count=previous_command_write_count,
                    expected_heat_percent=0,
                ),
                "Set heat back to zero.",
                heat_off_state,
            )
        )

        previous_command_write_count = _command_write_count(driver.read_state())
        driver.set_fan(fan_level_percent=fan_percent)
        sleeper(options.step_duration_seconds)
        fan_state = driver.read_state()
        steps.append(
            _capture_step(
                "fan",
                _control_step_status(
                    fan_state,
                    previous_command_write_count=previous_command_write_count,
                    expected_fan_percent=fan_percent,
                ),
                f"Set fan to {fan_percent} percent.",
                fan_state,
            )
        )

        if options.include_drop:
            driver.set_fan(fan_level_percent=0)
            sleeper(options.step_duration_seconds)
            previous_command_write_count = _command_write_count(driver.read_state())
            driver.drop_beans()
            sleeper(options.step_duration_seconds)
            drop_state = driver.read_state()
            steps.append(
                _capture_step(
                    "drop",
                    _drop_step_status(
                        drop_state,
                        previous_command_write_count=previous_command_write_count,
                    ),
                    "Triggered bean drop command.",
                    drop_state,
                )
            )
        else:
            steps.append(_skipped_step("drop", "Skipped; rerun with --include-drop."))
            previous_command_write_count = _command_write_count(driver.read_state())
            driver.start_cooling()
            sleeper(options.step_duration_seconds)
            cooling_start_state = driver.read_state()
            steps.append(
                _capture_step(
                    "cooling_start",
                    _cooling_start_status(
                        cooling_start_state,
                        previous_command_write_count=previous_command_write_count,
                    ),
                    "Started cooling.",
                    cooling_start_state,
                )
            )

        previous_command_write_count = _command_write_count(driver.read_state())
        driver.stop_cooling()
        sleeper(options.step_duration_seconds)
        cooling_stop_state = driver.read_state()
        steps.append(
            _capture_step(
                "cooling_stop",
                _cooling_stop_status(
                    cooling_stop_state,
                    previous_command_write_count=previous_command_write_count,
                ),
                "Stopped cooling.",
                cooling_stop_state,
            )
        )

        if options.include_emergency_stop:
            previous_command_write_count = _command_write_count(driver.read_state())
            driver.emergency_stop(reason="manual Hottop validation")
            sleeper(options.step_duration_seconds)
            emergency_stop_state = driver.read_state()
            steps.append(
                _capture_step(
                    "emergency_stop",
                    _emergency_stop_status(
                        emergency_stop_state,
                        previous_command_write_count=previous_command_write_count,
                    ),
                    "Triggered emergency-stop command.",
                    emergency_stop_state,
                )
            )
        else:
            steps.append(
                _skipped_step(
                    "emergency_stop",
                    "Skipped; rerun with --include-emergency-stop.",
                )
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        steps.append(
            HottopValidationStep(
                name="validation_error",
                status="failed",
                recorded_at_utc=_utc_now(),
                note=error,
                state={},
            )
        )
        raise
    finally:
        try:
            driver.disconnect()
        except Exception as exc:
            disconnect_error = f"{type(exc).__name__}: {exc}"
            steps.append(
                HottopValidationStep(
                    name="disconnect",
                    status="failed",
                    recorded_at_utc=_utc_now(),
                    note=disconnect_error,
                    state={},
                )
            )
            if error is None:
                error = disconnect_error
                raise
        finally:
            report = _build_report(
                config_source=str(config.source_path) if config.source_path is not None else None,
                driver=config.roaster.driver,
                port=config.roaster.port,
                baudrate=config.roaster.baudrate,
                temperature_unit=config.roaster.temperature_unit,
                command_interval_seconds=config.roaster.command_interval_seconds,
                heat_percent=heat_percent,
                fan_percent=fan_percent,
                step_duration_seconds=options.step_duration_seconds,
                telemetry_wait_seconds=options.telemetry_wait_seconds,
                started_at_utc=started_at_utc,
                steps=steps,
                error=error,
            )
            _write_report_if_requested(report, options.output_path)

    return report


def report_to_json(report: HottopValidationReport) -> str:
    """Return a stable JSON representation of a Hottop validation report."""
    return _report_to_json(report)


def _capture_step(
    name: str,
    status: ValidationStatus,
    note: str,
    state: RoasterState,
) -> HottopValidationStep:
    return HottopValidationStep(
        name=name,
        status=status,
        recorded_at_utc=_utc_now(),
        note=note,
        state=_state_to_evidence(state),
    )


def _skipped_step(name: str, note: str) -> HottopValidationStep:
    return HottopValidationStep(
        name=name,
        status="skipped",
        recorded_at_utc=_utc_now(),
        note=note,
        state={},
    )


def _state_to_evidence(state: RoasterState) -> dict[str, EventPayloadValue]:
    evidence: dict[str, EventPayloadValue] = {
        "driver": state.driver,
        "connected": state.connected,
        "bean_temp_c": state.bean_temp_c,
        "env_temp_c": state.env_temp_c,
        "heat_level_percent": state.heat_level_percent,
        "fan_level_percent": state.fan_level_percent,
        "cooling_on": state.cooling_on,
    }
    for key, value in state.raw_vendor_data.items():
        evidence[f"raw.{key}"] = value
    return evidence


def _telemetry_status(state: RoasterState) -> ValidationStatus:
    if _raw_int(state, "status_read_error_count") > 0:
        return "failed"
    if _raw_int(state, "status_packet_count") <= 0:
        return "needs_review"
    if state.bean_temp_c is None or state.env_temp_c is None:
        return "needs_review"
    return "passed"


def _control_step_status(
    state: RoasterState,
    *,
    previous_command_write_count: int,
    expected_heat_percent: int | None = None,
    expected_fan_percent: int | None = None,
) -> ValidationStatus:
    if _has_driver_errors(state):
        return "failed"
    if _command_write_count(state) <= previous_command_write_count:
        return "failed"
    if expected_heat_percent is not None and state.heat_level_percent != expected_heat_percent:
        return "failed"
    if expected_fan_percent is not None and state.fan_level_percent != expected_fan_percent:
        return "failed"
    return "passed"


def _drop_step_status(
    state: RoasterState,
    *,
    previous_command_write_count: int,
) -> ValidationStatus:
    if (
        _control_step_status(
            state,
            previous_command_write_count=previous_command_write_count,
        )
        == "failed"
    ):
        return "failed"
    if (
        state.heat_level_percent != 0
        or not state.cooling_on
        or state.fan_level_percent != 100
        or _raw_bool(state, "drum_motor_on")
        or not _raw_bool(state, "solenoid_open")
    ):
        return "failed"
    return "passed"


def _cooling_start_status(
    state: RoasterState,
    *,
    previous_command_write_count: int,
) -> ValidationStatus:
    if (
        _control_step_status(
            state,
            previous_command_write_count=previous_command_write_count,
        )
        == "failed"
    ):
        return "failed"
    if not state.cooling_on or state.fan_level_percent != 100:
        return "failed"
    return "passed"


def _cooling_stop_status(
    state: RoasterState,
    *,
    previous_command_write_count: int,
) -> ValidationStatus:
    if (
        _control_step_status(
            state,
            previous_command_write_count=previous_command_write_count,
        )
        == "failed"
    ):
        return "failed"
    if state.cooling_on or state.fan_level_percent != 0 or _raw_bool(state, "solenoid_open"):
        return "failed"
    return "passed"


def _emergency_stop_status(
    state: RoasterState,
    *,
    previous_command_write_count: int,
) -> ValidationStatus:
    if (
        _control_step_status(
            state,
            previous_command_write_count=previous_command_write_count,
        )
        == "failed"
    ):
        return "failed"
    if (
        state.heat_level_percent != 0
        or state.fan_level_percent != 100
        or not state.cooling_on
        or _raw_bool(state, "drum_motor_on")
        or _raw_bool(state, "solenoid_open")
    ):
        return "failed"
    return "passed"


def _driver_decisions(steps: list[HottopValidationStep]) -> tuple[str, ...]:
    decisions = [
        "Keep Hottop control commands behind explicit hardware validation evidence.",
        "Keep MCP roast-session semantics unchanged until driver control is wired deliberately.",
    ]
    if not _all_required_steps_passed(steps):
        decisions.append("Do not apply a hardware-ready release label from this run.")
    else:
        decisions.append("Manual evidence supports considering the Hottop path hardware-ready.")
    return tuple(decisions)


def _all_required_steps_passed(steps: list[HottopValidationStep]) -> bool:
    required_names = {
        "connect",
        "stable_telemetry",
        "heat",
        "heat_off",
        "fan",
        "drop",
        "cooling_stop",
        "emergency_stop",
    }
    statuses_by_name = {step.name: step.status for step in steps}
    return all(statuses_by_name.get(name) == "passed" for name in required_names) and all(
        step.status != "failed" for step in steps
    )


def _build_report(
    *,
    config_source: str | None,
    driver: str,
    port: str,
    baudrate: int,
    temperature_unit: str,
    command_interval_seconds: float,
    heat_percent: int,
    fan_percent: int,
    step_duration_seconds: float,
    telemetry_wait_seconds: float,
    started_at_utc: str,
    steps: list[HottopValidationStep],
    error: str | None,
) -> HottopValidationReport:
    return HottopValidationReport(
        started_at_utc=started_at_utc,
        completed_at_utc=_utc_now(),
        config_source=config_source,
        driver=driver,
        port=port,
        baudrate=baudrate,
        temperature_unit=temperature_unit,
        command_interval_seconds=command_interval_seconds,
        heat_percent=heat_percent,
        fan_percent=fan_percent,
        step_duration_seconds=step_duration_seconds,
        telemetry_wait_seconds=telemetry_wait_seconds,
        steps=tuple(steps),
        final_driver_decisions=_driver_decisions(steps),
        hardware_ready_release_label_allowed=_all_required_steps_passed(steps),
        error=error,
    )


def _write_report_if_requested(
    report: HottopValidationReport,
    output_path: Path | None,
) -> None:
    if output_path is None:
        return
    output_path.write_text(_report_to_json(report), encoding="utf-8")


def _preflight_output_path(output_path: Path | None) -> None:
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8"):
        pass


def _validate_duration(value: float, *, label: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and >= 0.")


def _has_driver_errors(state: RoasterState) -> bool:
    return (
        _raw_int(state, "command_loop_error_count") > 0
        or _raw_int(state, "status_read_error_count") > 0
        or _raw_int(state, "last_command_write_size") not in {0, 36}
    )


def _command_write_count(state: RoasterState) -> int:
    return _raw_int(state, "command_write_count")


def _raw_int(state: RoasterState, key: str) -> int:
    value = state.raw_vendor_data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _raw_bool(state: RoasterState, key: str) -> bool:
    value = state.raw_vendor_data.get(key)
    return value if isinstance(value, bool) else False


def _report_to_json(report: HottopValidationReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
