"""Roaster driver safety behavior coverage."""

from coffee_roaster_mcp.drivers import MockRoasterDriver, create_roaster_safety_driver


def test_create_roaster_safety_driver_returns_mock_driver() -> None:
    driver = create_roaster_safety_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_mock_driver_emergency_stop_returns_safe_session_state() -> None:
    driver = MockRoasterDriver()

    result = driver.emergency_stop(reason="unit-test")

    assert result.driver == "mock"
    assert result.safety_method == "emergency_stop"
    assert result.heat_level_percent == 0
    assert result.fan_level_percent == 100
    assert result.cooling_on is True
    assert result.as_event_payload()["driver_safety_method_called"] is True
