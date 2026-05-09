"""Roaster driver contract, capability, and safety behavior coverage."""

import pytest

from coffee_roaster_mcp.drivers import (
    CommandStreaming,
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
    assert initial_state.bean_temp_c == 20.0
    assert initial_state.env_temp_c == 20.0
    assert initial_state.heat_level_percent == 0
    assert initial_state.fan_level_percent == 0
    assert initial_state.cooling_on is False
    assert initial_state.raw_vendor_data["sample_index"] == 1

    driver.connect()
    connected_state = driver.read_state()
    assert connected_state.connected is True
    assert connected_state.raw_vendor_data["sample_index"] == 2

    heat_state = driver.set_heat(heat_level_percent=55)
    assert heat_state.heat_level_percent == 55
    assert heat_state.fan_level_percent == 0
    assert heat_state.raw_vendor_data["sample_index"] == 2

    fan_state = driver.set_fan(fan_level_percent=35)
    assert fan_state.heat_level_percent == 55
    assert fan_state.fan_level_percent == 35
    assert fan_state.raw_vendor_data["sample_index"] == 2

    dropped_state = driver.drop_beans()
    assert dropped_state.heat_level_percent == 0
    assert dropped_state.raw_vendor_data["beans_dropped"] is True
    assert dropped_state.raw_vendor_data["sample_index"] == 2

    cooling_state = driver.start_cooling()
    assert cooling_state.cooling_on is True

    stopped_cooling_state = driver.stop_cooling()
    assert stopped_cooling_state.cooling_on is False

    driver.disconnect()
    disconnected_state = driver.read_state()
    assert disconnected_state.connected is False


def _read_temperature_sequence(
    driver: RoasterDriver,
    *,
    sample_count: int,
) -> list[tuple[float | None, float | None]]:
    """Read a deterministic sequence of bean and environment temperatures."""
    sequence: list[tuple[float | None, float | None]] = []
    for _ in range(sample_count):
        state = driver.read_state()
        sequence.append((state.bean_temp_c, state.env_temp_c))
    return sequence


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


def test_mock_driver_returns_reproducible_telemetry_sequence() -> None:
    first_driver = MockRoasterDriver()
    second_driver = MockRoasterDriver()

    for driver in (first_driver, second_driver):
        driver.connect()
        driver.set_heat(heat_level_percent=100)
        driver.set_fan(fan_level_percent=0)

    first_sequence = _read_temperature_sequence(first_driver, sample_count=3)
    second_sequence = _read_temperature_sequence(second_driver, sample_count=3)

    assert first_sequence == second_sequence


def test_mock_driver_telemetry_responds_to_heat_and_cooling() -> None:
    driver = MockRoasterDriver()
    driver.connect()

    initial_state = driver.read_state()
    assert initial_state.bean_temp_c == 20.0
    assert initial_state.env_temp_c == 20.0

    driver.set_heat(heat_level_percent=100)
    heating_state = driver.read_state()
    assert heating_state.env_temp_c == 22.0
    assert heating_state.bean_temp_c == 20.2

    hotter_state = driver.read_state()
    assert hotter_state.env_temp_c == 24.0
    assert hotter_state.bean_temp_c == 20.6

    driver.start_cooling()
    cooling_state = driver.read_state()
    assert cooling_state.cooling_on is True
    assert cooling_state.env_temp_c == 21.0
    assert cooling_state.bean_temp_c == 20.6


def test_mock_driver_control_commands_do_not_advance_telemetry() -> None:
    driver = MockRoasterDriver()
    driver.connect()
    sampled_state = driver.read_state()

    heat_state = driver.set_heat(heat_level_percent=70)
    fan_state = driver.set_fan(fan_level_percent=30)
    drop_state = driver.drop_beans()

    assert sampled_state.raw_vendor_data["sample_index"] == 1
    assert heat_state.raw_vendor_data["sample_index"] == 1
    assert fan_state.raw_vendor_data["sample_index"] == 1
    assert drop_state.raw_vendor_data["sample_index"] == 1
    assert drop_state.bean_temp_c == sampled_state.bean_temp_c
    assert drop_state.env_temp_c == sampled_state.env_temp_c


def test_command_streaming_allows_required_positive_interval() -> None:
    streaming = CommandStreaming(required=True, interval_seconds=0.3)

    assert streaming.required is True
    assert streaming.interval_seconds == 0.3


@pytest.mark.parametrize(
    ("required", "interval_seconds", "match"),
    [
        (True, None, "required when command streaming is required"),
        (True, 0.0, "greater than 0"),
        (True, -0.1, "greater than 0"),
        (False, 0.3, "must be None"),
    ],
)
def test_command_streaming_rejects_inconsistent_values(
    required: bool,
    interval_seconds: float | None,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        CommandStreaming(required=required, interval_seconds=interval_seconds)


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
