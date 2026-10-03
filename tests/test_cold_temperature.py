"""Behavioural coverage for the cold-only temperature projection (#227)."""

from __future__ import annotations

import dataclasses
import json
import math
from typing import cast

import pytest

from coffee_roaster_mcp.cold_temperature import (
    COLD_TEMPERATURE_NOT_ELIGIBLE,
    ColdTemperatureProjection,
    ConfiguredTemperatureUnit,
    project_cold_temperature,
)
from coffee_roaster_mcp.drivers import HOTTOP_DRIVER_NAME, RoasterState
from coffee_roaster_mcp.session import EventPayloadValue

_VALUE_FIELDS = (
    "configured_temperature_unit",
    "reported_temperature_unit",
    "last_packet_valid",
    "last_packet_bean_temp_c",
    "last_packet_env_temp_c",
    "retained_bean_temp_c",
    "retained_env_temp_c",
    "value_agreement",
    "status_packet_count",
    "ignored_temperature_packet_count",
    "status_read_error_count",
    "command_loop_error_count",
)
_MAX_COUNTER = 2**53 - 1


def _vendor(**overrides: object) -> dict[str, object]:
    """Return valid Celsius Hottop vendor data with optional overrides."""
    values: dict[str, object] = {
        "port": "/dev/test-hottop",
        "temperature_unit": "celsius",
        "resolved_temperature_unit": "celsius",
        "raw_bean_temperature": 21,
        "raw_env_temperature": 22,
        "status_packet_count": 3,
        "ignored_temperature_packet_count": 0,
        "status_read_error_count": 1,
        "command_loop_error_count": 2,
    }
    values.update(overrides)
    return values


def _state(
    *,
    driver: str = HOTTOP_DRIVER_NAME,
    bean: float | None = 21.0,
    env: float | None = 22.0,
    vendor: dict[str, object] | None = None,
) -> RoasterState:
    """Build one exact `RoasterState` snapshot without any driver."""
    return RoasterState(
        driver=driver,
        connected=True,
        bean_temp_c=bean,
        env_temp_c=env,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
        raw_vendor_data=cast(dict[str, EventPayloadValue], _vendor() if vendor is None else vendor),
    )


def _with_vendor(state: RoasterState, vendor: object) -> RoasterState:
    """Replace vendor data after validation to reach the pure function directly."""
    object.__setattr__(state, "raw_vendor_data", vendor)
    return state


def _assert_empty(projection: ColdTemperatureProjection, outcome: str) -> None:
    """Assert a closed failure outcome with every value field `None`."""
    assert projection.projection_version == 1
    assert projection.outcome == outcome
    for name in _VALUE_FIELDS:
        assert getattr(projection, name) is None, name


def _assert_malformed(state: object) -> None:
    """Assert the snapshot projects as malformed with no retained values."""
    _assert_empty(project_cold_temperature(state), "malformed")


def test_cold_temperature_celsius_valid_packet_is_observed_and_agrees() -> None:
    """A1: valid Celsius projects raw numerics as-is with agreement and counters."""
    assert project_cold_temperature(_state()) == ColdTemperatureProjection(
        projection_version=1,
        outcome="observed",
        configured_temperature_unit="celsius",
        reported_temperature_unit="celsius",
        last_packet_valid=True,
        last_packet_bean_temp_c=21.0,
        last_packet_env_temp_c=22.0,
        retained_bean_temp_c=21.0,
        retained_env_temp_c=22.0,
        value_agreement="agree",
        status_packet_count=3,
        ignored_temperature_packet_count=0,
        status_read_error_count=1,
        command_loop_error_count=2,
    )


@pytest.mark.parametrize(("bean", "env"), [(20.0, 22.0), (21.0, 23.5)])
def test_cold_temperature_celsius_numeric_mismatch_is_reportable_disagree(
    bean: float, env: float
) -> None:
    """A Celsius raw-versus-retained mismatch is `disagree`, never malformed."""
    projection = project_cold_temperature(_state(bean=bean, env=env))

    assert projection.outcome == "observed"
    assert projection.value_agreement == "disagree"
    assert projection.last_packet_bean_temp_c == 21.0
    assert projection.retained_bean_temp_c == bean


@pytest.mark.parametrize("configured", ["fahrenheit", "auto"])
def test_cold_temperature_fahrenheit_never_exposes_raw_numerics(
    configured: ConfiguredTemperatureUnit,
) -> None:
    """A2/A3/M11: Fahrenheit projects retained Celsius only and never `auto`."""
    vendor = _vendor(
        temperature_unit=configured,
        resolved_temperature_unit="fahrenheit",
        raw_bean_temperature=70,
        raw_env_temperature=72,
    )
    projection = project_cold_temperature(_state(bean=21.1, env=22.2, vendor=vendor))

    assert projection == ColdTemperatureProjection(
        projection_version=1,
        outcome="observed",
        configured_temperature_unit=configured,
        reported_temperature_unit="fahrenheit",
        last_packet_valid=True,
        retained_bean_temp_c=21.1,
        retained_env_temp_c=22.2,
        value_agreement="indeterminate",
        status_packet_count=3,
        ignored_temperature_packet_count=0,
        status_read_error_count=1,
        command_loop_error_count=2,
    )


def test_cold_temperature_auto_resolving_celsius_reports_auto_configuration() -> None:
    """A3: `auto` is reported as configured and never as the reported unit."""
    projection = project_cold_temperature(_state(vendor=_vendor(temperature_unit="auto")))

    assert projection.configured_temperature_unit == "auto"
    assert projection.reported_temperature_unit == "celsius"
    assert projection.value_agreement == "agree"


@pytest.mark.parametrize("configured", ["celsius", "fahrenheit", "auto"])
def test_cold_temperature_ignored_latest_packet_keeps_retained_history(configured: str) -> None:
    """A4/M11: ignored latest packet with prior acceptance reports `unknown`, no numerics."""
    vendor = _vendor(
        temperature_unit=configured,
        resolved_temperature_unit=None,
        raw_bean_temperature=0,
        raw_env_temperature=0,
        ignored_temperature_packet_count=1,
    )
    projection = project_cold_temperature(_state(vendor=vendor))

    assert projection.outcome == "observed"
    assert projection.reported_temperature_unit == "unknown"
    assert projection.last_packet_valid is False
    assert projection.last_packet_bean_temp_c is None
    assert projection.last_packet_env_temp_c is None
    assert (projection.retained_bean_temp_c, projection.retained_env_temp_c) == (21.0, 22.0)
    assert projection.value_agreement == "indeterminate"
    assert (projection.status_packet_count, projection.ignored_temperature_packet_count) == (3, 1)


def test_cold_temperature_all_packets_ignored_has_no_retained_values() -> None:
    """A5: `I == S > 0` is observed with both retained values `None`."""
    vendor = _vendor(
        resolved_temperature_unit=None,
        raw_bean_temperature=0,
        raw_env_temperature=0,
        ignored_temperature_packet_count=3,
    )
    projection = project_cold_temperature(_state(bean=None, env=None, vendor=vendor))

    assert projection.outcome == "observed"
    assert projection.reported_temperature_unit == "unknown"
    assert projection.last_packet_valid is False
    assert projection.retained_bean_temp_c is None
    assert projection.retained_env_temp_c is None
    assert projection.value_agreement == "indeterminate"


def test_cold_temperature_awaiting_first_packet_reports_error_counters() -> None:
    """A6: no counted packet is ordinary startup absence, distinct from malformed."""
    vendor = _vendor(
        resolved_temperature_unit=None,
        raw_bean_temperature=None,
        raw_env_temperature=None,
        status_packet_count=0,
        status_read_error_count=4,
        command_loop_error_count=5,
    )
    projection = project_cold_temperature(_state(bean=None, env=None, vendor=vendor))

    assert projection == ColdTemperatureProjection(
        projection_version=1,
        outcome="awaiting_first_packet",
        configured_temperature_unit="celsius",
        last_packet_valid=False,
        value_agreement="indeterminate",
        status_packet_count=0,
        ignored_temperature_packet_count=0,
        status_read_error_count=4,
        command_loop_error_count=5,
    )


def test_cold_temperature_projection_is_pure_function_of_snapshot() -> None:
    """A8: identical snapshots from unrelated sources yield identical projections."""

    class FirstSource:
        def read_state(self) -> RoasterState:
            return _state()

    class SecondSource:
        def read_state(self) -> RoasterState:
            return _state()

    first = project_cold_temperature(FirstSource().read_state())
    second = project_cold_temperature(SecondSource().read_state())

    assert first == second
    assert project_cold_temperature(_state()) == first


def test_cold_temperature_counter_accepts_json_exact_upper_bound() -> None:
    """A10: the JSON-exactness representation bound itself is accepted."""
    vendor = _vendor(status_packet_count=_MAX_COUNTER, command_loop_error_count=_MAX_COUNTER)
    projection = project_cold_temperature(_state(vendor=vendor))

    assert projection.outcome == "observed"
    assert projection.status_packet_count == _MAX_COUNTER
    assert type(projection.status_packet_count) is int
    assert json.loads(json.dumps(dataclasses.asdict(projection)))["status_packet_count"] == (
        _MAX_COUNTER
    )


@pytest.mark.parametrize("raw", [0, 65535])
def test_cold_temperature_raw_uint16_bounds_are_accepted(raw: int) -> None:
    """Raw readings at the uint16 bounds are admitted as Celsius facts."""
    vendor = _vendor(raw_bean_temperature=raw)
    projection = project_cold_temperature(_state(vendor=vendor))

    assert projection.outcome == "observed"
    assert projection.last_packet_bean_temp_c == float(raw)
    assert projection.value_agreement == "disagree"


def test_cold_temperature_not_eligible_constant_has_no_values() -> None:
    """The shared not-eligible constant is versioned with every value `None`."""
    _assert_empty(COLD_TEMPERATURE_NOT_ELIGIBLE, "not_eligible")


def test_cold_temperature_unsupported_driver_name_is_unsupported() -> None:
    """M10: a non-Hottop driver name is unsupported, even with matching keys."""
    _assert_empty(project_cold_temperature(_state(driver="mock")), "unsupported")


def test_cold_temperature_non_string_driver_is_malformed() -> None:
    """M10: a non-exact-str driver identifier is malformed."""

    class DriverName(str):
        """`str` subclass equal to the Hottop name."""

    for driver in (7, DriverName(HOTTOP_DRIVER_NAME)):
        state = _state()
        object.__setattr__(state, "driver", driver)
        _assert_malformed(state)


@pytest.mark.parametrize(
    "value",
    [None, {}, object(), "state"],
)
def test_cold_temperature_non_roaster_state_is_malformed(value: object) -> None:
    """C1: any non-`RoasterState` input is malformed without attribute access."""
    _assert_malformed(value)


def test_cold_temperature_roaster_state_subclass_and_double_are_malformed() -> None:
    """C1: subclasses and duck-typed doubles never reach attribute access."""

    class StateSubclass(RoasterState):
        """Deliberate subclass of the exact snapshot type."""

    accessed: list[str] = []

    class Double:
        """Duck-typed double that records any attribute access."""

        def __getattr__(self, name: str) -> object:
            accessed.append(name)
            raise AssertionError(name)

    subclass = StateSubclass(
        driver=HOTTOP_DRIVER_NAME,
        connected=True,
        bean_temp_c=21.0,
        env_temp_c=22.0,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
        raw_vendor_data=cast(dict[str, EventPayloadValue], _vendor()),
    )
    _assert_malformed(subclass)
    _assert_malformed(Double())
    assert accessed == []


@pytest.mark.parametrize("raw", [65536, -1, 10**400])
def test_cold_temperature_raw_outside_uint16_is_malformed(raw: int) -> None:
    """M1: raw values outside `0..65535` are malformed, not disagree."""
    _assert_malformed(_with_vendor(_state(), _vendor(raw_bean_temperature=raw)))
    _assert_malformed(_with_vendor(_state(), _vendor(raw_env_temperature=raw)))


def test_cold_temperature_str_subclass_key_is_malformed() -> None:
    """M2: a `str` subclass key equal to a required name never stands in for it."""

    class KeyName(str):
        """`str` subclass that compares and hashes equal to its value."""

    vendor = _vendor()
    del vendor["status_packet_count"]
    vendor[KeyName("status_packet_count")] = 3
    _assert_malformed(_with_vendor(_state(), vendor))

    extra = _vendor()
    extra[KeyName("diagnostic")] = 1
    _assert_malformed(_with_vendor(_state(), extra))


def test_cold_temperature_hostile_key_is_malformed_without_invocation() -> None:
    """M2: a key whose hash and equality raise is rejected without being called."""
    calls: list[str] = []

    class HostileKey:
        """Key that becomes hostile after insertion."""

        armed = False

        def __hash__(self) -> int:
            if HostileKey.armed:
                calls.append("hash")
                raise RuntimeError("hostile hash")
            return hash("status_packet_count")

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise RuntimeError("hostile eq")

    vendor = _vendor()
    del vendor["status_packet_count"]
    vendor[cast(str, HostileKey())] = 3
    HostileKey.armed = True

    _assert_malformed(_with_vendor(_state(), vendor))
    assert calls == []


@pytest.mark.parametrize(
    "missing",
    [
        "temperature_unit",
        "resolved_temperature_unit",
        "raw_bean_temperature",
        "raw_env_temperature",
        "status_packet_count",
        "ignored_temperature_packet_count",
        "status_read_error_count",
        "command_loop_error_count",
    ],
)
def test_cold_temperature_missing_key_is_malformed(missing: str) -> None:
    """Every consumed vendor key is required."""
    vendor = _vendor()
    del vendor[missing]
    _assert_malformed(_with_vendor(_state(), vendor))


@pytest.mark.parametrize("vendor", [None, [("status_packet_count", 3)], "vendor"])
def test_cold_temperature_non_dict_vendor_data_is_malformed(vendor: object) -> None:
    """Vendor data must be an exact `dict`."""
    _assert_malformed(_with_vendor(_state(), vendor))


def test_cold_temperature_dict_subclass_vendor_data_is_malformed() -> None:
    """A `dict` subclass is not an exact vendor mapping."""

    class VendorDict(dict[str, object]):
        """Deliberate `dict` subclass."""

    _assert_malformed(_with_vendor(_state(), VendorDict(_vendor())))


class _IntSubclass(int):
    """Deliberate `int` subclass for exact-type boundaries."""


class _StrSubclass(str):
    """Deliberate `str` subclass for exact-type boundaries."""


class _FloatSubclass(float):
    """Deliberate `float` subclass for exact-type boundaries."""


@pytest.mark.parametrize(
    "counter",
    [
        "status_packet_count",
        "ignored_temperature_packet_count",
        "status_read_error_count",
        "command_loop_error_count",
    ],
)
@pytest.mark.parametrize("value", [True, False, _IntSubclass(0), 0.0, "0", None, -1, 2**53])
def test_cold_temperature_counter_type_and_bound_violations_are_malformed(
    counter: str, value: object
) -> None:
    """M4/M5: counters require exact in-range ints; bools and subclasses fail."""
    _assert_malformed(_with_vendor(_state(), _vendor(**{counter: value})))


@pytest.mark.parametrize("raw_key", ["raw_bean_temperature", "raw_env_temperature"])
@pytest.mark.parametrize("value", [True, _IntSubclass(21), 21.0, "21"])
def test_cold_temperature_raw_type_violations_are_malformed(raw_key: str, value: object) -> None:
    """M4: raws require exact ints; `bool` and `int` subclasses are rejected."""
    _assert_malformed(_with_vendor(_state(), _vendor(**{raw_key: value})))


@pytest.mark.parametrize("unit_key", ["temperature_unit", "resolved_temperature_unit"])
@pytest.mark.parametrize(
    "value", [_StrSubclass("celsius"), "Celsius", "kelvin", "", 1, True, b"celsius"]
)
def test_cold_temperature_unit_token_violations_are_malformed(unit_key: str, value: object) -> None:
    """M4: unit tokens require exact known `str` tokens."""
    _assert_malformed(_with_vendor(_state(), _vendor(**{unit_key: value})))


def test_cold_temperature_configured_unit_none_and_resolved_auto_are_malformed() -> None:
    """The configured unit is required and the resolved unit is never `auto`."""
    _assert_malformed(_with_vendor(_state(), _vendor(temperature_unit=None)))
    _assert_malformed(
        _with_vendor(_state(), _vendor(temperature_unit="auto", resolved_temperature_unit="auto"))
    )


@pytest.mark.parametrize(
    ("bean", "env"),
    [
        (math.nan, 22.0),
        (21.0, math.inf),
        (-math.inf, 22.0),
        (_FloatSubclass(21.0), 22.0),
        (21, 22.0),
        (True, 22.0),
        ("21.0", 22.0),
    ],
)
def test_cold_temperature_retained_non_finite_or_non_float_is_malformed(
    bean: object, env: object
) -> None:
    """M6: retained values must be exact finite floats."""
    state = _state()
    object.__setattr__(state, "bean_temp_c", bean)
    object.__setattr__(state, "env_temp_c", env)
    _assert_malformed(state)


@pytest.mark.parametrize(
    "case",
    [
        # S == 0 with a non-None raw.
        {
            "status_packet_count": 0,
            "resolved_temperature_unit": None,
            "raw_env_temperature": None,
            "bean": None,
            "env": None,
        },
        # S == 0 with a resolved unit.
        {
            "status_packet_count": 0,
            "raw_bean_temperature": None,
            "raw_env_temperature": None,
            "bean": None,
            "env": None,
        },
        # S == 0 with ignored packets counted.
        {
            "status_packet_count": 0,
            "ignored_temperature_packet_count": 1,
            "resolved_temperature_unit": None,
            "raw_bean_temperature": None,
            "raw_env_temperature": None,
            "bean": None,
            "env": None,
        },
        # S == 0 with retained values present.
        {
            "status_packet_count": 0,
            "resolved_temperature_unit": None,
            "raw_bean_temperature": None,
            "raw_env_temperature": None,
        },
        # S > 0 with a missing raw.
        {"raw_env_temperature": None},
        {"raw_bean_temperature": None, "raw_env_temperature": None},
        # I > S.
        {"ignored_temperature_packet_count": 4, "resolved_temperature_unit": None},
        # I == S > 0 with retained values present.
        {"ignored_temperature_packet_count": 3, "resolved_temperature_unit": None},
        # I == S > 0 with a resolved unit.
        {"ignored_temperature_packet_count": 3, "bean": None, "env": None},
        # I == S > 0 with a missing raw.
        {
            "ignored_temperature_packet_count": 3,
            "resolved_temperature_unit": None,
            "raw_bean_temperature": None,
            "bean": None,
            "env": None,
        },
        # I < S with R None and retained None.
        {
            "ignored_temperature_packet_count": 1,
            "resolved_temperature_unit": None,
            "bean": None,
            "env": None,
        },
        # R non-None with retained None.
        {"bean": None, "env": None},
        # S > 0, I == 0, R None, retained present: impossible (C2).
        {"resolved_temperature_unit": None},
        # Retained present for only one value.
        {"env": None},
        # Resolved unit incompatible with explicit configuration.
        {"resolved_temperature_unit": "fahrenheit"},
        {"temperature_unit": "fahrenheit"},
    ],
)
def test_cold_temperature_structural_violations_are_malformed(case: dict[str, object]) -> None:
    """M6/C2: every non-enumerated counter, unit, raw, or retained combination fails closed."""
    overrides = dict(case)
    bean = cast(float | None, overrides.pop("bean", 21.0))
    env = cast(float | None, overrides.pop("env", 22.0))
    state = _state()
    object.__setattr__(state, "bean_temp_c", bean)
    object.__setattr__(state, "env_temp_c", env)
    _assert_malformed(_with_vendor(state, _vendor(**overrides)))


def test_cold_temperature_deleted_attribute_is_contained_as_malformed() -> None:
    """M3: a deleted dataclass attribute maps to malformed instead of raising."""
    state = _state()
    object.__delattr__(state, "bean_temp_c")
    _assert_malformed(state)


def test_cold_temperature_raising_attribute_is_contained_without_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: an `Exception` raised mid-projection is contained without detail."""
    state = _state()

    def explode(instance: object, name: str) -> object:
        """Simulate a replaced snapshot attribute accessor."""
        del instance
        raise RuntimeError(f"SENTINEL-{name}")

    monkeypatch.setattr(RoasterState, "__getattribute__", explode)
    projection = project_cold_temperature(state)
    monkeypatch.undo()

    _assert_empty(projection, "malformed")
    assert "SENTINEL" not in json.dumps(dataclasses.asdict(projection))


def test_cold_temperature_base_exceptions_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-`Exception` faults such as `KeyboardInterrupt` are not contained."""
    state = _state()

    def interrupt(instance: object, name: str) -> object:
        """Simulate an interpreter interrupt during attribute access."""
        del instance, name
        raise KeyboardInterrupt

    monkeypatch.setattr(RoasterState, "__getattribute__", interrupt)
    with pytest.raises(KeyboardInterrupt):
        project_cold_temperature(state)


def test_cold_temperature_projection_serialises_with_exact_fields() -> None:
    """The 14-field grammar serialises to JSON with no message or detail field."""
    fields = [field.name for field in dataclasses.fields(ColdTemperatureProjection)]

    assert fields == ["projection_version", "outcome", *_VALUE_FIELDS]
    assert json.loads(json.dumps(dataclasses.asdict(project_cold_temperature(_state())))) == {
        "projection_version": 1,
        "outcome": "observed",
        "configured_temperature_unit": "celsius",
        "reported_temperature_unit": "celsius",
        "last_packet_valid": True,
        "last_packet_bean_temp_c": 21.0,
        "last_packet_env_temp_c": 22.0,
        "retained_bean_temp_c": 21.0,
        "retained_env_temp_c": 22.0,
        "value_agreement": "agree",
        "status_packet_count": 3,
        "ignored_temperature_packet_count": 0,
        "status_read_error_count": 1,
        "command_loop_error_count": 2,
    }
