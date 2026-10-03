"""Versioned cold-characterisation temperature projection (#227).

The projection reports driver facts from one already-read `RoasterState`
snapshot. It performs no screening, issues no freshness or quality verdict, and
makes no readiness or physical-safety claim; envelope and freshness screening
are owned by the downstream agent. Temperatures are Celsius only: no
Fahrenheit or unknown-unit numeric value is ever projected.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Final, Literal, cast

from coffee_roaster_mcp.drivers import HOTTOP_DRIVER_NAME, RoasterState

ColdTemperatureOutcome = Literal[
    "observed", "awaiting_first_packet", "not_eligible", "unsupported", "malformed"
]
ConfiguredTemperatureUnit = Literal["celsius", "fahrenheit", "auto"]
ProjectedTemperatureUnit = Literal["celsius", "fahrenheit", "unknown"]
ValueAgreement = Literal["agree", "disagree", "indeterminate"]

# JSON-exactness representation limit so projected counters stay exactly
# representable for JSON consumers. Not a hardware limit or screening threshold.
_MAX_JSON_EXACT_COUNTER: Final = 2**53 - 1
_MAX_RAW_UINT16: Final = 65535
_REQUIRED_KEYS: Final = (
    "temperature_unit",
    "resolved_temperature_unit",
    "raw_bean_temperature",
    "raw_env_temperature",
    "status_packet_count",
    "ignored_temperature_packet_count",
    "status_read_error_count",
    "command_loop_error_count",
)


@dataclass(frozen=True)
class ColdTemperatureProjection:
    """Typed cold-only temperature facts derived from one driver snapshot.

    Attributes:
        projection_version: Projection grammar version, always 1.
        outcome: Closed projection outcome.
        configured_temperature_unit: Driver-configured raw unit mechanism.
        reported_temperature_unit: Unit resolved for the latest counted packet.
        last_packet_valid: Whether the driver resolved a unit for the latest
            counted packet. Not a quality, accuracy, or freshness claim.
        last_packet_bean_temp_c: Latest raw bean value in Celsius, only when the
            latest packet resolved to Celsius.
        last_packet_env_temp_c: Latest raw environment value in Celsius, only
            when the latest packet resolved to Celsius.
        retained_bean_temp_c: Driver-retained last-good bean temperature in Celsius.
        retained_env_temp_c: Driver-retained last-good environment temperature
            in Celsius.
        value_agreement: Typed raw-versus-retained agreement only.
        status_packet_count: Counted status packets.
        ignored_temperature_packet_count: Counted packets with unresolved temperatures.
        status_read_error_count: Counted status read errors.
        command_loop_error_count: Counted command-loop errors.
    """

    projection_version: Literal[1]
    outcome: ColdTemperatureOutcome
    configured_temperature_unit: ConfiguredTemperatureUnit | None = None
    reported_temperature_unit: ProjectedTemperatureUnit | None = None
    last_packet_valid: bool | None = None
    last_packet_bean_temp_c: float | None = None
    last_packet_env_temp_c: float | None = None
    retained_bean_temp_c: float | None = None
    retained_env_temp_c: float | None = None
    value_agreement: ValueAgreement | None = None
    status_packet_count: int | None = None
    ignored_temperature_packet_count: int | None = None
    status_read_error_count: int | None = None
    command_loop_error_count: int | None = None


COLD_TEMPERATURE_NOT_ELIGIBLE: Final[ColdTemperatureProjection] = ColdTemperatureProjection(
    projection_version=1, outcome="not_eligible"
)
_UNSUPPORTED: Final = ColdTemperatureProjection(projection_version=1, outcome="unsupported")
_MALFORMED: Final = ColdTemperatureProjection(projection_version=1, outcome="malformed")


class _MalformedError(Exception):
    """Internal signal that the snapshot violates the closed grammar."""


def project_cold_temperature(driver_state: object) -> ColdTemperatureProjection:
    """Project cold-characterisation temperature facts from one driver snapshot.

    Args:
        driver_state: The exact `RoasterState` snapshot already read for the
            response. Any other object, including a subclass, is malformed.

    Returns:
        A valid projection. Any `Exception`-class fault maps to `malformed`
        with every value `None`; no exception detail is retained.
    """
    if type(driver_state) is not RoasterState:
        return _MALFORMED
    try:
        return _project(driver_state)
    except Exception:  # Containment: no message, repr, or object is retained.
        return _MALFORMED


def _project(state: RoasterState) -> ColdTemperatureProjection:
    """Validate the snapshot and derive the projection, raising on violation."""
    driver: object = state.driver
    if type(driver) is not str:
        return _MALFORMED
    if driver != HOTTOP_DRIVER_NAME:
        return _UNSUPPORTED

    mapping = _exact_mapping(state.raw_vendor_data)
    configured = _configured_unit(mapping["temperature_unit"])
    resolved = _resolved_unit(mapping["resolved_temperature_unit"])
    raw_bean = _optional_raw(mapping["raw_bean_temperature"])
    raw_env = _optional_raw(mapping["raw_env_temperature"])
    status_count = _counter(mapping["status_packet_count"])
    ignored_count = _counter(mapping["ignored_temperature_packet_count"])
    status_errors = _counter(mapping["status_read_error_count"])
    loop_errors = _counter(mapping["command_loop_error_count"])
    retained_bean = _optional_retained(state.bean_temp_c)
    retained_env = _optional_retained(state.env_temp_c)

    retained_present = _retained_presence(retained_bean, retained_env)
    _check_structure(
        configured=configured,
        resolved=resolved,
        raws_present=(raw_bean is not None, raw_env is not None),
        status_count=status_count,
        ignored_count=ignored_count,
        retained_present=retained_present,
    )

    outcome: ColdTemperatureOutcome = "observed"
    reported: ProjectedTemperatureUnit | None = "unknown"
    last_bean: float | None = None
    last_env: float | None = None
    agreement: ValueAgreement = "indeterminate"
    if status_count == 0:
        outcome = "awaiting_first_packet"
        reported = None
    elif resolved == "celsius":
        # Structure guarantees both raws are exact ints here; Celsius raws are
        # projected as-is with no unit conversion.
        reported = "celsius"
        last_bean = float(cast(int, raw_bean))
        last_env = float(cast(int, raw_env))
        agree = last_bean == retained_bean and last_env == retained_env
        agreement = "agree" if agree else "disagree"
    elif resolved == "fahrenheit":
        reported = "fahrenheit"
    return ColdTemperatureProjection(
        projection_version=1,
        outcome=outcome,
        configured_temperature_unit=configured,
        reported_temperature_unit=reported,
        last_packet_valid=resolved is not None,
        last_packet_bean_temp_c=last_bean,
        last_packet_env_temp_c=last_env,
        retained_bean_temp_c=retained_bean,
        retained_env_temp_c=retained_env,
        value_agreement=agreement,
        status_packet_count=status_count,
        ignored_temperature_packet_count=ignored_count,
        status_read_error_count=status_errors,
        command_loop_error_count=loop_errors,
    )


def _exact_mapping(value: object) -> dict[str, object]:
    """Return the exact-dict, exact-str-keyed mapping holding every required key."""
    if type(value) is not dict:
        raise _MalformedError
    # Type-check every key before any hash or equality is invoked on it.
    exact: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if type(key) is not str:
            raise _MalformedError
        exact[key] = item
    for name in _REQUIRED_KEYS:
        if name not in exact:
            raise _MalformedError
    return exact


def _configured_unit(value: object) -> ConfiguredTemperatureUnit:
    """Return the exact configured unit token."""
    if type(value) is not str:
        raise _MalformedError
    if value == "celsius":
        return "celsius"
    if value == "fahrenheit":
        return "fahrenheit"
    if value == "auto":
        return "auto"
    raise _MalformedError


def _resolved_unit(value: object) -> Literal["celsius", "fahrenheit"] | None:
    """Return the exact resolved unit token, which is never `auto`."""
    if value is None:
        return None
    configured = _configured_unit(value)
    if configured == "auto":
        raise _MalformedError
    return configured


def _optional_raw(value: object) -> int | None:
    """Return an exact uint16 raw reading or `None`."""
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_RAW_UINT16:
        raise _MalformedError
    return value


def _counter(value: object) -> int:
    """Return an exact non-negative, JSON-exact integer counter."""
    if type(value) is not int or not 0 <= value <= _MAX_JSON_EXACT_COUNTER:
        raise _MalformedError
    return value


def _optional_retained(value: object) -> float | None:
    """Return an exact finite retained Celsius float or `None`."""
    if value is None:
        return None
    if type(value) is not float or not isfinite(value):
        raise _MalformedError
    return value


def _retained_presence(bean: float | None, env: float | None) -> bool:
    """Return whether retained values are present, requiring both or neither."""
    if (bean is None) != (env is None):
        raise _MalformedError
    return bean is not None


def _check_structure(
    *,
    configured: ConfiguredTemperatureUnit,
    resolved: Literal["celsius", "fahrenheit"] | None,
    raws_present: tuple[bool, bool],
    status_count: int,
    ignored_count: int,
    retained_present: bool,
) -> None:
    """Enforce the closed counter, unit, raw, and retained-value invariants."""
    if status_count == 0:
        consistent = (
            raws_present == (False, False)
            and resolved is None
            and ignored_count == 0
            and not retained_present
        )
    elif ignored_count == status_count:
        consistent = raws_present == (True, True) and resolved is None and not retained_present
    else:
        consistent = (
            raws_present == (True, True)
            and ignored_count < status_count
            and retained_present
            and (resolved is not None or ignored_count >= 1)
        )
    if not consistent:
        raise _MalformedError
    if configured != "auto" and resolved is not None and resolved != configured:
        raise _MalformedError
