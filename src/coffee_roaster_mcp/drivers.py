"""Roaster driver contract and mock-safe implementation for RoastPilot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from math import isfinite
from threading import Event, Lock, Thread
from typing import Literal, Protocol, cast

from coffee_roaster_mcp.controls import validate_control_percent
from coffee_roaster_mcp.session import EventPayloadValue

ReportedTemperatureUnit = Literal["celsius", "unknown"]
HOTTOP_DRIVER_NAME = "hottop_kn8828b_2k_plus"


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

    def __post_init__(self) -> None:
        """Validate streaming settings are internally consistent."""
        if self.required and self.interval_seconds is None:
            raise ValueError("interval_seconds is required when command streaming is required.")
        if not self.required and self.interval_seconds is not None:
            raise ValueError(
                "interval_seconds must be None when command streaming is not required."
            )
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0.")


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

    def __post_init__(self) -> None:
        """Validate normalized roaster state at the driver boundary."""
        _validate_driver_name(self.driver)
        _validate_exact_bool(self.connected, label="connected")
        _validate_exact_bool(self.cooling_on, label="cooling_on")
        object.__setattr__(
            self,
            "bean_temp_c",
            _validate_optional_temperature_c(self.bean_temp_c, label="bean_temp_c"),
        )
        object.__setattr__(
            self,
            "env_temp_c",
            _validate_optional_temperature_c(self.env_temp_c, label="env_temp_c"),
        )
        object.__setattr__(
            self,
            "heat_level_percent",
            validate_control_percent(
                self.heat_level_percent,
                label="heat_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "fan_level_percent",
            validate_control_percent(
                self.fan_level_percent,
                label="fan_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "raw_vendor_data",
            _validate_raw_vendor_data(self.raw_vendor_data),
        )


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


class SerialTransport(Protocol):
    """Minimal serial transport surface used by the Hottop lifecycle."""

    @property
    def is_open(self) -> bool:
        """Return whether the serial transport is open."""
        ...

    def close(self) -> None:
        """Close the serial transport."""
        ...


SerialTransportFactory = Callable[..., SerialTransport]


class MockRoasterDriver:
    """Mock roaster driver with deterministic local-only telemetry and controls."""

    name = "mock"
    _SAMPLE_INTERVAL_SECONDS = 1.0
    _INITIAL_TEMP_C = 20.0
    _MIN_TEMP_C = 15.0
    _MAX_ENV_TEMP_C = 300.0
    _MAX_BEAN_TEMP_C = 250.0
    _MAX_HEAT_RATE_C_PER_SEC = 2.0
    _MAX_FAN_COOLING_C_PER_SEC = 0.5
    _COOLING_MODE_RATE_C_PER_SEC = 5.0
    _BEAN_THERMAL_LAG_FACTOR = 0.1

    def __init__(self) -> None:
        """Initialize deterministic mock roaster state."""
        self._connected = False
        self._bean_temp_c = self._INITIAL_TEMP_C
        self._env_temp_c = self._INITIAL_TEMP_C
        self._heat_level_percent = 0
        self._fan_level_percent = 0
        self._cooling_on = False
        self._beans_dropped = False
        self._sample_index = 0

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
        self._advance_telemetry()
        return self._state_snapshot()

    def _state_snapshot(self) -> RoasterState:
        """Return the current mock roaster state without advancing telemetry."""
        return RoasterState(
            driver=self.name,
            connected=self._connected,
            bean_temp_c=self._bean_temp_c,
            env_temp_c=self._env_temp_c,
            heat_level_percent=self._heat_level_percent,
            fan_level_percent=self._fan_level_percent,
            cooling_on=self._cooling_on,
            raw_vendor_data={
                "beans_dropped": self._beans_dropped,
                "sample_index": self._sample_index,
                "telemetry_model": "fixed_step_thermal_v1",
            },
        )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Set mock heat and return normalized state."""
        self._heat_level_percent = validate_control_percent(
            heat_level_percent,
            label="heat_level_percent",
        )
        return self._state_snapshot()

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Set mock fan and return normalized state."""
        self._fan_level_percent = validate_control_percent(
            fan_level_percent,
            label="fan_level_percent",
        )
        return self._state_snapshot()

    def drop_beans(self) -> RoasterState:
        """Record a mock bean drop and force heat off."""
        self._beans_dropped = True
        self._heat_level_percent = 0
        return self._state_snapshot()

    def start_cooling(self) -> RoasterState:
        """Start mock cooling and return normalized state."""
        self._cooling_on = True
        return self._state_snapshot()

    def stop_cooling(self) -> RoasterState:
        """Stop mock cooling and return normalized state."""
        self._cooling_on = False
        return self._state_snapshot()

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

    def _advance_telemetry(self) -> None:
        """Advance one deterministic mock telemetry sample."""
        self._sample_index += 1
        env_delta_c = self._environment_delta_c()
        self._env_temp_c = _clamp_float(
            self._env_temp_c + env_delta_c,
            minimum=self._MIN_TEMP_C,
            maximum=self._MAX_ENV_TEMP_C,
        )

        bean_target_c = self._env_temp_c
        bean_delta_c = (
            (bean_target_c - self._bean_temp_c)
            * self._BEAN_THERMAL_LAG_FACTOR
            * self._SAMPLE_INTERVAL_SECONDS
        )
        self._bean_temp_c = _clamp_float(
            self._bean_temp_c + bean_delta_c,
            minimum=self._MIN_TEMP_C,
            maximum=self._MAX_BEAN_TEMP_C,
        )

    def _environment_delta_c(self) -> float:
        """Return one fixed-step environment temperature delta."""
        heat_effect = (self._heat_level_percent / 100.0) * self._MAX_HEAT_RATE_C_PER_SEC
        fan_effect = (self._fan_level_percent / 100.0) * self._MAX_FAN_COOLING_C_PER_SEC
        cooling_effect = self._COOLING_MODE_RATE_C_PER_SEC if self._cooling_on else 0.0
        return (heat_effect - fan_effect - cooling_effect) * self._SAMPLE_INTERVAL_SECONDS


class HottopRoasterDriver:
    """Hottop KN-8828B-2K+ driver lifecycle skeleton.

    This story intentionally implements only connection ownership and command
    loop cleanup. Packet construction, status parsing, and hardware commands
    land in later Epic 3 stories.
    """

    name = HOTTOP_DRIVER_NAME

    def __init__(
        self,
        *,
        port: str | None = None,
        baudrate: int = 115_200,
        command_interval_seconds: float = 0.3,
        serial_factory: SerialTransportFactory | None = None,
        join_timeout_seconds: float = 1.0,
    ) -> None:
        """Initialize Hottop lifecycle dependencies.

        Args:
            port: Serial port path for the Hottop controller.
            baudrate: Serial baudrate.
            command_interval_seconds: Command-loop cadence.
            serial_factory: Optional injectable serial transport factory for tests.
            join_timeout_seconds: Maximum disconnect wait for command-loop cleanup.
        """
        if command_interval_seconds <= 0:
            raise ValueError("command_interval_seconds must be greater than 0.")
        if join_timeout_seconds <= 0:
            raise ValueError("join_timeout_seconds must be greater than 0.")
        self._port = port or "/dev/tty.usbserial-DN016OJ3"
        self._baudrate = baudrate
        self._command_interval_seconds = command_interval_seconds
        self._serial_factory = serial_factory or _create_pyserial_transport
        self._join_timeout_seconds = join_timeout_seconds
        self._serial: SerialTransport | None = None
        self._connected = False
        self._stop_event = Event()
        self._command_thread: Thread | None = None
        self._state_lock = Lock()
        self._command_loop_iterations = 0

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return static Hottop capabilities for lifecycle validation."""
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
            command_streaming=CommandStreaming(
                required=True,
                interval_seconds=self._command_interval_seconds,
            ),
        )

    def connect(self) -> None:
        """Open serial transport and start the Hottop command-loop lifecycle."""
        with self._state_lock:
            if self._connected:
                return
            self._serial = self._serial_factory(
                self._port,
                baudrate=self._baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.5,
            )
            self._connected = True
            self._stop_event.clear()
            self._command_thread = Thread(
                target=self._command_loop,
                daemon=True,
                name="HottopCommandLoop",
            )
            self._command_thread.start()

    def disconnect(self) -> None:
        """Stop command loop and close serial transport."""
        command_thread: Thread | None
        serial_transport: SerialTransport | None
        with self._state_lock:
            self._connected = False
            self._stop_event.set()
            command_thread = self._command_thread
            serial_transport = self._serial
            self._command_thread = None
            self._serial = None

        if command_thread is not None and command_thread.is_alive():
            command_thread.join(timeout=self._join_timeout_seconds)
        if command_thread is not None and command_thread.is_alive():
            raise RuntimeError("Hottop command loop did not stop during disconnect.")
        if serial_transport is not None and serial_transport.is_open:
            serial_transport.close()

    def read_state(self) -> RoasterState:
        """Return the current normalized Hottop lifecycle state."""
        with self._state_lock:
            command_loop_running = (
                self._command_thread is not None and self._command_thread.is_alive()
            )
            return RoasterState(
                driver=self.name,
                connected=self._connected,
                bean_temp_c=None,
                env_temp_c=None,
                heat_level_percent=0,
                fan_level_percent=0,
                cooling_on=False,
                raw_vendor_data={
                    "port": self._port,
                    "baudrate": self._baudrate,
                    "command_interval_seconds": self._command_interval_seconds,
                    "command_loop_running": command_loop_running,
                    "command_loop_iterations": self._command_loop_iterations,
                },
            )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Reject Hottop heat control until packet commands land."""
        validate_control_percent(heat_level_percent, label="heat_level_percent")
        raise NotImplementedError("Hottop heat control lands in E3-S7.")

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Reject Hottop fan control until packet commands land."""
        validate_control_percent(fan_level_percent, label="fan_level_percent")
        raise NotImplementedError("Hottop fan control lands in E3-S7.")

    def drop_beans(self) -> RoasterState:
        """Reject Hottop bean drop until packet commands land."""
        raise NotImplementedError("Hottop bean drop lands in E3-S7.")

    def start_cooling(self) -> RoasterState:
        """Reject Hottop cooling control until packet commands land."""
        raise NotImplementedError("Hottop cooling control lands in E3-S7.")

    def stop_cooling(self) -> RoasterState:
        """Reject Hottop cooling control until packet commands land."""
        raise NotImplementedError("Hottop cooling control lands in E3-S7.")

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Return safe Hottop emergency-stop payload until packet commands land."""
        _ = reason
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop_not_yet_hardware_commanded",
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        )

    def _command_loop(self) -> None:
        """Run the lifecycle loop until disconnect requests shutdown."""
        while not self._stop_event.wait(self._command_interval_seconds):
            with self._state_lock:
                self._command_loop_iterations += 1


def _clamp_float(value: float, *, minimum: float, maximum: float) -> float:
    """Clamp a float and keep telemetry output stable at one decimal place."""
    return round(max(minimum, min(maximum, value)), 1)


def _validate_exact_bool(value: object, *, label: str) -> None:
    """Validate that a state field is exactly a bool."""
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean.")


def _validate_driver_name(value: object) -> None:
    """Validate the stable driver identifier on normalized state."""
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("driver must be a non-empty string.")


def _validate_optional_temperature_c(value: object, *, label: str) -> float | None:
    """Validate one normalized Celsius temperature value."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite Celsius temperature or None.")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{label} must be finite.")
    return normalized


def _validate_raw_vendor_data(
    raw_vendor_data: object,
) -> dict[str, EventPayloadValue]:
    """Validate and copy raw vendor diagnostics for normalized state."""
    if not isinstance(raw_vendor_data, dict):
        raise TypeError("raw_vendor_data must be a dictionary.")
    raw_vendor_mapping = cast(dict[object, object], raw_vendor_data)
    copied: dict[str, EventPayloadValue] = {}
    allowed_value_types = (str, int, float, bool, type(None))
    for key, value in raw_vendor_mapping.items():
        if not isinstance(key, str):
            raise TypeError("raw_vendor_data keys must be strings.")
        if not isinstance(value, allowed_value_types):
            raise TypeError(
                "raw_vendor_data values must be strings, integers, floats, booleans, or None."
            )
        copied[key] = value
    return copied


def _create_pyserial_transport(*args: object, **kwargs: object) -> SerialTransport:
    """Create a pyserial transport lazily for the Hottop driver."""
    serial_module = import_module("serial")
    serial_class = serial_module.Serial
    transport = serial_class(*args, **kwargs)
    return cast(SerialTransport, transport)


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
    if driver_name == HOTTOP_DRIVER_NAME:
        return HottopRoasterDriver()
    raise ValueError(
        f"Roaster driver {driver_name!r} is not implemented in this bootstrap runtime."
    )


def create_roaster_safety_driver(driver_name: str) -> RoasterDriver:
    """Create the configured roaster driver for E2 compatibility."""
    return create_roaster_driver(driver_name)
