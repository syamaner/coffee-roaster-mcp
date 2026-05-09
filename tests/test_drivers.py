"""Roaster driver safety behavior coverage."""

import pytest

from coffee_roaster_mcp.drivers import (
    MockRoasterDriver,
    RoasterDriver,
    create_roaster_driver,
    create_roaster_safety_driver,
)


def _assert_roaster_driver_contract(driver: RoasterDriver) -> None:
    """Exercise the E3 roaster driver contract against one implementation."""
    capabilities = driver.capabilities
    assert capabilities.driver == "mock"
    assert capabilities.heat.minimum == 0
    assert capabilities.heat.maximum == 100
    assert capabilities.heat.step == 1
    assert capabilities.fan.minimum == 0
    assert capabilities.fan.maximum == 100
    assert capabilities.actions.heat_control is True
    assert capabilities.actions.fan_control is True
    assert capabilities.actions.bean_drop is True
    assert capabilities.actions.cooling_control is True
    assert capabilities.actions.emergency_stop is True
    assert capabilities.sensor_units.bean_temperature == "celsius"
    assert capabilities.sensor_units.environment_temperature == "celsius"
    assert capabilities.command_streaming.required is False
    assert capabilities.command_streaming.interval_seconds is None

    initial_state = driver.read_state()
    assert initial_state.driver == "mock"
    assert initial_state.connected is False
    assert initial_state.heat_level_percent == 0
    assert initial_state.fan_level_percent == 0
    assert initial_state.cooling_on is False

    driver.connect()
    connected_state = driver.read_state()
    assert connected_state.connected is True

    heat_state = driver.set_heat(heat_level_percent=55)
    assert heat_state.heat_level_percent == 55
    assert heat_state.fan_level_percent == 0

    fan_state = driver.set_fan(fan_level_percent=35)
    assert fan_state.heat_level_percent == 55
    assert fan_state.fan_level_percent == 35

    dropped_state = driver.drop_beans()
    assert dropped_state.heat_level_percent == 0
    assert dropped_state.raw_vendor_data["beans_dropped"] is True

    cooling_state = driver.start_cooling()
    assert cooling_state.cooling_on is True

    stopped_cooling_state = driver.stop_cooling()
    assert stopped_cooling_state.cooling_on is False

    driver.disconnect()
    disconnected_state = driver.read_state()
    assert disconnected_state.connected is False


def test_create_roaster_driver_returns_mock_driver() -> None:
    driver = create_roaster_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_create_roaster_safety_driver_alias_returns_mock_driver() -> None:
    driver = create_roaster_safety_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_create_roaster_driver_rejects_unsupported_driver() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        create_roaster_driver("hottop")


def test_mock_driver_satisfies_roaster_driver_contract() -> None:
    _assert_roaster_driver_contract(MockRoasterDriver())


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("set_heat", {"heat_level_percent": -1}),
        ("set_heat", {"heat_level_percent": 101}),
        ("set_fan", {"fan_level_percent": -1}),
        ("set_fan", {"fan_level_percent": 101}),
    ],
)
def test_mock_driver_rejects_out_of_range_controls(
    method_name: str,
    kwargs: dict[str, int],
) -> None:
    driver = MockRoasterDriver()

    with pytest.raises(ValueError, match="between 0 and 100"):
        getattr(driver, method_name)(**kwargs)


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("set_heat", {"heat_level_percent": True}),
        ("set_fan", {"fan_level_percent": True}),
    ],
)
def test_mock_driver_rejects_bool_controls(
    method_name: str,
    kwargs: dict[str, bool],
) -> None:
    driver = MockRoasterDriver()

    with pytest.raises(TypeError, match="integer between 0 and 100"):
        getattr(driver, method_name)(**kwargs)


def test_mock_driver_emergency_stop_returns_safe_session_state() -> None:
    driver = MockRoasterDriver()
    driver.set_heat(heat_level_percent=75)
    driver.set_fan(fan_level_percent=20)

    result = driver.emergency_stop(reason="unit-test")

    assert result.driver == "mock"
    assert result.safety_method == "emergency_stop"
    assert result.heat_level_percent == 0
    assert result.fan_level_percent == 100
    assert result.cooling_on is True
    assert result.as_event_payload()["driver_safety_method_called"] is True
    assert driver.read_state().heat_level_percent == 0
    assert driver.read_state().fan_level_percent == 100
    assert driver.read_state().cooling_on is True
