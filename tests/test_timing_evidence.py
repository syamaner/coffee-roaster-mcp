"""Mock-only contract tests for the ``timing_evidence/v1`` boundary."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

import coffee_roaster_mcp.mcp_server as mcp_server
import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.drivers import MockRoasterDriver, RoasterCapabilities, RoasterState
from coffee_roaster_mcp.session import RoastSessionStore


class _Clock:
    """Deterministic exact-float clock for one timing-evidence test."""

    def __init__(self) -> None:
        self.values = iter((100.0, 100.1, 100.2, 101.25, 102.5))

    def now(self) -> float:
        """Return the next injected clock value."""
        return next(self.values)


def _context() -> tuple[mcp_server.ServerContext, MockRoasterDriver, str]:
    """Build a minimal mock-only context accepted by the orchestration function."""
    clock = _Clock()
    store = RoastSessionStore(
        utc_now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        monotonic_now=clock.now,
        session_id_factory=lambda: "timing-session",
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


def test_timing_evidence_models_reject_inexact_and_inconsistent_rows() -> None:
    """Wire models reject coercion and source/time-basis mismatches before output."""
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
