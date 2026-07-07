"""Ambient environmental sensor reading (#185).

Mirrors the first-crack microphone pipeline exactly: an MCP-owned USB device
(Yoctopuce Yocto-Meteo-V2-C) read for temperature, relative humidity, and
barometric pressure. Read-only, corpus-metadata for the roast record — no
roaster-write or control-loop involvement. Fail-soft by design: an absent,
unplugged, or erroring probe never blocks or faults a roast.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

from coffee_roaster_mcp.config import AmbientConfig

DEFAULT_AMBIENT_HUB_URL = "usb"


class AmbientReaderError(RuntimeError):
    """Raised when an ambient sensor reading cannot be obtained."""


@dataclass(frozen=True)
class AmbientReading:
    """One ambient environmental reading.

    Attributes:
        temperature_c: Ambient (room) temperature in Celsius.
        humidity_percent: Relative humidity as a percentage (0-100).
        pressure_hpa: Barometric pressure in hectopascals.
        monotonic_seconds: Monotonic clock timestamp when the reading was taken.
    """

    temperature_c: float
    humidity_percent: float
    pressure_hpa: float
    monotonic_seconds: float


class AmbientReader(Protocol):
    """Readable ambient sensor source."""

    def read(self) -> AmbientReading:
        """Return one fresh ambient reading, or raise `AmbientReaderError`."""
        ...


class AmbientReaderFactory(Protocol):
    """Factory for configured ambient readers."""

    def __call__(self, config: AmbientConfig) -> AmbientReader:
        """Create an ambient reader for the supplied configuration."""
        ...


class YoctoMeteoAmbientReader:
    """Read temperature/humidity/pressure from a Yoctopuce Yocto-Meteo-V2-C.

    Lifecycle: `YAPI.RegisterHub("usb")` is called once, lazily, on the first
    read and left registered for the life of this reader instance (mirroring
    how `MicrophoneAudioInput` opens its stream lazily and keeps it open across
    reads). Registering per read would repeatedly re-enumerate the USB bus for
    no benefit, since only one process may hold direct USB access to Yoctopuce
    devices at a time; a single long-lived registration is the simpler correct
    choice and matches the poll-interval-bounded read cadence. `close()` calls
    `YAPI.FreeAPI()` to release the hub registration cleanly.
    """

    def __init__(
        self,
        config: AmbientConfig,
        *,
        yoctopuce_api_module: Any | None = None,
        yoctopuce_temperature_module: Any | None = None,
        yoctopuce_humidity_module: Any | None = None,
        yoctopuce_pressure_module: Any | None = None,
    ) -> None:
        """Configure a Yocto-Meteo reader that registers its USB hub lazily.

        Args:
            config: Validated ambient sensor configuration.
            yoctopuce_api_module: Optional injected `yoctopuce.yocto_api`-compatible
                module for tests.
            yoctopuce_temperature_module: Optional injected
                `yoctopuce.yocto_temperature`-compatible module for tests.
            yoctopuce_humidity_module: Optional injected
                `yoctopuce.yocto_humidity`-compatible module for tests.
            yoctopuce_pressure_module: Optional injected
                `yoctopuce.yocto_pressure`-compatible module for tests.
        """
        self._config = config
        self._yocto_api = yoctopuce_api_module or _load_yoctopuce("yocto_api")
        self._yocto_temperature = yoctopuce_temperature_module or _load_yoctopuce(
            "yocto_temperature"
        )
        self._yocto_humidity = yoctopuce_humidity_module or _load_yoctopuce("yocto_humidity")
        self._yocto_pressure = yoctopuce_pressure_module or _load_yoctopuce("yocto_pressure")
        self._hub_registered = False

    def _ensure_hub_registered(self) -> None:
        if self._hub_registered:
            return
        yapi = self._yocto_api.YAPI
        errmsg = self._yocto_api.YRefParam()
        try:
            result = yapi.RegisterHub(DEFAULT_AMBIENT_HUB_URL, errmsg)
        except Exception as exc:  # noqa: BLE001 - native backend exceptions vary.
            raise AmbientReaderError(f"Could not register the Yoctopuce USB hub: {exc}") from exc
        if yapi.YISERR(result):
            raise AmbientReaderError(f"Could not register the Yoctopuce USB hub: {errmsg.value}")
        self._hub_registered = True

    def _function_selector(self, sensor_suffix: str) -> str | None:
        """Return an explicit `FindXxx` selector, or `None` to use the first device."""
        if self._config.device is None:
            return None
        return f"{self._config.device}.{sensor_suffix}"

    def read(self) -> AmbientReading:
        """Read one fresh temperature/humidity/pressure reading from the probe."""
        try:
            self._ensure_hub_registered()
            temperature_sensor = self._resolve_sensor(
                self._yocto_temperature.YTemperature,
                sensor_suffix="temperature",
            )
            humidity_sensor = self._resolve_sensor(
                self._yocto_humidity.YHumidity,
                sensor_suffix="humidity",
            )
            pressure_sensor = self._resolve_sensor(
                self._yocto_pressure.YPressure,
                sensor_suffix="pressure",
            )
            temperature_c = self._current_value(temperature_sensor, label="temperature")
            humidity_percent = self._current_value(humidity_sensor, label="humidity")
            pressure_hpa = self._current_value(pressure_sensor, label="pressure")
        except AmbientReaderError:
            raise
        except Exception as exc:  # noqa: BLE001 - native backend exceptions vary.
            raise AmbientReaderError(f"Could not read the Yocto-Meteo probe: {exc}") from exc
        return AmbientReading(
            temperature_c=temperature_c,
            humidity_percent=humidity_percent,
            pressure_hpa=pressure_hpa,
            monotonic_seconds=time.monotonic(),
        )

    def _resolve_sensor(self, sensor_class: Any, *, sensor_suffix: str) -> Any:
        selector = self._function_selector(sensor_suffix)
        sensor = (
            self._find_sensor(sensor_class, sensor_suffix, selector)
            if selector is not None
            else self._first_sensor(sensor_class, sensor_suffix)
        )
        if sensor is None or not sensor.isOnline():
            raise AmbientReaderError(f"No online Yocto-Meteo {sensor_suffix} sensor was found.")
        return sensor

    def _find_sensor(self, sensor_class: Any, sensor_suffix: str, selector: str) -> Any:
        if sensor_suffix == "temperature":
            return sensor_class.FindTemperature(selector)
        if sensor_suffix == "humidity":
            return sensor_class.FindHumidity(selector)
        return sensor_class.FindPressure(selector)

    def _first_sensor(self, sensor_class: Any, sensor_suffix: str) -> Any:
        if sensor_suffix == "temperature":
            return sensor_class.FirstTemperature()
        if sensor_suffix == "humidity":
            return sensor_class.FirstHumidity()
        return sensor_class.FirstPressure()

    def _current_value(self, sensor: Any, *, label: str) -> float:
        value = sensor.get_currentValue()
        invalid = getattr(sensor, "CURRENTVALUE_INVALID", None)
        if invalid is not None and value == invalid:
            raise AmbientReaderError(f"Yocto-Meteo {label} reading is invalid.")
        return float(value)

    def close(self) -> None:
        """Release the Yoctopuce USB hub registration, if held.

        Idempotent: safe to call multiple times or on a reader that never
        successfully registered a hub.
        """
        if not self._hub_registered:
            return
        try:
            self._yocto_api.YAPI.FreeAPI()
        finally:
            self._hub_registered = False


def build_configured_ambient_reader(config: AmbientConfig) -> AmbientReader:
    """Create the concrete configured ambient reader.

    Args:
        config: Validated ambient sensor configuration.

    Returns:
        A configured `AmbientReader`.

    Raises:
        AmbientReaderError: If `config.mode` is not a supported reader mode.
    """
    if config.mode == "yoctopuce":
        return YoctoMeteoAmbientReader(config)
    raise AmbientReaderError(f"Unsupported ambient sensor mode: {config.mode}")


def _load_yoctopuce(module_name: str) -> Any:
    try:
        return importlib.import_module(f"yoctopuce.{module_name}")
    except (ImportError, OSError) as exc:
        raise AmbientReaderError(
            "Ambient sensor input requires the yoctopuce package and its native runtime."
        ) from exc
