"""Roaster driver contract and mock-safe implementation for RoastPilot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from coffee_roaster_mcp.session import EventPayloadValue

ReportedTemperatureUnit = Literal["celsius", "unknown"]


def _raw_vendor_data_default() -> dict[str, EventPayloadValue]:
    """Return an empty raw vendor data mapping."""
    return {}


@dataclass(frozen=True)
class ControlRange:
    """Supported integer range for one roaster control.

    Attributes:
        minimum: Lowest accepted control value.
        maximum: Highest accepted control value.
        step: Smallest supported increment.
        unit: Human-readable control unit.
    """

    minimum: int
    maximum: int
    step: int
    unit: str = "percent"


@dataclass(frozen=True)
class SupportedActions:
    """Actions supported by one roaster driver.

    Attributes:
        heat_control: Whether heat percentage commands are supported.
        fan_control: Whether fan percentage commands are supported.
        bean_drop: Whether bean drop commands are supported.
        cooling_control: Whether cooling start and stop commands are supported.
        emergency_stop: Whether driver-owned emergency stop is supported.
    """

    heat_control: bool
    fan_control: bool
    bean_drop: bool
    cooling_control: bool
    emergency_stop: bool


@dataclass(frozen=True)
class SensorUnits:
    """Temperature units emitted by normalized driver state.

    Raw hardware may report Celsius or Fahrenheit, but `RoasterState`
    temperature fields are normalized to Celsius before crossing the driver
    boundary. `unknown` is reserved for drivers that cannot provide a sensor.

    Attributes:
        bean_temperature: Unit for normalized bean temperature readings.
        environment_temperature: Unit for normalized environment temperature readings.
    """

    bean_temperature: ReportedTemperatureUnit
    environment_temperature: ReportedTemperatureUnit


@dataclass(frozen=True)
class CommandStreaming:
    """Command streaming requirements for one roaster driver.

    Attributes:
        required: Whether the driver needs continuous command streaming.
        interval_seconds: Required command interval, when streaming is required.
    """

    required: bool
    interval_seconds: float | None = None


@dataclass(frozen=True)
class RoasterCapabilities:
    """Static capabilities exposed by one roaster driver.

    Attributes:
        driver: Stable driver identifier.
        heat: Supported heat range.
        fan: Supported fan range.
        actions: Supported control actions.
        sensor_units: Units used by normalized temperature readings.
        command_streaming: Driver command-streaming requirements.
    """

    driver: str
    heat: ControlRange
    fan: ControlRange
    actions: SupportedActions
    sensor_units: SensorUnits
    command_streaming: CommandStreaming


@dataclass(frozen=True)
class RoasterState:
    """Normalized roaster state returned by the driver boundary.

    Attributes:
        driver: Stable driver identifier.
        connected: Whether the driver is connected to its roaster backend.
        bean_temp_c: Normalized bean temperature in Celsius when available.
        env_temp_c: Normalized environment temperature in Celsius when available.
        heat_level_percent: Current heat control level.
        fan_level_percent: Current fan control level.
        cooling_on: Whether cooling is active.
        raw_vendor_data: Optional vendor-specific fields for diagnostics.
    """

    driver: str
    connected: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    raw_vendor_data: dict[str, EventPayloadValue] = field(default_factory=_raw_vendor_data_default)


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


class RoasterDriver(Protocol):
    """Roaster driver interface used by the RoastPilot runtime."""

    name: str

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return static driver capabilities."""
        ...

    def connect(self) -> None:
        """Open the driver connection lifecycle."""
        ...

    def disconnect(self) -> None:
        """Close the driver connection lifecycle."""
        ...

    def read_state(self) -> RoasterState:
        """Return the latest normalized roaster state."""
        ...

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Apply a heat control update and return normalized state."""
        ...

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Apply a fan control update and return normalized state."""
        ...

    def drop_beans(self) -> RoasterState:
        """Trigger the bean drop action and return normalized state."""
        ...

    def start_cooling(self) -> RoasterState:
        """Start cooling and return normalized state."""
        ...

    def stop_cooling(self) -> RoasterState:
        """Stop cooling and return normalized state."""
        ...

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Apply the safest available driver-owned stop behavior."""
        ...


RoasterSafetyDriver = RoasterDriver


class MockRoasterDriver:
    """Mock roaster driver with deterministic local-only control behavior."""

    name = "mock"

    def __init__(self) -> None:
        """Initialize deterministic mock roaster state."""
        self._connected = False
        self._heat_level_percent = 0
        self._fan_level_percent = 0
        self._cooling_on = False
        self._beans_dropped = False

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return mock-safe capabilities for local development."""
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
            command_streaming=CommandStreaming(required=False),
        )

    def connect(self) -> None:
        """Mark the mock driver connected."""
        self._connected = True

    def disconnect(self) -> None:
        """Mark the mock driver disconnected."""
        self._connected = False

    def read_state(self) -> RoasterState:
        """Return deterministic mock roaster state."""
        return RoasterState(
            driver=self.name,
            connected=self._connected,
            bean_temp_c=None,
            env_temp_c=None,
            heat_level_percent=self._heat_level_percent,
            fan_level_percent=self._fan_level_percent,
            cooling_on=self._cooling_on,
            raw_vendor_data={"beans_dropped": self._beans_dropped},
        )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Set mock heat and return normalized state."""
        self._heat_level_percent = _validate_control_percent(
            heat_level_percent,
            label="heat_level_percent",
        )
        return self.read_state()

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Set mock fan and return normalized state."""
        self._fan_level_percent = _validate_control_percent(
            fan_level_percent,
            label="fan_level_percent",
        )
        return self.read_state()

    def drop_beans(self) -> RoasterState:
        """Record a mock bean drop and force heat off."""
        self._beans_dropped = True
        self._heat_level_percent = 0
        return self.read_state()

    def start_cooling(self) -> RoasterState:
        """Start mock cooling and return normalized state."""
        self._cooling_on = True
        return self.read_state()

    def stop_cooling(self) -> RoasterState:
        """Stop mock cooling and return normalized state."""
        self._cooling_on = False
        return self.read_state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Return deterministic mock-safe emergency stop controls."""
        _ = reason
        self._heat_level_percent = 0
        self._fan_level_percent = 100
        self._cooling_on = True
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=self._heat_level_percent,
            fan_level_percent=self._fan_level_percent,
            cooling_on=self._cooling_on,
        )


def create_roaster_driver(driver_name: str) -> RoasterDriver:
    """Create the configured roaster driver.

    Args:
        driver_name: Roaster driver name from configuration.

    Returns:
        Driver adapter for the configured driver.

    Raises:
        ValueError: If the driver is not implemented in the current runtime.
    """
    if driver_name == "mock":
        return MockRoasterDriver()
    raise ValueError(
        f"Roaster driver {driver_name!r} is not implemented in this bootstrap runtime."
    )


def create_roaster_safety_driver(driver_name: str) -> RoasterDriver:
    """Create the configured roaster driver for E2 compatibility."""
    return create_roaster_driver(driver_name)


def _validate_control_percent(value: object, *, label: str) -> int:
    """Validate one percentage control input."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer between 0 and 100.")
    if not 0 <= value <= 100:
        raise ValueError(f"{label} must be between 0 and 100.")
    return value
