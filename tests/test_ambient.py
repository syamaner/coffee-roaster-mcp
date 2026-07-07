"""Unit coverage for the Yoctopuce ambient sensor reader (#185)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coffee_roaster_mcp import ambient as ambient_module
from coffee_roaster_mcp.ambient import (
    AmbientReaderError,
    YoctoMeteoAmbientReader,
    build_configured_ambient_reader,
)
from coffee_roaster_mcp.config import AmbientConfig


class FakeSensor:
    """Minimal stand-in for a YSensor-derived Yoctopuce function object."""

    CURRENTVALUE_INVALID = -999999.0

    def __init__(self, *, value: float, online: bool = True) -> None:
        self._value = value
        self._online = online

    def isOnline(self) -> bool:  # noqa: N802 - mirrors the Yoctopuce SDK naming.
        return self._online

    def get_currentValue(self) -> float:  # noqa: N802 - mirrors the Yoctopuce SDK naming.
        return self._value


def _fake_sensor_class(
    *,
    first_sensor: FakeSensor | None,
    find_sensor: FakeSensor | None = None,
) -> Any:
    find_calls: list[str] = []

    def first() -> FakeSensor | None:
        return first_sensor

    def find(selector: str) -> FakeSensor | None:
        find_calls.append(selector)
        return find_sensor

    return SimpleNamespace(
        FirstTemperature=first,
        FindTemperature=find,
        FirstHumidity=first,
        FindHumidity=find,
        FirstPressure=first,
        FindPressure=find,
        calls={"find": find_calls},
    )


class FakeYAPI:
    """Fake `yoctopuce.yocto_api.YAPI` sufficient for hub registration tests."""

    def __init__(self, *, register_result: int = 0, register_errmsg: str = "") -> None:
        self.register_result = register_result
        self.register_errmsg = register_errmsg
        self.register_calls: list[str] = []
        self.free_api_calls = 0

    def RegisterHub(self, url: str, errmsg: Any) -> int:  # noqa: N802
        self.register_calls.append(url)
        if self.register_result != 0:
            errmsg.value = self.register_errmsg
        return self.register_result

    def YISERR(self, result: int) -> bool:  # noqa: N802
        return result != 0

    def FreeAPI(self) -> None:  # noqa: N802
        self.free_api_calls += 1


class FakeYRefParam:
    def __init__(self) -> None:
        self.value = ""


def _fake_api_module(**kwargs: Any) -> Any:
    return SimpleNamespace(YAPI=FakeYAPI(**kwargs), YRefParam=FakeYRefParam)


def _reader(
    *,
    config: AmbientConfig | None = None,
    api_module: Any | None = None,
    temperature: FakeSensor | None = None,
    humidity: FakeSensor | None = None,
    pressure: FakeSensor | None = None,
) -> YoctoMeteoAmbientReader:
    return YoctoMeteoAmbientReader(
        config or AmbientConfig(mode="yoctopuce"),
        yoctopuce_api_module=api_module or _fake_api_module(),
        yoctopuce_temperature_module=SimpleNamespace(
            YTemperature=_fake_sensor_class(first_sensor=temperature)
        ),
        yoctopuce_humidity_module=SimpleNamespace(
            YHumidity=_fake_sensor_class(first_sensor=humidity)
        ),
        yoctopuce_pressure_module=SimpleNamespace(
            YPressure=_fake_sensor_class(first_sensor=pressure)
        ),
    )


def test_reader_reads_temperature_humidity_pressure() -> None:
    reader = _reader(
        temperature=FakeSensor(value=21.5),
        humidity=FakeSensor(value=45.0),
        pressure=FakeSensor(value=1013.25),
    )

    reading = reader.read()

    assert reading.temperature_c == 21.5
    assert reading.humidity_percent == 45.0
    assert reading.pressure_hpa == 1013.25
    assert reading.monotonic_seconds > 0


def test_reader_registers_hub_once_across_multiple_reads() -> None:
    api_module = _fake_api_module()
    reader = _reader(
        api_module=api_module,
        temperature=FakeSensor(value=20.0),
        humidity=FakeSensor(value=40.0),
        pressure=FakeSensor(value=1000.0),
    )

    reader.read()
    reader.read()

    assert api_module.YAPI.register_calls == ["usb"]


def test_reader_uses_configured_device_selector() -> None:
    config = AmbientConfig(mode="yoctopuce", device="METEOMK2-12345")
    temperature_module = SimpleNamespace(
        YTemperature=_fake_sensor_class(
            first_sensor=None,
            find_sensor=FakeSensor(value=22.0),
        )
    )
    humidity_module = SimpleNamespace(
        YHumidity=_fake_sensor_class(first_sensor=None, find_sensor=FakeSensor(value=41.0))
    )
    pressure_module = SimpleNamespace(
        YPressure=_fake_sensor_class(first_sensor=None, find_sensor=FakeSensor(value=1001.0))
    )
    reader = YoctoMeteoAmbientReader(
        config,
        yoctopuce_api_module=_fake_api_module(),
        yoctopuce_temperature_module=temperature_module,
        yoctopuce_humidity_module=humidity_module,
        yoctopuce_pressure_module=pressure_module,
    )

    reading = reader.read()

    assert reading.temperature_c == 22.0
    assert temperature_module.YTemperature.calls["find"] == ["METEOMK2-12345.temperature"]
    assert humidity_module.YHumidity.calls["find"] == ["METEOMK2-12345.humidity"]
    assert pressure_module.YPressure.calls["find"] == ["METEOMK2-12345.pressure"]


def test_reader_fails_soft_when_hub_registration_fails() -> None:
    reader = _reader(
        api_module=_fake_api_module(register_result=1, register_errmsg="no hub available"),
    )

    with pytest.raises(AmbientReaderError, match="no hub available"):
        reader.read()


def test_reader_fails_soft_when_hub_registration_raises() -> None:
    class RaisingYAPI(FakeYAPI):
        def RegisterHub(self, url: str, errmsg: Any) -> int:  # noqa: N802
            raise RuntimeError("USB backend unavailable")

    reader = _reader(
        api_module=SimpleNamespace(YAPI=RaisingYAPI(), YRefParam=FakeYRefParam),
    )

    with pytest.raises(AmbientReaderError, match="Could not register the Yoctopuce USB hub"):
        reader.read()


def test_reader_fails_soft_when_no_sensor_present() -> None:
    reader = _reader(temperature=None, humidity=None, pressure=None)

    with pytest.raises(AmbientReaderError, match="No online Yocto-Meteo temperature sensor"):
        reader.read()


def test_reader_fails_soft_when_sensor_is_offline() -> None:
    reader = _reader(
        temperature=FakeSensor(value=20.0, online=False),
        humidity=FakeSensor(value=40.0),
        pressure=FakeSensor(value=1000.0),
    )

    with pytest.raises(AmbientReaderError, match="No online Yocto-Meteo temperature sensor"):
        reader.read()


def test_reader_fails_soft_on_invalid_current_value() -> None:
    invalid_sensor = FakeSensor(value=FakeSensor.CURRENTVALUE_INVALID)
    reader = _reader(
        temperature=invalid_sensor,
        humidity=FakeSensor(value=40.0),
        pressure=FakeSensor(value=1000.0),
    )

    with pytest.raises(AmbientReaderError, match="temperature reading is invalid"):
        reader.read()


def test_reader_wraps_unexpected_backend_exception() -> None:
    class ExplodingSensorClass:
        @staticmethod
        def FirstTemperature() -> FakeSensor:  # noqa: N802
            raise RuntimeError("native backend exploded")

    reader = YoctoMeteoAmbientReader(
        AmbientConfig(mode="yoctopuce"),
        yoctopuce_api_module=_fake_api_module(),
        yoctopuce_temperature_module=SimpleNamespace(YTemperature=ExplodingSensorClass),
        yoctopuce_humidity_module=SimpleNamespace(
            YHumidity=_fake_sensor_class(first_sensor=FakeSensor(value=40.0))
        ),
        yoctopuce_pressure_module=SimpleNamespace(
            YPressure=_fake_sensor_class(first_sensor=FakeSensor(value=1000.0))
        ),
    )

    with pytest.raises(AmbientReaderError, match="Could not read the Yocto-Meteo probe"):
        reader.read()


def test_reader_close_frees_api_and_is_idempotent() -> None:
    api_module = _fake_api_module()
    reader = _reader(
        api_module=api_module,
        temperature=FakeSensor(value=20.0),
        humidity=FakeSensor(value=40.0),
        pressure=FakeSensor(value=1000.0),
    )
    reader.read()

    reader.close()
    reader.close()

    assert api_module.YAPI.free_api_calls == 1


def test_close_before_any_read_is_a_no_op() -> None:
    reader = _reader()

    reader.close()  # must not raise even though the hub was never registered


def test_build_configured_ambient_reader_returns_yocto_meteo_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_load_yoctopuce(module_name: str) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(ambient_module, "_load_yoctopuce", _fake_load_yoctopuce)

    reader = build_configured_ambient_reader(AmbientConfig(mode="yoctopuce"))

    assert isinstance(reader, YoctoMeteoAmbientReader)


def test_build_configured_ambient_reader_rejects_disabled_mode() -> None:
    with pytest.raises(AmbientReaderError, match="Unsupported ambient sensor mode"):
        build_configured_ambient_reader(AmbientConfig(mode="disabled"))


def test_missing_yoctopuce_runtime_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors `test_microphone_audio_input_normalizes_missing_portaudio_runtime`."""

    def fail_import(module_name: str) -> object:
        assert module_name == "yoctopuce.yocto_api"
        raise OSError("Yoctopuce native runtime not found")

    monkeypatch.setattr(ambient_module.importlib, "import_module", fail_import)

    with pytest.raises(AmbientReaderError, match="yoctopuce package and its native runtime"):
        YoctoMeteoAmbientReader(AmbientConfig(mode="yoctopuce"))
