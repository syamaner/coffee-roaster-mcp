"""Atomic exact-normal-drop behavior at the mock-driver boundary."""

from collections.abc import Callable
from threading import Event, Thread

import pytest

from coffee_roaster_mcp.drivers import (
    ExactDropArbitration,
    ExactDropCancelled,
    HottopRoasterDriver,
    MockRoasterDriver,
    RoasterState,
)


def _arbitration(
    driver: MockRoasterDriver,
    *,
    cancelled: Event | None = None,
    expected_drop_transition_count: int | None = None,
    expected_emergency_count: int | None = None,
    before_transition: Callable[[], None] | None = None,
) -> ExactDropArbitration:
    """Build one per-call exact-drop arbitration token from current counts."""
    return ExactDropArbitration(
        operation_id="operation-1",
        session_id="session-1",
        cancelled=cancelled if cancelled is not None else Event(),
        expected_drop_transition_count=(
            driver.drop_transition_count
            if expected_drop_transition_count is None
            else expected_drop_transition_count
        ),
        expected_emergency_count=(
            driver.emergency_count if expected_emergency_count is None else expected_emergency_count
        ),
        before_transition=before_transition,
    )


def _command_projection(driver: MockRoasterDriver) -> tuple[object, ...]:
    """Return the non-advancing fields a refused exact drop must preserve."""
    state = driver._state_snapshot()  # pyright: ignore[reportPrivateUsage]
    evidence = driver.read_lifecycle_evidence()
    return (
        state.connected,
        state.heat_level_percent,
        state.fan_level_percent,
        state.cooling_on,
        state.raw_vendor_data["beans_dropped"],
        state.raw_vendor_data["sample_index"],
        evidence.drum_motor_on,
        evidence.solenoid_open,
    )


class _InterruptedCommandDriver(MockRoasterDriver):
    """Mock driver that deterministically raises during one command-field write."""

    _interrupted_field: str
    _interrupt_writes: bool

    def __init__(self, interrupted_field: str) -> None:
        object.__setattr__(self, "_interrupted_field", interrupted_field)
        object.__setattr__(self, "_interrupt_writes", False)
        super().__init__()

    def interrupt_writes(self) -> None:
        """Cause the next matching command-field write to raise."""
        self._interrupt_writes = True

    def __setattr__(self, name: str, value: object) -> None:
        """Raise only after test setup reaches the selected command field."""
        if name == self._interrupted_field and self._interrupt_writes:
            raise RuntimeError("injected command write failure")
        super().__setattr__(name, value)


def test_normal_drop_capability_is_declared_only_by_mock_and_legacy_is_counted() -> None:
    """The mock alone declares v1 and legacy drop remains a counted transition."""
    driver = MockRoasterDriver()
    sampled = driver.read_state()

    state = driver.drop_beans()

    assert driver.capabilities.normal_drop_version == 1
    assert HottopRoasterDriver().capabilities.normal_drop_version is None
    assert driver.drop_transition_count == 1
    assert driver.emergency_count == 0
    assert state.heat_level_percent == 0
    assert state.fan_level_percent == 100
    assert state.cooling_on is True
    assert state.raw_vendor_data["beans_dropped"] is True
    assert state.raw_vendor_data["sample_index"] == sampled.raw_vendor_data["sample_index"]


def test_transition_count_properties_do_not_advance_mock_telemetry() -> None:
    """Counter reads remain command-history observations rather than telemetry reads."""
    driver = MockRoasterDriver()
    before = driver.set_fan(fan_level_percent=0)

    assert driver.drop_transition_count == 0
    assert driver.emergency_count == 0

    after = driver.set_heat(heat_level_percent=0)
    assert before.raw_vendor_data["sample_index"] == after.raw_vendor_data["sample_index"] == 0


def test_cancelled_exact_drop_preserves_the_entire_command_projection() -> None:
    """A pre-set cancellation token refuses before any command-state write."""
    driver = MockRoasterDriver()
    driver.set_heat(heat_level_percent=57)
    driver.set_fan(fan_level_percent=31)
    cancelled = Event()
    cancelled.set()
    before = _command_projection(driver)

    with pytest.raises(ExactDropCancelled) as raised:
        driver.drop_beans(exact_drop=_arbitration(driver, cancelled=cancelled))

    assert raised.value.discriminator == "emergency_cancelled"
    assert driver.drop_transition_count == 0
    assert driver.emergency_count == 0
    assert _command_projection(driver) == before


def test_hook_runs_once_without_the_command_lock_and_observes_emergency_count_change() -> None:
    """An emergency from the hook completes before exact-drop arbitration begins."""
    driver = MockRoasterDriver()
    hook_calls: list[None] = []

    def stop_before_transition() -> None:
        hook_calls.append(None)
        driver.emergency_stop(reason="hook emergency")

    with pytest.raises(ExactDropCancelled) as raised:
        driver.drop_beans(exact_drop=_arbitration(driver, before_transition=stop_before_transition))

    assert raised.value.discriminator == "emergency_count_changed"
    assert hook_calls == [None]
    assert driver.drop_transition_count == 0
    assert driver.emergency_count == 1
    evidence = driver.read_lifecycle_evidence()
    assert evidence.solenoid_open is False
    assert evidence.drum_motor_on is False


def test_stale_transition_count_refuses_without_clearing_prior_drop_evidence() -> None:
    """A competing drop is visible through its count and preserves beans_dropped."""
    driver = MockRoasterDriver()
    driver.drop_beans()
    before = _command_projection(driver)

    with pytest.raises(ExactDropCancelled) as raised:
        driver.drop_beans(exact_drop=_arbitration(driver, expected_drop_transition_count=0))

    assert raised.value.discriminator == "transition_count_changed"
    assert driver.drop_transition_count == 1
    assert driver.emergency_count == 0
    assert _command_projection(driver) == before


def test_background_emergency_before_transition_refuses_exact_drop_without_dispatch() -> None:
    """An emergency ordered between the hook and atomic region closes the chute."""
    driver = MockRoasterDriver()
    hook_entered = Event()
    release_hook = Event()
    completed = Event()
    errors: list[BaseException] = []

    def wait_before_transition() -> None:
        hook_entered.set()
        assert release_hook.wait(timeout=1.0)

    def attempt_drop() -> None:
        try:
            driver.drop_beans(
                exact_drop=_arbitration(driver, before_transition=wait_before_transition)
            )
        except BaseException as exc:  # noqa: BLE001 - surface worker failure for the assertion.
            errors.append(exc)
        finally:
            completed.set()

    worker = Thread(target=attempt_drop)
    worker.start()
    try:
        assert hook_entered.wait(timeout=1.0)
        driver.emergency_stop(reason="background emergency")
        release_hook.set()
        assert completed.wait(timeout=1.0)
    finally:
        release_hook.set()
        worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ExactDropCancelled)
    assert errors[0].discriminator == "emergency_count_changed"
    assert driver.drop_transition_count == 0
    assert driver.emergency_count == 1
    evidence = driver.read_lifecycle_evidence()
    assert evidence.solenoid_open is False
    assert evidence.drum_motor_on is False


def test_drop_response_keeps_captured_command_state_when_later_emergency_stops_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response combines post-transition sample data with captured Drop fields."""
    driver = MockRoasterDriver()
    hook_called = Event()
    response_assembly_entered = Event()
    release_response_assembly = Event()
    completed = Event()
    results: list[RoasterState] = []
    errors: list[BaseException] = []
    original_assembly = driver._state_snapshot_from_command  # pyright: ignore[reportPrivateUsage]

    def publish_completed_sample() -> None:
        hook_called.set()
        driver._advance_telemetry()  # pyright: ignore[reportPrivateUsage]

    def pause_response_assembly(*, thermal_sample: object, command_state: object) -> RoasterState:
        response_assembly_entered.set()
        assert release_response_assembly.wait(timeout=1.0)
        return original_assembly(
            thermal_sample=thermal_sample,  # type: ignore[arg-type]
            command_state=command_state,  # type: ignore[arg-type]
        )

    def attempt_drop() -> None:
        try:
            results.append(
                driver.drop_beans(
                    exact_drop=_arbitration(driver, before_transition=publish_completed_sample)
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surface worker failure for the assertion.
            errors.append(exc)
        finally:
            completed.set()

    monkeypatch.setattr(driver, "_state_snapshot_from_command", pause_response_assembly)
    worker = Thread(target=attempt_drop)
    worker.start()
    try:
        assert hook_called.wait(timeout=1.0)
        assert response_assembly_entered.wait(timeout=1.0)
        driver.emergency_stop(reason="after drop command capture")
        release_response_assembly.set()
        assert completed.wait(timeout=1.0)
    finally:
        release_response_assembly.set()
        worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert not errors
    assert len(results) == 1
    response = results[0]
    assert response.heat_level_percent == 0
    assert response.fan_level_percent == 100
    assert response.cooling_on is True
    assert response.raw_vendor_data["beans_dropped"] is True
    assert response.raw_vendor_data["sample_index"] == 1
    evidence = driver.read_lifecycle_evidence()
    assert evidence.drum_motor_on is False
    assert evidence.solenoid_open is False
    assert driver.drop_transition_count == 1
    assert driver.emergency_count == 1


def test_counts_are_instance_scoped_and_another_instance_cannot_cancel_drop() -> None:
    """No driver registration or process-wide cancellation state affects peers."""
    first = MockRoasterDriver()
    second = MockRoasterDriver()

    first.emergency_stop(reason="first only")
    second_state = second.drop_beans(exact_drop=_arbitration(second))

    assert first.drop_transition_count == 0
    assert first.emergency_count == 1
    assert second.drop_transition_count == 1
    assert second.emergency_count == 0
    assert second_state.raw_vendor_data["beans_dropped"] is True


def test_drop_counter_is_advanced_before_an_interrupted_exact_transition() -> None:
    """A partial exact Drop write retains transition evidence rather than zero."""
    driver = _InterruptedCommandDriver("_fan_level_percent")
    driver.set_heat(heat_level_percent=44)
    driver.interrupt_writes()

    with pytest.raises(RuntimeError, match="injected command write failure"):
        driver.drop_beans(exact_drop=_arbitration(driver))

    assert driver.drop_transition_count == 1
    assert driver.emergency_count == 0
    state = driver._state_snapshot()  # pyright: ignore[reportPrivateUsage]
    assert state.raw_vendor_data["beans_dropped"] is True
    assert state.heat_level_percent == 0
    assert state.fan_level_percent == 0
    evidence = driver.read_lifecycle_evidence()
    assert evidence.solenoid_open is False


def test_emergency_counter_is_advanced_before_an_interrupted_emergency_stop() -> None:
    """An interrupted emergency remains visible to later exact-drop arbitration."""
    driver = _InterruptedCommandDriver("_fan_level_percent")
    driver.set_heat(heat_level_percent=44)
    driver.interrupt_writes()

    with pytest.raises(RuntimeError, match="injected command write failure"):
        driver.emergency_stop(reason="injected failure")

    assert driver.drop_transition_count == 0
    assert driver.emergency_count == 1
    with pytest.raises(ExactDropCancelled) as raised:
        driver.drop_beans(exact_drop=_arbitration(driver, expected_emergency_count=0))
    assert raised.value.discriminator == "emergency_count_changed"
