"""Roaster driver safety behavior coverage."""

from datetime import UTC, datetime

from coffee_roaster_mcp.drivers import MockRoasterDriver, create_roaster_safety_driver
from coffee_roaster_mcp.session import RoastSessionStore


def test_create_roaster_safety_driver_returns_mock_driver() -> None:
    driver = create_roaster_safety_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_mock_driver_emergency_stop_applies_safe_session_state() -> None:
    store = RoastSessionStore(
        utc_now=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        monotonic_now=lambda: 100.0,
    )
    session = store.start_session()
    session.heat_level_percent = 70
    session.fan_level_percent = 20
    session.cooling_on = False
    driver = MockRoasterDriver()

    result = driver.emergency_stop(session, reason="unit-test")

    assert result.driver == "mock"
    assert result.safety_method == "emergency_stop"
    assert session.heat_level_percent == 0
    assert session.fan_level_percent == 100
    assert session.cooling_on is True
    assert result.as_event_payload()["driver_safety_method_called"] is True
