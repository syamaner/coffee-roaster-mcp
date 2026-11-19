"""Mock-only contract tests for the ``timing_evidence/v1`` boundary."""

from __future__ import annotations

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
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

import coffee_roaster_mcp.mcp_server as mcp_server
import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.drivers import MockRoasterDriver, RoasterCapabilities, RoasterState
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


def test_session_timing_ownership_does_not_depend_on_mcp_or_drivers() -> None:
    """The store keeps its closed direction while accepting plain evidence values."""
    source = inspect.getsource(session_module)

    assert "coffee_roaster_mcp.mcp_server" not in source
    assert "coffee_roaster_mcp.drivers" not in source


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
        ("environment", math.inf, "invalid", "non_finite"),
        ("bean", -math.inf, "invalid", "non_finite"),
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
                "9f032a0022a8d948a7f2ede0c5c1c4f4a683c1f1612a38046d0688ad193d5370"
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
            malformed = [
                {"expected_session_id": "x", "evidence_protocol": "wrong"},
                {"expected_session_id": 1, "evidence_protocol": "timing_evidence/v1"},
                {"expected_session_id": "", "evidence_protocol": "timing_evidence/v1"},
                {"expected_session_id": "x" * 129, "evidence_protocol": "timing_evidence/v1"},
                {
                    "expected_session_id": "x",
                    "evidence_protocol": "timing_evidence/v1",
                    "extra": "blocked",
                },
            ]
            for arguments in malformed:
                reply = await client.call_tool("get_timing_evidence", arguments)
                assert reply.isError is True
                assert read_counter.read_text(encoding="utf-8") == "0"

            started = await client.call_tool("start_roast_session", {})
            assert started.structuredContent is not None
            started_content = cast(Mapping[str, object], started.structuredContent)
            session = cast(Mapping[str, object], started_content["session"])
            session_id = cast(str, session["session_id"])
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
