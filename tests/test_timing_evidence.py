"""Mock-only contract tests for the ``timing_evidence/v1`` boundary."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import math
import os
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

import coffee_roaster_mcp.mcp_server as mcp_server
import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.drivers import (
    HottopRoasterDriver,
    MockRoasterDriver,
    RoasterCapabilities,
    RoasterState,
)
from coffee_roaster_mcp.session import RoastSessionStore


class _Clock:
    """Deterministic exact-float clock for one timing-evidence test."""

    def __init__(self) -> None:
        self.values = iter((100.0, 100.1, 100.2, 101.25, 102.5, 103.0, 104.0, 105.0, 106.0))

    def now(self) -> float:
        """Return the next injected clock value."""
        return next(self.values)


def _context() -> tuple[mcp_server.ServerContext, MockRoasterDriver, str]:
    """Build a minimal mock-only context accepted by the orchestration function."""
    clock = _Clock()
    store = RoastSessionStore(
        utc_now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        monotonic_now=clock.now,
        session_id_factory=iter(("timing-session", "replacement-session")).__next__,
    )
    session = store.start_session()
    store.record_event(session, "beans_added", provenance="manual_mark")
    store.record_event(session, "first_crack_detected", provenance="manual_mark")
    driver = MockRoasterDriver()
    driver.connect()
    context = SimpleNamespace(
        session_store=store,
        roaster_driver=driver,
        config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
    )
    return cast(mcp_server.ServerContext, context), driver, session.id


def _run(context: mcp_server.ServerContext, session_id: str) -> mcp_server.TimingEvidenceResult:
    """Call the private orchestration seam through its public-tool implementation."""
    return mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id=session_id
    )


def _assert_refusal(
    result: mcp_server.TimingEvidenceResult,
    *,
    outcome: str,
    reason: str,
    driver_read: str,
) -> None:
    """Assert the complete closed refusal row shared by negative witnesses."""
    assert result.outcome == outcome
    assert result.reason_code == reason
    assert result.driver_read == driver_read
    assert result.record is None


def _count_reads(monkeypatch: pytest.MonkeyPatch, driver: MockRoasterDriver) -> list[int]:
    """Install a counting mock read wrapper and return its mutable counter."""
    reads = [0]
    original = driver.read_state

    def counted() -> RoasterState:
        reads[0] += 1
        return original()

    monkeypatch.setattr(driver, "read_state", counted)
    return reads


def _state(*, bean: object = 20.0, environment: object = 21.0) -> RoasterState:
    """Build an otherwise-valid state while allowing raw channel corruption."""
    result = RoasterState(
        driver="mock",
        connected=True,
        bean_temp_c=20.0,
        env_temp_c=21.0,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
    )
    object.__setattr__(result, "bean_temp_c", bean)
    object.__setattr__(result, "env_temp_c", environment)
    return result


def test_timing_evidence_returns_atomic_mock_record() -> None:
    """A connected exact mock yields canonical events and one shared bracket."""
    context, _, session_id = _context()

    result = mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id=session_id
    )

    assert result.outcome == "evidence"
    assert result.record is not None
    assert result.record.evidence_sequence == 1
    assert result.record.charge is not None
    assert result.record.charge.source == "manual_mark"
    assert result.record.first_crack is not None
    assert result.record.first_crack.time_basis == "producer_canonical_record"
    assert result.record.bean.status == "valid"
    assert result.record.environment.status == "valid"
    assert result.record.bean.acquisition_lo_seconds.hex() == (1.25).hex()
    assert result.record.bean.acquisition_hi_seconds.hex() == (2.5).hex()
    assert result.record.bean.clock_epoch_id == result.record.clock.epoch_id
    assert result.record.environment.clock_epoch_id == result.record.clock.epoch_id


def test_timing_evidence_preserves_exact_bracket_and_has_no_session_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic read neither rounds values nor processes session machinery."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    values = iter((100.0, 100.30000000000004))
    monkeypatch.setattr(store, "read_timing_evidence_clock", lambda: next(values))
    state = _state(bean=196.00000000000003, environment=0.30000000000000004)
    monkeypatch.setattr(driver, "read_state", lambda: state)
    before = (
        list(session.telemetry_buffer),
        list(session.event_timeline),
        session.phase,
        session.auto_t0_charge_temperature_c,
        session.first_crack_source,
        session.pending_driver_command_token,
    )

    result = _run(context, session_id)

    assert result.record is not None
    assert result.record.bean.status == "valid"
    assert result.record.environment.status == "valid"
    assert result.record.bean.celsius.hex() == (196.00000000000003).hex()
    assert result.record.environment.celsius.hex() == (0.30000000000000004).hex()
    assert result.record.bean.acquisition_lo_seconds.hex() == (0.0).hex()
    assert result.record.bean.acquisition_hi_seconds.hex() == (100.30000000000004 - 100.0).hex()
    assert (
        list(session.telemetry_buffer),
        list(session.event_timeline),
        session.phase,
        session.auto_t0_charge_temperature_c,
        session.first_crack_source,
        session.pending_driver_command_token,
    ) == before


def test_timing_evidence_classifies_one_bad_channel_without_losing_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel corruption remains local and does not coerce an integer to float."""
    context, driver, session_id = _context()
    state = RoasterState(
        driver="mock",
        connected=True,
        bean_temp_c=20.0,
        env_temp_c=20.0,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
    )
    object.__setattr__(state, "env_temp_c", 7)
    monkeypatch.setattr(driver, "read_state", lambda: state)

    result = mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id=session_id
    )

    assert result.outcome == "evidence"
    assert result.record is not None
    assert result.record.bean.status == "valid"
    assert result.record.environment.status == "invalid"
    assert result.record.environment.reason == "not_exact_float"


def test_timing_evidence_refuses_mock_subclasses_without_reading() -> None:
    """Only the exact built-in mock type carries synthetic qualification."""

    class _Subclass(MockRoasterDriver):
        pass

    context, _, session_id = _context()
    driver = _Subclass()
    driver.connect()
    called = False

    def unexpected_read() -> RoasterState:
        nonlocal called
        called = True
        raise AssertionError("refused composition must not read the driver")

    driver.read_state = unexpected_read  # type: ignore[method-assign]
    object.__setattr__(context, "roaster_driver", driver)

    result = mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id=session_id
    )

    assert result.outcome == "refused_capability"
    assert result.reason_code == "composition_not_qualified"
    assert result.driver_read == "not_invoked"
    assert called is False


def test_timing_evidence_precheck_prevents_driver_read_for_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity precedes capability and does not amplify an invalid request."""
    context, driver, _ = _context()
    reads = 0
    original = driver.read_state

    def counted_read() -> RoasterState:
        nonlocal reads
        reads += 1
        return original()

    monkeypatch.setattr(driver, "read_state", counted_read)

    result = mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id="unknown"
    )

    assert result.outcome == "refused_identity"
    assert result.reason_code == "unknown_session"
    assert result.driver_read == "not_invoked"
    assert reads == 0


def test_timing_evidence_capability_version_refuses_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported declaration is rejected before the synthetic read."""
    context, driver, session_id = _context()
    reads = 0
    capabilities = driver.capabilities

    def version_two(_: MockRoasterDriver) -> RoasterCapabilities:
        return RoasterCapabilities(
            driver=capabilities.driver,
            heat=capabilities.heat,
            fan=capabilities.fan,
            actions=capabilities.actions,
            sensor_units=capabilities.sensor_units,
            command_streaming=capabilities.command_streaming,
            heat_containment_version=capabilities.heat_containment_version,
            manual_first_crack_version=capabilities.manual_first_crack_version,
            timing_evidence_version=2,
        )

    def unexpected_read() -> RoasterState:
        nonlocal reads
        reads += 1
        raise AssertionError("unsupported capability must not read")

    monkeypatch.setattr(MockRoasterDriver, "capabilities", property(version_two))
    monkeypatch.setattr(driver, "read_state", unexpected_read)

    result = mcp_server._run_timing_evidence(  # pyright: ignore[reportPrivateUsage]
        context, expected_session_id=session_id
    )

    assert result.outcome == "refused_capability"
    assert result.reason_code == "capability_version_unsupported"
    assert result.driver_read == "not_invoked"
    assert reads == 0


@pytest.mark.parametrize(
    ("kind", "source", "time_basis", "accepted"),
    (
        ("beans_added", "manual_mark", "producer_canonical_record", True),
        ("beans_added", "auto_t0", "producer_canonical_record", True),
        ("beans_added", "model_detection", "detector_clock_rebased", False),
        ("beans_added", "unattributed", "producer_canonical_record", True),
        ("first_crack_detected", "manual_mark", "producer_canonical_record", True),
        ("first_crack_detected", "auto_t0", "producer_canonical_record", False),
        ("first_crack_detected", "model_detection", "detector_clock_rebased", True),
        ("first_crack_detected", "unattributed", "producer_canonical_record", True),
        ("beans_dropped", "manual_mark", "producer_canonical_record", False),
        ("beans_dropped", "auto_t0", "producer_canonical_record", False),
        ("beans_dropped", "model_detection", "detector_clock_rebased", False),
        ("beans_dropped", "unattributed", "producer_canonical_record", True),
    ),
)
def test_event_evidence_enforces_the_closed_kind_source_matrix(
    kind: str, source: str, time_basis: str, accepted: bool
) -> None:
    """Each source/kind pairing is accepted only when §4.5 permits it.

    This is the causal witness for the kind/source guard: removing that guard
    admits each rejected pairing.
    """
    row = {
        "kind": kind,
        "event_index": 1,
        "canonical_seconds": 1.0,
        "clock_epoch_id": "0" * 32,
        "source": source,
        "time_basis": time_basis,
        "physical_time_bound": "not_provided",
    }
    if accepted:
        assert mcp_server.EventEvidence.model_validate(row).source == source
    else:
        with pytest.raises((TypeError, ValueError)):
            mcp_server.EventEvidence.model_validate(row)


def test_event_evidence_rejects_inconsistent_source_time_basis() -> None:
    """The §18.1 source/time-basis guard remains independently causal."""
    with pytest.raises((TypeError, ValueError)):
        mcp_server.EventEvidence.model_validate(
            {
                "kind": "first_crack_detected",
                "event_index": 1,
                "canonical_seconds": 1.0,
                "clock_epoch_id": "0" * 32,
                "source": "model_detection",
                "time_basis": "producer_canonical_record",
                "physical_time_bound": "not_provided",
            }
        )


def test_hottop_reception_read_models_close_settlement_slots_and_reasons() -> None:
    """Advertised and typed read models reject crossed slots and bad refusal reasons."""
    settlement_schema = mcp_server.HottopCommandSettlements.model_json_schema()
    pair_schema = mcp_server.HottopIneligibleReceptionPair.model_json_schema()
    settlement_validator: Any = Draft202012Validator(settlement_schema)
    pair_validator: Any = Draft202012Validator(pair_schema)
    settlement = {
        "transition_seq": 1,
        "result": "pending",
        "attempt_seq": None,
        "physical_effect": "not_established",
    }
    valid_settlements = (
        {"drop": {**settlement, "transition": "drop"}, "emergency": None},
        {"drop": None, "emergency": {**settlement, "transition": "emergency"}},
    )
    for value in valid_settlements:
        settlement_validator.validate(value)
        assert mcp_server.HottopCommandSettlements.model_validate(value)

    invalid_settlements = (
        {"drop": {**settlement, "transition": "emergency"}, "emergency": None},
        {"drop": None, "emergency": {**settlement, "transition": "drop"}},
    )
    for value in invalid_settlements:
        with pytest.raises(ValidationError):
            settlement_validator.validate(value)
        with pytest.raises((TypeError, ValueError)):
            mcp_server.HottopCommandSettlements.model_validate(value)

    for value in (
        {"status": "ineligible", "reasons": ["no_receipt"]},
        {"status": "ineligible", "reasons": ["no_receipt", "superseded"]},
    ):
        pair_validator.validate(value)
        assert mcp_server.HottopIneligibleReceptionPair.model_validate(
            {**value, "reasons": tuple(value["reasons"])}
        )
    invalid_pairs: tuple[dict[str, object], ...] = (
        {"status": "ineligible", "reasons": []},
        {"status": "ineligible", "reasons": ["no_receipt", "no_receipt"]},
    )
    for value in invalid_pairs:
        with pytest.raises(ValidationError):
            pair_validator.validate(value)
        with pytest.raises((TypeError, ValueError)):
            mcp_server.HottopIneligibleReceptionPair.model_validate(
                {**value, "reasons": tuple(cast(list[str], value["reasons"]))}
            )


def test_timing_evidence_models_reject_inexact_rows() -> None:
    """Wire models reject inexact values before output."""
    with pytest.raises((TypeError, ValueError)):
        mcp_server.TimingEvidenceResult.model_validate(
            {
                "evidence_protocol": "timing_evidence/v1",
                "evidence_version": True,
                "outcome": "unavailable",
                "reason_code": "driver_raised",
                "driver_read": "invoked",
                "requested_session_id": "session",
                "record": None,
            }
        )


def test_timing_evidence_schema_is_closed_at_the_sdk_boundary() -> None:
    """The registered tool exposes exactly the two required closed inputs."""
    server = mcp_server.create_mcp_server()
    tool = server._tool_manager.get_tool("get_timing_evidence")  # pyright: ignore[reportPrivateUsage]
    assert tool is not None

    assert tool.parameters == {
        "additionalProperties": False,
        "description": "Strict raw SDK-boundary arguments for synthetic timing evidence.",
        "properties": {
            "expected_session_id": {
                "maxLength": 128,
                "minLength": 1,
                "title": "Expected Session Id",
                "type": "string",
            },
            "evidence_protocol": {
                "const": "timing_evidence/v1",
                "title": "Evidence Protocol",
                "type": "string",
            },
        },
        "required": ["expected_session_id", "evidence_protocol"],
        "title": "_TimingEvidenceArguments",
        "type": "object",
    }


_FORBIDDEN_SESSION_IMPORTS = frozenset(
    {"coffee_roaster_mcp.drivers", "coffee_roaster_mcp.mcp_server"}
)


def _static_imported_modules(source: str) -> set[str]:
    """Return closed-package modules directly bound by static import statements."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module in {"drivers", "mcp_server"}:
                imported.add(f"coffee_roaster_mcp.{node.module}")
            elif (node.level, node.module) in {(1, None), (0, "coffee_roaster_mcp")}:
                imported.update(
                    f"coffee_roaster_mcp.{alias.name}"
                    for alias in node.names
                    if alias.name in {"drivers", "mcp_server"}
                )
            elif node.level == 0 and node.module in _FORBIDDEN_SESSION_IMPORTS:
                imported.add(node.module)
    return imported


@pytest.mark.parametrize(
    ("source", "is_forbidden"),
    [
        ("from .drivers import MockRoasterDriver", True),
        ("from . import drivers", True),
        ("from coffee_roaster_mcp import drivers", True),
        ("import coffee_roaster_mcp.drivers", True),
        ("from .mcp_server import ServerContext", True),
        ("from . import mcp_server", True),
        ("from coffee_roaster_mcp import mcp_server", True),
        ("import coffee_roaster_mcp.mcp_server", True),
        ("from . import controls", False),
        ("from coffee_roaster_mcp.config import RoastConfig", False),
    ],
)
def test_session_dependency_import_guard_discriminates_static_import_forms(
    source: str, is_forbidden: bool
) -> None:
    """The AST guard rejects driver/server imports without banning allowed modules."""
    assert bool(_static_imported_modules(source) & _FORBIDDEN_SESSION_IMPORTS) is is_forbidden


def test_session_timing_ownership_does_not_depend_on_mcp_or_drivers() -> None:
    """The store keeps its closed direction while accepting plain evidence values."""
    imported = _static_imported_modules(inspect.getsource(session_module))

    assert not imported & _FORBIDDEN_SESSION_IMPORTS


@pytest.mark.parametrize(
    ("channel", "value", "status", "reason"),
    [
        ("bean", None, "unavailable", "reported_none"),
        ("environment", None, "unavailable", "reported_none"),
        ("bean", 7, "invalid", "not_exact_float"),
        ("environment", 7, "invalid", "not_exact_float"),
        ("bean", True, "invalid", "not_exact_float"),
        ("environment", True, "invalid", "not_exact_float"),
        ("bean", "7.0", "invalid", "not_exact_float"),
        ("environment", "7.0", "invalid", "not_exact_float"),
        ("bean", math.nan, "invalid", "non_finite"),
        ("bean", math.inf, "invalid", "non_finite"),
        ("environment", math.inf, "invalid", "non_finite"),
        ("bean", -math.inf, "invalid", "non_finite"),
        ("environment", -math.inf, "invalid", "non_finite"),
        ("environment", math.nan, "invalid", "non_finite"),
    ],
)
def test_timing_evidence_channel_matrix_is_local_and_non_coercing(
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    value: object,
    status: str,
    reason: str,
) -> None:
    """Every malformed channel remains local while the peer stays valid."""
    context, driver, session_id = _context()
    monkeypatch.setattr(
        driver,
        "read_state",
        lambda: _state(
            bean=value if channel == "bean" else 20.0,
            environment=value if channel == "environment" else 21.0,
        ),
    )

    result = _run(context, session_id)

    assert result.outcome == "evidence"
    assert result.record is not None
    tested = getattr(result.record, channel)
    peer = result.record.environment if channel == "bean" else result.record.bean
    assert tested.status == status
    assert tested.reason == reason  # type: ignore[union-attr]
    assert peer.status == "valid"


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        (None, "driver_result_invalid"),
        (object(), "driver_result_invalid"),
        (_state(), "driver_result_invalid"),
    ],
)
def test_timing_evidence_rejects_non_exact_driver_state(
    monkeypatch: pytest.MonkeyPatch, replacement: object, reason: str
) -> None:
    """Only an exact connected mock state can begin channel classification."""
    context, driver, session_id = _context()
    if isinstance(replacement, RoasterState):
        object.__setattr__(replacement, "driver", "hottop")
    monkeypatch.setattr(driver, "read_state", lambda: replacement)
    result = _run(context, session_id)
    _assert_refusal(result, outcome="unavailable", reason=reason, driver_read="invoked")


def test_timing_evidence_rejects_state_subclass_and_string_subclass_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-state checks reject lookalikes before channel-local classification."""
    context, driver, session_id = _context()

    class _StateSubclass(RoasterState):
        pass

    subclass_state = _state()
    object.__setattr__(subclass_state, "__class__", _StateSubclass)
    monkeypatch.setattr(driver, "read_state", lambda: subclass_state)
    _assert_refusal(
        _run(context, session_id),
        outcome="unavailable",
        reason="driver_result_invalid",
        driver_read="invoked",
    )

    class _MockString(str):
        pass

    string_subclass_state = _state()
    object.__setattr__(string_subclass_state, "driver", _MockString("mock"))
    monkeypatch.setattr(driver, "read_state", lambda: string_subclass_state)
    _assert_refusal(
        _run(context, session_id),
        outcome="unavailable",
        reason="driver_result_invalid",
        driver_read="invoked",
    )


@pytest.mark.parametrize("connected", [False, 1, "yes"])
def test_timing_evidence_rejects_disconnected_or_non_bool_state(
    monkeypatch: pytest.MonkeyPatch, connected: object
) -> None:
    """Connection is an exact boolean whole-read guard."""
    context, driver, session_id = _context()
    state = _state()
    object.__setattr__(state, "connected", connected)
    monkeypatch.setattr(driver, "read_state", lambda: state)
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="unavailable",
        reason="driver_not_connected" if connected is False else "driver_result_invalid",
        driver_read="invoked",
    )


def test_timing_evidence_does_not_leak_driver_exception_or_mutate_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver faults are closed results with no exception-text disclosure."""
    context, driver, session_id = _context()
    session = context.session_store.get_latest_session()
    assert session is not None
    before = (list(session.event_timeline), list(session.telemetry_buffer), session.phase)

    def explode() -> RoasterState:
        raise RuntimeError("P1-SENTINEL-DO-NOT-LEAK")

    monkeypatch.setattr(driver, "read_state", explode)
    result = _run(context, session_id)
    _assert_refusal(result, outcome="unavailable", reason="driver_raised", driver_read="invoked")
    assert "P1-SENTINEL-DO-NOT-LEAK" not in result.model_dump_json()
    assert (list(session.event_timeline), list(session.telemetry_buffer), session.phase) == before


@pytest.mark.parametrize(
    ("requested", "expected_reason"),
    [("missing", "unknown_session"), ("old", "not_latest_session")],
)
def test_timing_evidence_identity_rows_precede_capability(
    monkeypatch: pytest.MonkeyPatch, requested: str, expected_reason: str
) -> None:
    """Identity rejects before an incapable driver can be inspected or read."""
    context, driver, session_id = _context()
    if requested == "old":
        context.session_store.stop_session()
        replacement = context.session_store.start_session()
        session_id = replacement.id
    reads = _count_reads(monkeypatch, driver)
    monkeypatch.setattr(MockRoasterDriver, "capabilities", property(lambda _: object()))
    result = _run(context, requested if requested == "missing" else "timing-session")
    _assert_refusal(
        result, outcome="refused_identity", reason=expected_reason, driver_read="not_invoked"
    )
    assert reads == [0]
    assert session_id


def test_timing_evidence_post_capability_identity_revalidation_has_no_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability hook replacing the session is caught by admission before t0."""
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    original = MockRoasterDriver.capabilities

    def replace(_: MockRoasterDriver) -> RoasterCapabilities:
        context.session_store.stop_session()
        context.session_store.start_session()
        return original.__get__(driver, MockRoasterDriver)

    monkeypatch.setattr(MockRoasterDriver, "capabilities", property(replace))
    result = _run(context, session_id)
    _assert_refusal(
        result, outcome="refused_identity", reason="not_latest_session", driver_read="not_invoked"
    )
    assert reads == [0]


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("timing_evidence_version", None, "capability_absent"),
        ("timing_evidence_version", True, "capability_malformed"),
        ("timing_evidence_version", 2, "capability_version_unsupported"),
        ("driver", "other", "composition_not_qualified"),
    ],
)
def test_timing_evidence_capability_rows_have_zero_reads(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, reason: str
) -> None:
    """Each malformed capability row fails closed independently."""
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    caps = driver.capabilities

    def mutated(_: MockRoasterDriver) -> RoasterCapabilities:
        return RoasterCapabilities(
            driver=cast(str, value) if attribute == "driver" else caps.driver,
            heat=caps.heat,
            fan=caps.fan,
            actions=caps.actions,
            sensor_units=caps.sensor_units,
            command_streaming=caps.command_streaming,
            heat_containment_version=caps.heat_containment_version,
            manual_first_crack_version=caps.manual_first_crack_version,
            timing_evidence_version=cast(int | None, value)
            if attribute == "timing_evidence_version"
            else caps.timing_evidence_version,
        )

    monkeypatch.setattr(MockRoasterDriver, "capabilities", property(mutated))
    result = _run(context, session_id)
    _assert_refusal(result, outcome="refused_capability", reason=reason, driver_read="not_invoked")
    assert reads == [0]


def test_timing_evidence_capability_property_and_config_are_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable declarations and configured composition never invoke reads."""
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    monkeypatch.setattr(
        MockRoasterDriver, "capabilities", property(lambda _: (_ for _ in ()).throw(RuntimeError()))
    )
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="refused_capability",
        reason="capability_unreadable",
        driver_read="not_invoked",
    )
    assert reads == [0]

    monkeypatch.undo()
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    object.__setattr__(context.config.roaster, "driver", "hottop")
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="refused_capability",
        reason="composition_not_qualified",
        driver_read="not_invoked",
    )
    assert reads == [0]


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("name", "composition_not_qualified"),
        ("capabilities_driver", "composition_not_qualified"),
        ("configured_driver", "composition_not_qualified"),
        ("hottop_absent", "capability_absent"),
        ("hottop_version_one", "composition_not_qualified"),
    ],
)
def test_timing_evidence_composition_subguards_are_independent(
    monkeypatch: pytest.MonkeyPatch, mutation: str, expected_reason: str
) -> None:
    """Every capability/composition component refuses before a synthetic read."""
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    caps = driver.capabilities
    if mutation == "name":
        monkeypatch.setattr(MockRoasterDriver, "name", "not-mock")
        monkeypatch.setattr(MockRoasterDriver, "capabilities", property(lambda _: caps))
    elif mutation == "capabilities_driver":
        monkeypatch.setattr(
            MockRoasterDriver,
            "capabilities",
            property(
                lambda _: RoasterCapabilities(
                    driver="hottop",
                    heat=caps.heat,
                    fan=caps.fan,
                    actions=caps.actions,
                    sensor_units=caps.sensor_units,
                    command_streaming=caps.command_streaming,
                    heat_containment_version=caps.heat_containment_version,
                    manual_first_crack_version=caps.manual_first_crack_version,
                    timing_evidence_version=1,
                )
            ),
        )
    elif mutation == "configured_driver":
        object.__setattr__(context.config.roaster, "driver", "hottop")
    else:

        class _HottopNamedDriver:
            name = "hottop"

            @property
            def capabilities(self) -> RoasterCapabilities:
                return RoasterCapabilities(
                    driver="hottop",
                    heat=caps.heat,
                    fan=caps.fan,
                    actions=caps.actions,
                    sensor_units=caps.sensor_units,
                    command_streaming=caps.command_streaming,
                    heat_containment_version=caps.heat_containment_version,
                    manual_first_crack_version=caps.manual_first_crack_version,
                    timing_evidence_version=None if mutation == "hottop_absent" else 1,
                )

            def read_state(self) -> RoasterState:
                raise AssertionError("capability refusal must not read")

        object.__setattr__(context, "roaster_driver", _HottopNamedDriver())
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="refused_capability",
        reason=expected_reason,
        driver_read="not_invoked",
    )
    assert reads == [0]


def test_timing_evidence_real_hottop_capability_never_opens_or_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual Hottop class is refused before its transport can be touched."""
    context, _, session_id = _context()
    opened = [0]

    def unopened_transport(*_: object, **__: object) -> object:
        opened[0] += 1
        raise AssertionError("timing evidence must not open a Hottop transport")

    driver = HottopRoasterDriver(serial_factory=unopened_transport)  # type: ignore[arg-type]
    reads = [0]
    original_read = driver.read_state

    def counted_read() -> RoasterState:
        reads[0] += 1
        return original_read()

    monkeypatch.setattr(driver, "read_state", counted_read)
    object.__setattr__(context, "roaster_driver", driver)

    absent = _run(context, session_id)
    _assert_refusal(
        absent,
        outcome="refused_capability",
        reason="capability_absent",
        driver_read="not_invoked",
    )
    assert opened == [0]
    assert reads == [0]

    original_capabilities = HottopRoasterDriver.capabilities

    def version_one(_: HottopRoasterDriver) -> RoasterCapabilities:
        caps = original_capabilities.__get__(driver, HottopRoasterDriver)
        return RoasterCapabilities(
            driver=caps.driver,
            heat=caps.heat,
            fan=caps.fan,
            actions=caps.actions,
            sensor_units=caps.sensor_units,
            command_streaming=caps.command_streaming,
            heat_containment_version=caps.heat_containment_version,
            manual_first_crack_version=caps.manual_first_crack_version,
            timing_evidence_version=1,
        )

    monkeypatch.setattr(HottopRoasterDriver, "capabilities", property(version_one))
    versioned = _run(context, session_id)
    _assert_refusal(
        versioned,
        outcome="refused_capability",
        reason="composition_not_qualified",
        driver_read="not_invoked",
    )
    assert opened == [0]
    assert reads == [0]

    monkeypatch.setattr(HottopRoasterDriver, "capabilities", property(lambda _: object()))
    malformed = _run(context, session_id)
    _assert_refusal(
        malformed,
        outcome="refused_capability",
        reason="capability_malformed",
        driver_read="not_invoked",
    )
    assert opened == [0]
    assert reads == [0]


def test_timing_evidence_epoch_is_store_lifetime_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen returned clock exposes one immutable store-lifetime epoch."""
    values = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(session_module, "_generate_clock_epoch_id", lambda: next(values))
    one = RoastSessionStore(
        monotonic_now=lambda: 1.0,
        session_id_factory=iter(("one", "one-next")).__next__,
    )
    first = one.start_session()
    one.record_event(first, "beans_added", provenance="manual_mark")
    driver = MockRoasterDriver()
    driver.connect()
    context = cast(
        mcp_server.ServerContext,
        SimpleNamespace(
            session_store=one,
            roaster_driver=driver,
            config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
        ),
    )
    first_result = _run(context, first.id)
    assert first_result.record is not None
    one.stop_session()
    second = one.start_session()
    one.record_event(second, "beans_added", provenance="manual_mark")
    second_result = _run(context, second.id)
    assert second_result.record is not None
    two = RoastSessionStore(monotonic_now=lambda: 1.0, session_id_factory=lambda: "two")
    assert first_result.record.clock.epoch_id == "a" * 32
    assert second_result.record.clock.epoch_id == first_result.record.clock.epoch_id
    assert second_result.record.clock.epoch_id != "b" * 32
    assert two._clock_epoch_id == "b" * 32  # pyright: ignore[reportPrivateUsage]
    assert first.id == "one"
    monkeypatch.setattr(session_module, "_generate_clock_epoch_id", lambda: "INVALID")
    with pytest.raises(ValueError):
        RoastSessionStore(monotonic_now=lambda: 1.0)


def test_timing_evidence_record_rejects_mismatched_event_epoch() -> None:
    """A record epoch tag cannot be substituted for a canonical event epoch."""
    context, _, session_id = _context()
    result = _run(context, session_id)
    assert result.record is not None and result.record.charge is not None
    row = result.record.model_dump()
    charge = cast(dict[str, object], row["charge"])
    charge["clock_epoch_id"] = "f" * 32
    try:
        mcp_server.TimingEvidenceRecord.model_validate(row)
    except (TypeError, ValueError):
        return
    raise AssertionError("mismatched event epoch was accepted")


def test_timing_evidence_sequence_rollover_and_invalid_assembly_do_not_commit() -> None:
    """Wire bounds and failed finalization leave the session sequence untouched."""
    context, _, session_id = _context()
    session = context.session_store.get_latest_session()
    assert session is not None
    session.timing_evidence_sequence = 2**53 - 2
    final = _run(context, session_id)
    assert final.record is not None and final.record.evidence_sequence == 2**53 - 1
    exhausted = _run(context, session_id)
    _assert_refusal(
        exhausted, outcome="unavailable", reason="sequence_exhausted", driver_read="invoked"
    )
    assert session.timing_evidence_sequence == 2**53 - 1

    session.timing_evidence_sequence = 0
    session.beans_added_source = "manual_mark"
    session.event_timeline.clear()
    invalid = _run(context, session_id)
    _assert_refusal(invalid, outcome="unavailable", reason="record_invalid", driver_read="invoked")
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_finalizer_failure_is_observed_before_any_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G18: a failed final validation reaches finalization and cannot advance state."""
    context, _, session_id = _context()
    session = context.session_store.get_latest_session()
    assert session is not None
    before = (
        session.timing_evidence_sequence,
        session.timing_evidence_last_lo,
        session.timing_evidence_last_hi,
    )
    called = False
    original = mcp_server._timing_evidence_result  # pyright: ignore[reportPrivateUsage]

    def reject_candidate(**kwargs: object) -> mcp_server.TimingEvidenceResult:
        nonlocal called
        if kwargs["outcome"] == "evidence":
            called = True
            raise ValueError("test finalizer rejection")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_server, "_timing_evidence_result", reject_candidate)
    result = _run(context, session_id)
    _assert_refusal(result, outcome="unavailable", reason="record_invalid", driver_read="invoked")
    assert called is True
    assert (
        session.timing_evidence_sequence,
        session.timing_evidence_last_lo,
        session.timing_evidence_last_hi,
    ) == before


def test_timing_evidence_wire_ownership_bound_is_enforced_by_its_model() -> None:
    """G19: direct model validation exercises the wire bound without store masking."""
    context, _, session_id = _context()
    result = _run(context, session_id)
    assert result.record is not None
    row = result.record.model_dump()
    row["session_ownership_sequence"] = 2**53

    try:
        mcp_server.TimingEvidenceRecord.model_validate(row)
    except (TypeError, ValueError):
        return
    raise AssertionError("ownership sequence above the exact wire bound was accepted")


def test_timing_evidence_revalidates_same_id_replacement_and_ownership_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5: object and ownership identity both survive an identical textual id."""
    clock = iter((0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)).__next__
    store = RoastSessionStore(monotonic_now=clock, session_id_factory=lambda: "same-id")
    original_session = store.start_session()
    driver = MockRoasterDriver()
    driver.connect()
    context = cast(
        mcp_server.ServerContext,
        SimpleNamespace(
            session_store=store,
            roaster_driver=driver,
            config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
        ),
    )
    reads = _count_reads(monkeypatch, driver)
    original_read = driver.read_state

    def replace_same_id() -> RoasterState:
        store.stop_session()
        store.start_session()
        return original_read()

    monkeypatch.setattr(driver, "read_state", replace_same_id)
    replaced = _run(context, original_session.id)
    _assert_refusal(
        replaced,
        outcome="refused_identity",
        reason="session_changed_during_read",
        driver_read="invoked",
    )
    assert reads == [1]

    current = store.get_latest_session()
    assert current is not None

    def change_ownership() -> RoasterState:
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            current.capture_ownership_sequence += 1
        return original_read()

    monkeypatch.setattr(driver, "read_state", change_ownership)
    changed = _run(context, current.id)
    _assert_refusal(
        changed,
        outcome="refused_identity",
        reason="session_changed_during_read",
        driver_read="invoked",
    )
    assert reads == [2]


def test_timing_evidence_fault_and_inactive_drop_win_over_busy_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G6: post-read fault or inactivity wins even while a Drop reservation remains."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.event_timeline = [
            event for event in session.event_timeline if event.kind != "first_crack_detected"
        ]
        session.first_crack_at_utc = None
        session.first_crack_monotonic_seconds = None
        session.first_crack_source = None
        session.phase = "roasting"
    reads = _count_reads(monkeypatch, driver)
    original = driver.read_state

    def fault_with_pending_drop() -> RoasterState:
        reservation = store.reserve_driver_command(session, kind="drop")
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            session.faulted_at_utc = datetime(2026, 1, 1, tzinfo=UTC)
            assert session.active is True
            assert session.pending_driver_command_token == reservation.token
            assert session.pending_driver_command_kind == "drop"
        return original()

    monkeypatch.setattr(driver, "read_state", fault_with_pending_drop)
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="refused_identity",
        reason="session_changed_during_read",
        driver_read="invoked",
    )
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_handles_pending_and_completed_drop_at_the_right_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G6/G14: pending Drop refuses; a completed active Drop is assembled live."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    reads = _count_reads(monkeypatch, driver)
    pending = store.reserve_driver_command(session, kind="drop")
    precheck = _run(context, session_id)
    _assert_refusal(
        precheck, outcome="refused_busy", reason="drop_in_progress", driver_read="not_invoked"
    )
    assert reads == [0]
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        store._clear_driver_command_reservation_locked(session, pending)  # pyright: ignore[reportPrivateUsage]
    original = driver.read_state

    def complete_drop() -> RoasterState:
        reservation = store.reserve_driver_drop(session).reservation
        assert reservation is not None
        store.complete_reserved_driver_drop_snapshot(
            session,
            reservation=reservation,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=True,
        )
        return original()

    monkeypatch.setattr(driver, "read_state", complete_drop)
    completed = _run(context, session_id)
    assert completed.outcome == "evidence"
    assert completed.record is not None
    assert completed.record.drop is not None
    assert completed.record.phase in {"dropped", "cooling"}
    assert reads == [1]


def test_timing_evidence_rechecks_pending_drop_and_first_crack_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G6/G14: live assembly sees a new reservation or a new canonical FC event."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.event_timeline = [
            event for event in session.event_timeline if event.kind != "first_crack_detected"
        ]
        session.first_crack_at_utc = None
        session.first_crack_monotonic_seconds = None
        session.first_crack_source = None
        session.phase = "roasting"
    reads = _count_reads(monkeypatch, driver)
    original = driver.read_state

    def reserve_drop() -> RoasterState:
        assert session is not None
        store.reserve_driver_command(session, kind="drop")
        return original()

    monkeypatch.setattr(driver, "read_state", reserve_drop)
    pending = _run(context, session_id)
    _assert_refusal(
        pending, outcome="refused_busy", reason="drop_in_progress", driver_read="invoked"
    )
    assert reads == [1]

    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.pending_driver_command_kind = None
        session.pending_driver_command_token = None
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.event_timeline = [
            event for event in session.event_timeline if event.kind != "first_crack_detected"
        ]
        session.first_crack_at_utc = None
        session.first_crack_monotonic_seconds = None
        session.first_crack_source = None
        session.phase = "roasting"
    reads = _count_reads(monkeypatch, driver)
    original = driver.read_state

    def record_fc() -> RoasterState:
        store.record_event(session, "first_crack_detected", provenance="manual_mark")
        return original()

    monkeypatch.setattr(driver, "read_state", record_fc)
    evidence = _run(context, session_id)
    assert evidence.outcome == "evidence"
    assert evidence.record is not None
    assert evidence.record.first_crack is not None
    assert evidence.record.first_crack.source == "manual_mark"
    assert reads == [1]


def test_timing_evidence_refuses_drop_then_stop_during_read_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G6: a completed Drop followed by a stop is identity refusal, never evidence."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    reads = _count_reads(monkeypatch, driver)
    original = driver.read_state

    def complete_and_stop() -> RoasterState:
        reservation = store.reserve_driver_drop(session).reservation
        assert reservation is not None
        store.complete_reserved_driver_drop_snapshot(
            session,
            reservation=reservation,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=True,
        )
        store.stop_cooling(session)
        return original()

    monkeypatch.setattr(driver, "read_state", complete_and_stop)
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="refused_identity",
        reason="session_changed_during_read",
        driver_read="invoked",
    )
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_finalisation_and_active_complete_state_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalisation is pre-read busy; an impossible active complete state cannot leak a record."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    reads = _count_reads(monkeypatch, driver)
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        store._nonterminal_finalisations.add(session.id)  # pyright: ignore[reportPrivateUsage]
    busy = _run(context, session_id)
    _assert_refusal(
        busy,
        outcome="refused_busy",
        reason="finalisation_in_progress",
        driver_read="not_invoked",
    )
    assert reads == [0]
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        store._nonterminal_finalisations.clear()  # pyright: ignore[reportPrivateUsage]
        session.phase = "complete"  # type: ignore[assignment]
    invalid = _run(context, session_id)
    _assert_refusal(invalid, outcome="unavailable", reason="record_invalid", driver_read="invoked")
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_exposes_auto_t0_and_model_fc_writer_provenance() -> None:
    """Real auto-T0 and model writers preserve their sources and canonical bases."""
    clock = iter((10.0, 11.0, 12.0, 13.0, 14.0, 15.0)).__next__
    store = RoastSessionStore(monotonic_now=clock, session_id_factory=lambda: "writer-session")
    session = store.start_session()
    first_t0, first_snapshot = store.process_auto_t0_reading_snapshot(
        session, bean_temp_c=200.0, drop_threshold_c=5.0
    )
    auto_t0, auto_snapshot = store.process_auto_t0_reading_snapshot(
        session, bean_temp_c=194.0, drop_threshold_c=5.0
    )
    assert first_t0 is None
    assert first_snapshot.beans_added_source is None
    assert auto_t0 is not None
    assert auto_snapshot.beans_added_source == "auto_t0"
    assert auto_snapshot.beans_added_at_utc is not None
    model_fc, model_snapshot = store.record_first_crack_detection_snapshot(
        session,
        detected_at_monotonic_seconds=12.25,
        max_future_seconds=None,
    )
    manual_fc = store.record_event(session, "first_crack_detected", provenance="manual_mark")
    assert manual_fc is model_fc
    assert model_snapshot.first_crack_source == "model_detection"
    assert model_snapshot.first_crack_at_utc is not None
    driver = MockRoasterDriver()
    driver.connect()
    context = cast(
        mcp_server.ServerContext,
        SimpleNamespace(
            session_store=store,
            roaster_driver=driver,
            config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
        ),
    )

    result = _run(context, session.id)

    assert result.record is not None
    assert result.record.charge is not None
    assert result.record.first_crack is not None
    assert result.record.charge.source == "auto_t0"
    assert result.record.charge.time_basis == "producer_canonical_record"
    assert result.record.charge.canonical_seconds.hex() == (1.0).hex()
    assert result.record.first_crack.source == "model_detection"
    assert result.record.first_crack.time_basis == "detector_clock_rebased"
    assert result.record.first_crack.canonical_seconds.hex() == (2.25).hex()
    snapshot = store.get_session_snapshot(session_id=session.id)
    assert snapshot.beans_added_source == "auto_t0"
    assert snapshot.first_crack_source == "model_detection"


def test_timing_evidence_bare_cooling_stop_active_complete_is_record_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare lifecycle event can make active complete and cannot yield evidence."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    store.record_event(session, "beans_dropped")
    store.record_event(session, "cooling_started")
    store.record_event(session, "cooling_stopped")
    assert session.active is True
    assert session.phase == "complete"
    reads = _count_reads(monkeypatch, driver)

    result = _run(context, session_id)

    _assert_refusal(result, outcome="unavailable", reason="record_invalid", driver_read="invoked")
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


@pytest.mark.parametrize(
    ("clock_values", "driver_read"),
    [
        ((1, 101.0), "not_invoked"),
        ((math.nan, 101.0), "not_invoked"),
        ((math.inf, 101.0), "not_invoked"),
        ((-math.inf, 101.0), "not_invoked"),
        ((100.0, 1), "invoked"),
        ((100.0, math.nan), "invoked"),
        ((100.0, math.inf), "invoked"),
        ((100.0, -math.inf), "invoked"),
    ],
)
def test_timing_evidence_rejects_exact_invalid_t0_and_t1_values(
    monkeypatch: pytest.MonkeyPatch,
    clock_values: tuple[object, object],
    driver_read: str,
) -> None:
    """Raw clock typing and finiteness are checked at both causal positions."""
    context, driver, session_id = _context()
    values = iter(clock_values)
    monkeypatch.setattr(context.session_store, "read_timing_evidence_clock", lambda: next(values))
    reads = _count_reads(monkeypatch, driver)

    result = _run(context, session_id)

    _assert_refusal(result, outcome="unavailable", reason="clock_invalid", driver_read=driver_read)
    assert reads == [0 if driver_read == "not_invoked" else 1]


@pytest.mark.parametrize("position", ["t0", "t1"])
def test_timing_evidence_rejects_clock_exceptions_at_both_positions(
    monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    """Clock exceptions are closed before or after exactly one driver read."""
    context, driver, session_id = _context()
    reads = _count_reads(monkeypatch, driver)
    calls = [0]

    def clock() -> float:
        calls[0] += 1
        if calls[0] == (1 if position == "t0" else 2):
            raise RuntimeError("test clock failure")
        return 101.0

    monkeypatch.setattr(context.session_store, "read_timing_evidence_clock", clock)
    result = _run(context, session_id)
    _assert_refusal(
        result,
        outcome="unavailable",
        reason="clock_invalid",
        driver_read="not_invoked" if position == "t0" else "invoked",
    )
    assert reads == [0 if position == "t0" else 1]


def test_timing_evidence_rejects_nonfinite_derived_bracket_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite raw clocks cannot make a non-finite session-relative bracket valid."""
    context, driver, session_id = _context()
    session = context.session_store.get_latest_session()
    assert session is not None
    session.monotonic_start = math.inf
    monkeypatch.setattr(
        context.session_store, "read_timing_evidence_clock", iter((100.0, 101.0)).__next__
    )
    reads = _count_reads(monkeypatch, driver)

    result = _run(context, session_id)

    _assert_refusal(result, outcome="unavailable", reason="clock_invalid", driver_read="invoked")
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_driver_exception_precedes_post_read_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised driver result wins over replacement that happened during that read."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    reads = [0]

    def replace_then_raise() -> RoasterState:
        reads[0] += 1
        store.stop_session()
        store.start_session()
        raise RuntimeError("replacement happened before driver failure")

    monkeypatch.setattr(driver, "read_state", replace_then_raise)
    result = _run(context, session_id)

    _assert_refusal(result, outcome="unavailable", reason="driver_raised", driver_read="invoked")
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


def test_timing_evidence_post_read_finalisation_refuses_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalisation created during a read is a post-read busy refusal."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    reads = _count_reads(monkeypatch, driver)
    original_read = driver.read_state

    def finalise_during_read() -> RoasterState:
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            store._nonterminal_finalisations.add(session.id)  # pyright: ignore[reportPrivateUsage]
        return original_read()

    monkeypatch.setattr(driver, "read_state", finalise_during_read)
    result = _run(context, session_id)

    _assert_refusal(
        result,
        outcome="refused_busy",
        reason="finalisation_in_progress",
        driver_read="invoked",
    )
    assert reads == [1]
    assert session.timing_evidence_sequence == 0


@pytest.mark.parametrize(
    ("clock_values", "reason"),
    [((100.0, 99.0), "clock_regression"), ((100.0, 101.0), "clock_regression")],
)
def test_timing_evidence_rejects_reversed_or_lower_than_start_clock(
    monkeypatch: pytest.MonkeyPatch,
    clock_values: tuple[float, float],
    reason: str,
) -> None:
    """G7: raw clocks are checked before clamping or assembly."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    if clock_values[1] > clock_values[0]:
        session.monotonic_start = 200.0
    values = iter(clock_values)
    monkeypatch.setattr(store, "read_timing_evidence_clock", lambda: next(values))
    reads = _count_reads(monkeypatch, driver)
    result = _run(context, session_id)
    _assert_refusal(result, outcome="unavailable", reason=reason, driver_read="invoked")
    assert reads == [1]


def _reverse_assembly_results(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_bracket: tuple[float, float],
    second_bracket: tuple[float, float],
) -> tuple[mcp_server.TimingEvidenceResult, mcp_server.TimingEvidenceResult]:
    """Run two calls with a controlled reverse assembly order and no sleeps."""
    initial_clock = iter((0.0,)).__next__
    store = RoastSessionStore(monotonic_now=initial_clock, session_id_factory=lambda: "race")
    session = store.start_session()
    driver = MockRoasterDriver()
    driver.connect()
    context = cast(
        mcp_server.ServerContext,
        SimpleNamespace(
            session_store=store,
            roaster_driver=driver,
            config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
        ),
    )
    first_clock = iter(first_bracket)
    second_clock = iter(second_bracket)
    monkeypatch.setattr(
        store,
        "read_timing_evidence_clock",
        lambda: (
            next(first_clock)
            if threading.current_thread().name == "timing-first"
            else next(second_clock)
        ),
    )
    first_read_entered = threading.Event()
    release_first = threading.Event()
    original = driver.read_state

    def ordered_read() -> RoasterState:
        if threading.current_thread().name == "timing-first":
            first_read_entered.set()
            assert release_first.wait(2)
        return original()

    monkeypatch.setattr(driver, "read_state", ordered_read)
    first_result: list[mcp_server.TimingEvidenceResult] = []
    thread = threading.Thread(
        target=lambda: first_result.append(_run(context, session.id)), name="timing-first"
    )
    thread.start()
    assert first_read_entered.wait(2)
    second_result = _run(context, session.id)
    release_first.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(first_result) == 1
    return first_result[0], second_result


@pytest.mark.parametrize(
    ("first_bracket", "second_bracket"),
    [((10.0, 40.0), (20.0, 30.0)), ((30.0, 35.0), (20.0, 40.0))],
)
def test_timing_evidence_rejects_distinct_reverse_assembly_endpoint_regressions(
    monkeypatch: pytest.MonkeyPatch,
    first_bracket: tuple[float, float],
    second_bracket: tuple[float, float],
) -> None:
    """G11: lo-only and hi-only regressions have separate threaded witnesses."""
    first, second = _reverse_assembly_results(
        monkeypatch, first_bracket=first_bracket, second_bracket=second_bracket
    )
    assert second.outcome == "evidence"
    _assert_refusal(first, outcome="unavailable", reason="out_of_order_read", driver_read="invoked")


def test_timing_evidence_does_not_hold_store_or_lifecycle_lock_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second thread can acquire both unrelated locks while the driver is blocked."""
    context, driver, session_id = _context()
    object.__setattr__(context, "lifecycle_barrier", threading.Lock())
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()
    original = driver.read_state

    def blocked() -> RoasterState:
        entered.set()
        assert release.wait(2)
        return original()

    def probe() -> None:
        assert entered.wait(2)
        with context.session_store._lock:  # pyright: ignore[reportPrivateUsage]
            assert context.lifecycle_barrier.acquire(timeout=1)
            context.lifecycle_barrier.release()
        acquired.set()
        release.set()

    monkeypatch.setattr(driver, "read_state", blocked)
    worker = threading.Thread(target=probe)
    worker.start()
    result = _run(context, session_id)
    worker.join(2)
    assert acquired.is_set()
    assert result.outcome == "evidence"


def test_timing_evidence_real_stdio_schema_and_malformed_requests(
    tmp_path: Path,
) -> None:
    """A patched child preserves strict parsing and exact values on the stdio wire."""
    injection_root = tmp_path / "child_injection"
    injection_root.mkdir()
    read_counter = tmp_path / "child-read-count.txt"
    imported_module = tmp_path / "child-mcp-server-path.txt"
    injection_root.joinpath("sitecustomize.py").write_text(
        "\n".join(
            (
                "import functools",
                "import os",
                "from pathlib import Path",
                "import coffee_roaster_mcp.mcp_server as mcp_server",
                "from coffee_roaster_mcp.drivers import MockRoasterDriver",
                "from coffee_roaster_mcp.session import RoastSessionStore",
                "Path(os.environ['P1_WIRE_IMPORTED_MODULE']).write_text(mcp_server.__file__)",
                "Path(os.environ['P1_WIRE_READ_COUNTER']).write_text('0')",
                "clock_values = iter((100.0, 100.30000000000004, 100.6000000000001, 100.7, 100.8))",
                "mcp_server.RoastSessionStore = functools.partial(",
                "    RoastSessionStore, monotonic_now=lambda: next(clock_values)",
                ")",
                "driver = MockRoasterDriver()",
                "driver.connect()",
                "original_read = driver.read_state",
                "read_count = 0",
                "def counted_read():",
                "    global read_count",
                "    read_count += 1",
                "    Path(os.environ['P1_WIRE_READ_COUNTER']).write_text(str(read_count))",
                "    if read_count == 2:",
                "        raise RuntimeError('P1-STDIO-SENTINEL-DO-NOT-LEAK')",
                "    state = original_read()",
                "    object.__setattr__(state, 'bean_temp_c', 196.00000000000003)",
                "    object.__setattr__(state, 'env_temp_c', 0.30000000000000004)",
                "    return state",
                "driver.read_state = counted_read",
                "mcp_server.create_roaster_driver = lambda *args, **kwargs: driver",
            )
        ),
        encoding="utf-8",
    )
    mutated_source = os.environ.get("P1_WIRE_MUTANT_SOURCE")
    python_path = os.pathsep.join(
        path for path in (str(injection_root), mutated_source) if path is not None
    )

    async def exercise() -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": python_path,
            "P1_WIRE_IMPORTED_MODULE": str(imported_module),
            "P1_WIRE_READ_COUNTER": str(read_counter),
        }
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "coffee_roaster_mcp.cli", "serve"],
            cwd=tmp_path,
            env=environment,
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            tool = next(item for item in tools.tools if item.name == "get_timing_evidence")
            assert tool.inputSchema == {
                "additionalProperties": False,
                "description": "Strict raw SDK-boundary arguments for synthetic timing evidence.",
                "properties": {
                    "expected_session_id": {
                        "maxLength": 128,
                        "minLength": 1,
                        "title": "Expected Session Id",
                        "type": "string",
                    },
                    "evidence_protocol": {
                        "const": "timing_evidence/v1",
                        "title": "Evidence Protocol",
                        "type": "string",
                    },
                },
                "required": ["expected_session_id", "evidence_protocol"],
                "title": "_TimingEvidenceArguments",
                "type": "object",
            }
            assert tool.outputSchema is not None
            output_schema = tool.outputSchema
            canonical_schema = json.dumps(
                output_schema, sort_keys=True, separators=(",", ":")
            ).encode()
            assert hashlib.sha256(canonical_schema).hexdigest() == (
                "aa7543a7d5644e18753cea66e26e26137b86aba17b5be646df226c3c688e7cf6"
            )
            assert output_schema["additionalProperties"] is False
            assert output_schema["properties"]["evidence_protocol"]["const"] == "timing_evidence/v1"
            assert output_schema["properties"]["evidence_version"]["const"] == 1
            assert output_schema["$defs"]["TimingEvidenceRecord"]["additionalProperties"] is False
            assert output_schema["$defs"]["TimingClock"]["properties"]["domain"]["const"] == (
                "session_relative_store_monotonic"
            )
            valid_channel = output_schema["$defs"]["ValidChannelEvidence"]
            assert valid_channel["additionalProperties"] is False
            assert valid_channel["properties"]["status"]["const"] == "valid"
            assert (
                valid_channel["properties"]["qualification"]["const"] == "synthetic_software_only"
            )
            record = output_schema["$defs"]["TimingEvidenceRecord"]
            assert record["properties"]["bean"]["discriminator"] == {
                "propertyName": "status",
                "mapping": {
                    "invalid": "#/$defs/InvalidChannelEvidence",
                    "unavailable": "#/$defs/UnavailableChannelEvidence",
                    "valid": "#/$defs/ValidChannelEvidence",
                },
            }
            event = output_schema["$defs"]["EventEvidence"]
            assert event["properties"]["physical_time_bound"]["const"] == "not_provided"
            assert event["properties"]["time_basis"]["enum"] == [
                "producer_canonical_record",
                "detector_clock_rebased",
            ]
            assert event["allOf"] == [
                {
                    "if": {"properties": {"source": {"const": "model_detection"}}},
                    "then": {"properties": {"time_basis": {"const": "detector_clock_rebased"}}},
                    "else": {"properties": {"time_basis": {"const": "producer_canonical_record"}}},
                },
                {
                    "if": {"properties": {"kind": {"const": "beans_added"}}},
                    "then": {
                        "properties": {
                            "source": {"enum": ["manual_mark", "auto_t0", "unattributed"]}
                        }
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "first_crack_detected"}}},
                    "then": {
                        "properties": {
                            "source": {"enum": ["manual_mark", "model_detection", "unattributed"]}
                        }
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "beans_dropped"}}},
                    "then": {"properties": {"source": {"const": "unattributed"}}},
                },
            ]
            schema_validator: Any = Draft202012Validator(output_schema)
            schema_record: dict[str, object] = {
                "session_id": "schema-session",
                "session_ownership_sequence": 1,
                "driver": "mock",
                "clock": {
                    "epoch_id": "0" * 32,
                    "domain": "session_relative_store_monotonic",
                    "suspend_behaviour": "unqualified",
                },
                "evidence_sequence": 1,
                "phase": "development",
                "charge": {
                    "kind": "beans_added",
                    "event_index": 0,
                    "canonical_seconds": 1.0,
                    "clock_epoch_id": "0" * 32,
                    "source": "manual_mark",
                    "time_basis": "producer_canonical_record",
                    "physical_time_bound": "not_provided",
                },
                "first_crack": {
                    "kind": "first_crack_detected",
                    "event_index": 1,
                    "canonical_seconds": 2.0,
                    "clock_epoch_id": "0" * 32,
                    "source": "model_detection",
                    "time_basis": "detector_clock_rebased",
                    "physical_time_bound": "not_provided",
                },
                "drop": None,
                "bean": {
                    "status": "unavailable",
                    "reason": "reported_none",
                },
                "environment": {
                    "status": "unavailable",
                    "reason": "reported_none",
                },
                "time_basis": "canonical_operational_session_relative",
                "physical_qualification": "none_synthetic_mock_only",
            }
            schema_result: dict[str, object] = {
                "evidence_protocol": "timing_evidence/v1",
                "evidence_version": 1,
                "outcome": "evidence",
                "reason_code": None,
                "driver_read": "invoked",
                "requested_session_id": "schema-session",
                "record": schema_record,
            }
            schema_validator.validate(schema_result)
            schema_charge = cast(dict[str, object], schema_record["charge"])
            schema_first_crack = cast(dict[str, object], schema_record["first_crack"])
            invalid_schema_cases: list[
                tuple[Literal["charge", "first_crack", "drop"], dict[str, object], str]
            ] = [
                (
                    "first_crack",
                    schema_first_crack | {"time_basis": "producer_canonical_record"},
                    "development",
                ),
                (
                    "charge",
                    {
                        **schema_charge,
                        "source": "model_detection",
                        "time_basis": "detector_clock_rebased",
                    },
                    "development",
                ),
                (
                    "first_crack",
                    {
                        **schema_first_crack,
                        "source": "auto_t0",
                        "time_basis": "producer_canonical_record",
                    },
                    "development",
                ),
                (
                    "drop",
                    {
                        "kind": "beans_dropped",
                        "event_index": 2,
                        "canonical_seconds": 3.0,
                        "clock_epoch_id": "0" * 32,
                        "source": "manual_mark",
                        "time_basis": "producer_canonical_record",
                        "physical_time_bound": "not_provided",
                    },
                    "dropped",
                ),
            ]
            for index, (slot, invalid_event, phase) in enumerate(invalid_schema_cases):
                invalid_record = dict(schema_record)
                invalid_record[slot] = invalid_event
                invalid_record["phase"] = phase
                invalid_result = {**schema_result, "record": invalid_record}
                with pytest.raises(ValidationError):
                    schema_validator.validate(invalid_result)
                mutated_event_schema = dict(event)
                mutated_event_schema["allOf"] = [
                    condition
                    for conditional_index, condition in enumerate(event["allOf"])
                    if conditional_index != index
                ]
                mutated_output_schema = dict(output_schema)
                mutated_definitions = dict(output_schema["$defs"])
                mutated_definitions["EventEvidence"] = mutated_event_schema
                mutated_output_schema["$defs"] = mutated_definitions
                mutated_schema_validator: Any = Draft202012Validator(mutated_output_schema)
                mutated_schema_validator.validate(invalid_result)
            started = await client.call_tool("start_roast_session", {})
            assert started.structuredContent is not None
            started_content = cast(Mapping[str, object], started.structuredContent)
            session = cast(Mapping[str, object], started_content["session"])
            session_id = cast(str, session["session_id"])
            read_baseline = read_counter.read_text(encoding="utf-8")
            malformed = [
                {"expected_session_id": session_id, "evidence_protocol": "wrong"},
                {"expected_session_id": 1, "evidence_protocol": "timing_evidence/v1"},
                {"expected_session_id": "", "evidence_protocol": "timing_evidence/v1"},
                {
                    "expected_session_id": "x" * 129,
                    "evidence_protocol": "timing_evidence/v1",
                },
                {
                    "expected_session_id": session_id,
                    "evidence_protocol": "timing_evidence/v1",
                    "extra": "blocked",
                },
            ]
            for arguments in malformed:
                reply = await client.call_tool("get_timing_evidence", arguments)
                assert reply.isError is True
                assert read_counter.read_text(encoding="utf-8") == read_baseline

            reply = await client.call_tool(
                "get_timing_evidence",
                {"expected_session_id": session_id, "evidence_protocol": "timing_evidence/v1"},
            )
            assert reply.isError is False
            assert reply.structuredContent is not None
            assert set(reply.structuredContent) == {
                "evidence_protocol",
                "evidence_version",
                "outcome",
                "reason_code",
                "driver_read",
                "requested_session_id",
                "record",
            }
            assert reply.structuredContent["record"] is not None
            record_content = cast(Mapping[str, object], reply.structuredContent["record"])
            assert set(record_content) == {
                "session_id",
                "session_ownership_sequence",
                "driver",
                "clock",
                "evidence_sequence",
                "phase",
                "charge",
                "first_crack",
                "drop",
                "bean",
                "environment",
                "time_basis",
                "physical_qualification",
            }
            assert read_counter.read_text(encoding="utf-8") == "1"
            bean = cast(Mapping[str, object], record_content["bean"])
            environment_channel = cast(Mapping[str, object], record_content["environment"])
            assert cast(float, bean["celsius"]).hex() == (196.00000000000003).hex()
            assert cast(float, environment_channel["celsius"]).hex() == (0.30000000000000004).hex()
            assert (
                cast(float, bean["acquisition_lo_seconds"]).hex()
                == (100.30000000000004 - 100.0).hex()
            )
            assert (
                cast(float, bean["acquisition_hi_seconds"]).hex()
                == (100.6000000000001 - 100.0).hex()
            )
            text_content = next(
                content.text for content in reply.content if isinstance(content, TextContent)
            )
            text_record = cast(Mapping[str, object], json.loads(text_content)["record"])
            text_bean = cast(Mapping[str, object], text_record["bean"])
            text_environment = cast(Mapping[str, object], text_record["environment"])
            assert cast(float, text_bean["celsius"]).hex() == cast(float, bean["celsius"]).hex()
            assert (
                cast(float, text_environment["celsius"]).hex()
                == cast(float, environment_channel["celsius"]).hex()
            )
            assert (
                cast(float, text_bean["acquisition_lo_seconds"]).hex()
                == cast(float, bean["acquisition_lo_seconds"]).hex()
            )
            assert (
                cast(float, text_bean["acquisition_hi_seconds"]).hex()
                == cast(float, bean["acquisition_hi_seconds"]).hex()
            )

            sentinel_reply = await client.call_tool(
                "get_timing_evidence",
                {"expected_session_id": session_id, "evidence_protocol": "timing_evidence/v1"},
            )
            assert sentinel_reply.isError is False
            sentinel_text = "".join(
                content.text
                for content in sentinel_reply.content
                if isinstance(content, TextContent)
            )
            assert "P1-STDIO-SENTINEL-DO-NOT-LEAK" not in sentinel_text
            assert read_counter.read_text(encoding="utf-8") == "2"

    asyncio.run(exercise())
    assert imported_module.read_text(encoding="utf-8").endswith("coffee_roaster_mcp/mcp_server.py")
    if mutated_source is not None:
        assert Path(imported_module.read_text(encoding="utf-8")).is_relative_to(mutated_source)


def _context_without_fc(
    clock_values: tuple[float, ...],
) -> tuple[mcp_server.ServerContext, MockRoasterDriver, str]:
    """Create a roast with charge only and a fully test-owned clock stream."""
    clock = iter(clock_values).__next__
    store = RoastSessionStore(monotonic_now=clock, session_id_factory=lambda: "boundary-session")
    session = store.start_session()
    store.record_event(session, "beans_added", provenance="manual_mark")
    driver = MockRoasterDriver()
    driver.connect()
    return (
        cast(
            mcp_server.ServerContext,
            SimpleNamespace(
                session_store=store,
                roaster_driver=driver,
                config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
            ),
        ),
        driver,
        session.id,
    )


def test_timing_evidence_keeps_fc_at_its_actual_read_or_assembly_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G13a/G14: FC written in-read differs from FC written after t1, bit-for-bit.

    A disposable ``round(event.monotonic_seconds, 6)`` mutation in the event
    evidence builder makes the first raw-hex assertion fail.
    """
    in_read, in_driver, in_id = _context_without_fc(
        (100.0, 100.1, 100.30000000000004, 100.6000000000001, 100.8000000000001)
    )
    in_store = in_read.session_store
    in_session = in_store.get_latest_session()
    assert in_session is not None
    original_read = in_driver.read_state

    def record_inside_read() -> RoasterState:
        event = in_store.record_event(in_session, "first_crack_detected", provenance="manual_mark")
        assert event.monotonic_seconds.hex() == (100.6000000000001 - 100.0).hex()
        return original_read()

    monkeypatch.setattr(in_driver, "read_state", record_inside_read)
    inside = _run(in_read, in_id)
    assert inside.record is not None and inside.record.first_crack is not None
    inside_fc = inside.record.first_crack.canonical_seconds
    inside_lo = inside.record.bean.acquisition_lo_seconds  # type: ignore[union-attr]
    inside_hi = inside.record.bean.acquisition_hi_seconds  # type: ignore[union-attr]
    assert inside_fc.hex() == (100.6000000000001 - 100.0).hex()
    assert inside_lo <= inside_fc <= inside_hi

    after, _, after_id = _context_without_fc(
        (100.0, 100.1, 100.30000000000004, 100.8000000000001, 100.9000000000001)
    )
    after_store = after.session_store
    after_session = after_store.get_latest_session()
    assert after_session is not None
    original_assemble = after_store.assemble_timing_evidence

    def assemble_after_t1(*args: object, **kwargs: object) -> object:
        event = after_store.record_event(
            after_session, "first_crack_detected", provenance="manual_mark"
        )
        assert event.monotonic_seconds.hex() == (100.9000000000001 - 100.0).hex()
        return original_assemble(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(after_store, "assemble_timing_evidence", assemble_after_t1)
    later = _run(after, after_id)
    assert later.record is not None and later.record.first_crack is not None
    assert later.record.first_crack.canonical_seconds.hex() == (100.9000000000001 - 100.0).hex()
    assert later.record.first_crack.canonical_seconds > later.record.bean.acquisition_hi_seconds  # type: ignore[union-attr]


@pytest.mark.parametrize("pending_kind", ["control", "heat_containment"])
def test_timing_evidence_non_drop_reservations_remain_pending_and_visible_at_assembly(
    monkeypatch: pytest.MonkeyPatch, pending_kind: str
) -> None:
    """Only Drop is busy: other exact reservations survive an evidence read."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    before_phase = session.phase
    original = driver.read_state
    reservation: list[object] = []

    def reserve_other() -> RoasterState:
        reservation.append(store.reserve_driver_command(session, kind=cast(Any, pending_kind)))
        return original()

    monkeypatch.setattr(driver, "read_state", reserve_other)
    result = _run(context, session_id)
    assert result.record is not None
    assert result.record.drop is None
    assert result.record.evidence_sequence == 1
    assert len(reservation) == 1
    assert session.pending_driver_command_kind == pending_kind
    assert session.pending_driver_command_token == reservation[0].token  # type: ignore[union-attr]
    assert session.phase == before_phase


def test_timing_evidence_cleared_drop_reservation_has_no_drop_event_or_public_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Drop reservation cleared during read is not mistaken for a completed Drop."""
    context, driver, session_id = _context()
    store = context.session_store
    session = store.get_latest_session()
    assert session is not None
    original = driver.read_state
    reservation: list[object] = []
    before = (session.phase, list(session.event_timeline), session.pending_driver_command_token)

    def reserve_then_clear() -> RoasterState:
        item = store.reserve_driver_command(session, kind="drop")
        reservation.append(item)
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            store._clear_driver_command_reservation_locked(session, item)  # pyright: ignore[reportPrivateUsage]
        return original()

    monkeypatch.setattr(driver, "read_state", reserve_then_clear)
    result = _run(context, session_id)
    assert result.record is not None and result.record.drop is None
    assert result.record.evidence_sequence == 1
    assert len(reservation) == 1
    assert session.pending_driver_command_token is None
    assert session.pending_driver_command_kind is None
    assert (session.phase, list(session.event_timeline), before[2]) == before


def test_timing_evidence_provenance_first_writer_and_payload_are_store_owned() -> None:
    """Writer ordering, not an arbitrary payload key, determines evidence provenance."""
    manual, _, manual_id = _context_without_fc((10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0))
    manual_store = manual.session_store
    manual_session = manual_store.get_latest_session()
    assert manual_session is not None
    manual_store.process_auto_t0_reading_snapshot(
        manual_session, bean_temp_c=200.0, drop_threshold_c=5.0
    )
    auto_attempt, _ = manual_store.process_auto_t0_reading_snapshot(
        manual_session, bean_temp_c=194.0, drop_threshold_c=5.0
    )
    assert auto_attempt is not None
    manual_store.record_event(manual_session, "first_crack_detected", provenance="manual_mark")
    model_attempt, copied = manual_store.record_first_crack_detection_snapshot(
        manual_session, detected_at_monotonic_seconds=13.0, max_future_seconds=None
    )
    result = _run(manual, manual_id)
    assert result.record is not None
    assert result.record.charge is not None and result.record.charge.source == "manual_mark"
    assert (
        result.record.first_crack is not None and result.record.first_crack.source == "manual_mark"
    )
    assert model_attempt is manual_session.event_timeline[-1]
    assert copied.first_crack_source == "manual_mark"

    for payload_source in ("operator", "auto_t0"):
        bare, _, bare_id = _context_without_fc((10.0, 11.0, 12.0, 13.0, 14.0))
        bare_store = bare.session_store
        bare_session = bare_store.get_latest_session()
        assert bare_session is not None
        bare_store.record_event(
            bare_session, "first_crack_detected", payload={"source": payload_source}
        )
        bare_result = _run(bare, bare_id)
        assert bare_result.record is not None and bare_result.record.first_crack is not None
        assert bare_result.record.first_crack.source == "unattributed"
        assert bare_result.record.first_crack.canonical_seconds.hex() == (
            bare_session.first_crack_monotonic_seconds.hex()  # type: ignore[union-attr]
        )


@pytest.mark.parametrize(
    ("purpose", "faulted", "stopped", "reason"),
    [
        ("cold_characterisation", False, False, "session_purpose_not_eligible"),
        ("roast", True, True, "session_faulted"),
        ("roast", False, True, "session_not_active"),
    ],
)
def test_timing_evidence_identity_rows_have_exact_reason_and_zero_reads(
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    faulted: bool,
    stopped: bool,
    reason: str,
) -> None:
    """Eligible purpose and stopped/faulted identity rows are pre-read refusals."""
    context, driver, _ = _context()
    store = context.session_store
    store.stop_session()
    session = store.start_session(purpose=cast(Any, purpose))
    if faulted:
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            session.faulted_at_utc = datetime(2026, 1, 1, tzinfo=UTC)
            session.phase = "fault"
    if stopped:
        with store._lock:  # pyright: ignore[reportPrivateUsage]
            session.stopped_at_utc = datetime(2026, 1, 1, tzinfo=UTC)
            session.monotonic_stop = 1.0
    reads = _count_reads(monkeypatch, driver)
    result = _run(context, session.id)
    _assert_refusal(result, outcome="refused_identity", reason=reason, driver_read="not_invoked")
    assert reads == [0]


def test_timing_evidence_invalid_assembly_keeps_both_endpoints_then_advances_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed validation commits neither endpoint; valid records remain consecutive."""
    context, _, session_id = _context()
    session = context.session_store.get_latest_session()
    assert session is not None
    first = _run(context, session_id)
    assert first.record is not None
    committed = (
        session.timing_evidence_sequence,
        session.timing_evidence_last_lo,
        session.timing_evidence_last_hi,
    )
    original = mcp_server._timing_evidence_result  # pyright: ignore[reportPrivateUsage]

    def reject_evidence(**kwargs: object) -> mcp_server.TimingEvidenceResult:
        if kwargs["outcome"] == "evidence":
            raise ValueError("test invalid assembly")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_server, "_timing_evidence_result", reject_evidence)
    failed = _run(context, session_id)
    _assert_refusal(failed, outcome="unavailable", reason="record_invalid", driver_read="invoked")
    assert (
        session.timing_evidence_sequence,
        session.timing_evidence_last_lo,
        session.timing_evidence_last_hi,
    ) == committed
    monkeypatch.undo()
    second = _run(context, session_id)
    assert second.record is not None
    assert (first.record.evidence_sequence, second.record.evidence_sequence) == (1, 2)


@pytest.mark.parametrize("mutation", ["slot", "phase", "wire_integer", "epoch"])
def test_timing_evidence_record_model_rejects_direct_cross_boundary_mutations(
    mutation: str,
) -> None:
    """Direct model validation closes slot, phase, safe-integer and epoch rows."""
    context, _, session_id = _context()
    result = _run(context, session_id)
    assert result.record is not None
    row = result.record.model_dump()
    if mutation == "slot":
        charge = cast(dict[str, object], row["charge"])
        charge["kind"] = "first_crack_detected"
    elif mutation == "phase":
        row["phase"] = "pre_roast"
    elif mutation == "wire_integer":
        row["evidence_sequence"] = 2**53
    else:
        clock = cast(dict[str, object], row["clock"])
        clock["epoch_id"] = "f" * 32
    with pytest.raises((TypeError, ValueError)):
        mcp_server.TimingEvidenceRecord.model_validate(row)


@pytest.mark.parametrize(
    ("outcome", "expected_read"),
    [("pre_read", "not_invoked"), ("post_read", "invoked"), ("evidence", "invoked")],
)
def test_timing_evidence_outcomes_do_not_run_controls_or_append_test_owned_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    expected_read: str,
) -> None:
    """§10.8: refusal and evidence leave non-bracket session machinery untouched."""
    ticks = iter((10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0)).__next__
    store = RoastSessionStore(
        monotonic_now=ticks,
        session_id_factory=lambda: "side-effects",
        default_log_dir=tmp_path,
    )
    session = store.start_session()
    store.record_event(session, "beans_added", provenance="manual_mark")
    driver = MockRoasterDriver()
    driver.connect()
    context = cast(
        mcp_server.ServerContext,
        SimpleNamespace(
            session_store=store,
            roaster_driver=driver,
            config=SimpleNamespace(roaster=SimpleNamespace(driver="mock")),
        ),
    )
    log_path = session.log_writer.log_dir / "roast.jsonl"  # type: ignore[union-attr]
    before = (
        log_path.read_bytes(),
        list(session.telemetry_buffer),
        list(session.event_timeline),
        session.heat_level_percent,
        session.fan_level_percent,
        session.cooling_on,
        session.phase,
        session.pending_driver_command_token,
        session.auto_t0_charge_temperature_c,
        session.first_crack_source,
    )
    actuator_calls = [0]

    def forbidden_control(*_: object, **__: object) -> RoasterState:
        actuator_calls[0] += 1
        raise AssertionError("timing evidence must not actuate")

    for method in ("set_heat", "set_fan", "drop_beans", "start_cooling", "stop_cooling"):
        monkeypatch.setattr(driver, method, forbidden_control)
    if outcome == "post_read":
        original_read = driver.read_state

        def finalise_during_read() -> RoasterState:
            with store._lock:  # pyright: ignore[reportPrivateUsage]
                store._nonterminal_finalisations.add(session.id)  # pyright: ignore[reportPrivateUsage]
            return original_read()

        monkeypatch.setattr(driver, "read_state", finalise_during_read)
    result = _run(context, "missing" if outcome == "pre_read" else session.id)
    assert result.driver_read == expected_read
    assert actuator_calls == [0]
    assert (
        log_path.read_bytes(),
        list(session.telemetry_buffer),
        list(session.event_timeline),
        session.heat_level_percent,
        session.fan_level_percent,
        session.cooling_on,
        session.phase,
        session.pending_driver_command_token,
        session.auto_t0_charge_temperature_c,
        session.first_crack_source,
    ) == before
    if outcome == "evidence":
        assert result.record is not None and result.record.evidence_sequence == 1
    else:
        assert result.record is None
