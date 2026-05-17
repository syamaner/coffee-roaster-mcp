"""Guarded manual validation harness for Hottop integration checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Literal

from coffee_roaster_mcp.config import load_config
from coffee_roaster_mcp.drivers import (
    HOTTOP_DRIVER_NAME,
    RoasterDriver,
    RoasterState,
    create_roaster_driver,
)
from coffee_roaster_mcp.session import EventPayloadValue

ValidationStatus = Literal["passed", "skipped", "needs_review"]


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
    if options.step_duration_seconds < 0:
        raise ValueError("step_duration_seconds must be >= 0.")
    if options.telemetry_wait_seconds < 0:
        raise ValueError("telemetry_wait_seconds must be >= 0.")

    config = load_config(path=options.config_path)
    if config.roaster.driver != HOTTOP_DRIVER_NAME:
        raise ValueError(f"roaster.driver must be {HOTTOP_DRIVER_NAME!r}.")
    if config.roaster.port is None:
        raise ValueError("roaster.port is required for Hottop validation.")

    driver = driver_factory(
        config.roaster.driver,
        port=config.roaster.port,
        baudrate=config.roaster.baudrate,
        temperature_unit=config.roaster.temperature_unit,
        command_interval_seconds=config.roaster.command_interval_seconds,
    )

    started_at_utc = _utc_now()
    steps: list[HottopValidationStep] = []
    try:
        driver.connect()
        steps.append(_capture_step("connect", "passed", "Driver connected.", driver.read_state()))

        sleeper(options.telemetry_wait_seconds)
        steps.append(
            _capture_step(
                "stable_telemetry",
                _telemetry_status(driver.read_state()),
                "Captured post-connect telemetry and raw diagnostics.",
                driver.read_state(),
            )
        )

        driver.set_heat(heat_level_percent=options.heat_percent)
        sleeper(options.step_duration_seconds)
        steps.append(
            _capture_step(
                "heat",
                "passed",
                f"Set heat to {options.heat_percent} percent.",
                driver.read_state(),
            )
        )

        driver.set_heat(heat_level_percent=0)
        sleeper(options.step_duration_seconds)
        steps.append(
            _capture_step("heat_off", "passed", "Set heat back to zero.", driver.read_state())
        )

        driver.set_fan(fan_level_percent=options.fan_percent)
        sleeper(options.step_duration_seconds)
        steps.append(
            _capture_step(
                "fan",
                "passed",
                f"Set fan to {options.fan_percent} percent.",
                driver.read_state(),
            )
        )

        driver.start_cooling()
        sleeper(options.step_duration_seconds)
        steps.append(
            _capture_step("cooling_start", "passed", "Started cooling.", driver.read_state())
        )

        if options.include_drop:
            driver.drop_beans()
            sleeper(options.step_duration_seconds)
            steps.append(
                _capture_step(
                    "drop",
                    "passed",
                    "Triggered bean drop command.",
                    driver.read_state(),
                )
            )
        else:
            steps.append(_skipped_step("drop", "Skipped; rerun with --include-drop."))

        driver.stop_cooling()
        sleeper(options.step_duration_seconds)
        steps.append(
            _capture_step("cooling_stop", "passed", "Stopped cooling.", driver.read_state())
        )

        if options.include_emergency_stop:
            driver.emergency_stop(reason="manual Hottop validation")
            sleeper(options.step_duration_seconds)
            steps.append(
                _capture_step(
                    "emergency_stop",
                    "passed",
                    "Triggered emergency-stop command.",
                    driver.read_state(),
                )
            )
        else:
            steps.append(
                _skipped_step(
                    "emergency_stop",
                    "Skipped; rerun with --include-emergency-stop.",
                )
            )
    finally:
        driver.disconnect()

    report = HottopValidationReport(
        started_at_utc=started_at_utc,
        completed_at_utc=_utc_now(),
        config_source=str(config.source_path) if config.source_path is not None else None,
        driver=config.roaster.driver,
        port=config.roaster.port,
        baudrate=config.roaster.baudrate,
        temperature_unit=config.roaster.temperature_unit,
        command_interval_seconds=config.roaster.command_interval_seconds,
        heat_percent=options.heat_percent,
        fan_percent=options.fan_percent,
        step_duration_seconds=options.step_duration_seconds,
        telemetry_wait_seconds=options.telemetry_wait_seconds,
        steps=tuple(steps),
        final_driver_decisions=_driver_decisions(steps),
        hardware_ready_release_label_allowed=_all_required_steps_passed(steps),
    )
    if options.output_path is not None:
        options.output_path.parent.mkdir(parents=True, exist_ok=True)
        options.output_path.write_text(_report_to_json(report), encoding="utf-8")
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
    if state.bean_temp_c is None or state.env_temp_c is None:
        return "needs_review"
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
        "cooling_start",
        "cooling_stop",
        "emergency_stop",
    }
    statuses_by_name = {step.name: step.status for step in steps}
    return all(statuses_by_name.get(name) == "passed" for name in required_names)


def _report_to_json(report: HottopValidationReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
