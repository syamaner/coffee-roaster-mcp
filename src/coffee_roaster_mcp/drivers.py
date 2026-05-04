"""Roaster driver safety boundary for the current mock runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from coffee_roaster_mcp.session import EventPayloadValue, RoastSession


@dataclass(frozen=True)
class EmergencyStopResult:
    """Result returned by a driver-owned emergency stop action.

    Attributes:
        driver: Driver identifier that handled the safety action.
        safety_method: Driver method that was called.
        heat_level_percent: Heat level after the safety action.
        fan_level_percent: Fan level after the safety action.
        cooling_on: Cooling state after the safety action.
    """

    driver: str
    safety_method: str
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool

    def as_event_payload(self) -> dict[str, EventPayloadValue]:
        """Return event payload fields that prove the driver safety call ran."""
        return {
            "driver": self.driver,
            "driver_safety_method": self.safety_method,
            "driver_safety_method_called": True,
            "heat_level_percent": self.heat_level_percent,
            "fan_level_percent": self.fan_level_percent,
            "cooling_on": self.cooling_on,
        }


class RoasterSafetyDriver(Protocol):
    """Minimal driver protocol needed before the full E3 driver contract lands."""

    def emergency_stop(self, session: RoastSession, *, reason: str) -> EmergencyStopResult:
        """Apply the safest available stop behavior for one active session."""
        ...


class MockRoasterDriver:
    """Mock roaster driver with deterministic fail-closed emergency stop behavior."""

    name = "mock"

    def emergency_stop(self, session: RoastSession, *, reason: str) -> EmergencyStopResult:
        """Apply mock-safe emergency stop controls to one session."""
        _ = reason
        session.heat_level_percent = 0
        session.fan_level_percent = 100
        session.cooling_on = True
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=session.heat_level_percent,
            fan_level_percent=session.fan_level_percent,
            cooling_on=session.cooling_on,
        )


def create_roaster_safety_driver(driver_name: str) -> RoasterSafetyDriver:
    """Create the configured roaster safety driver.

    Args:
        driver_name: Roaster driver name from configuration.

    Returns:
        Driver safety adapter for the configured driver.

    Raises:
        ValueError: If the driver is not implemented in the current runtime.
    """
    if driver_name == "mock":
        return MockRoasterDriver()
    raise ValueError(
        f"Roaster driver {driver_name!r} is not implemented in this bootstrap runtime."
    )
