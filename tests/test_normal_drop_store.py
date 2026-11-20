"""Store-only contract tests for the P2 exact normal-drop reservation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.session import (
    NormalDropIdentity,
    RoastSession,
    RoastSessionStore,
    SessionLifecycleError,
)


class ClockHarness:
    """Deterministic clock suppliers for normal-drop store tests."""

    def __init__(self) -> None:
        self.utc_value = datetime(2026, 11, 20, tzinfo=UTC)
        self.monotonic_value = 100.0

    def utc_now(self) -> datetime:
        """Return the configured UTC value."""
        return self.utc_value

    def monotonic_now(self) -> float:
        """Return the configured monotonic value."""
        return self.monotonic_value


def _ready_drop(store: RoastSessionStore) -> tuple[RoastSession, NormalDropIdentity]:
    """Create an active roasting session with deterministic timing evidence."""
    session = store.start_session()
    session.log_writer = None
    store.record_event(session, "beans_added")
    session.timing_evidence_sequence = 1
    return session, store.normal_drop_identity(session)


def test_exact_admission_reserves_once_and_blocks_legacy_and_recovery_drop() -> None:
    store = RoastSessionStore()
    session, identity = _ready_drop(store)

    first = store.admit_normal_drop(identity)
    second = store.admit_normal_drop(identity)

    assert first.outcome == "dispatch"
    assert first.reservation is not None
    assert second.outcome == "in_progress"
    with pytest.raises(SessionLifecycleError, match="unresolved"):
        store.reserve_driver_drop(session)

    store.emergency_stop(session, reason="test")
    store.cancel_nonfinalisation_driver_command(session)
    assert store.admit_normal_drop(identity).outcome == "refused"
    with pytest.raises(SessionLifecycleError, match="unresolved"):
        store.reserve_driver_fault_recovery(session, kind="drop")


def test_known_cancellation_releases_only_its_exact_admission() -> None:
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    cancelled = store.settle_normal_drop_cancelled(session, reservation=admission.reservation)

    assert cancelled.outcome == "cancelled"
    assert store.admit_normal_drop(identity).outcome == "dispatch"
    duplicate = store.settle_normal_drop_cancelled(session, reservation=admission.reservation)
    assert duplicate.outcome == "stale"


def test_recorded_event_precedes_evidence_freshness_but_partial_endpoint_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None
    append_name = "_append_event_log_row"
    original_append = getattr(session_module, append_name)
    calls = 0

    def fail_second_append(current: RoastSession, event: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second endpoint append failed")
        original_append(current, event)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "_append_event_log_row", fail_second_append)

    settled = store.complete_normal_drop_success(
        session,
        reservation=admission.reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=True,
    )

    assert settled.outcome == "unknown"
    assert session.beans_dropped_at_utc is not None
    stale_identity = NormalDropIdentity(
        session_id=identity.session_id,
        clock_epoch_id=identity.clock_epoch_id,
        ownership_sequence=identity.ownership_sequence,
        timing_evidence_sequence=999,
    )
    retry = store.admit_normal_drop(stale_identity)
    assert retry.outcome == "ambiguous"
    assert retry.existing_event is not None
    assert retry.existing_event.kind == "beans_dropped"


def test_first_endpoint_append_failure_retains_unknown_fence_without_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    def fail_append(_session: RoastSession, _event: object) -> None:
        raise OSError("first endpoint append failed")

    monkeypatch.setattr(session_module, "_append_event_log_row", fail_append)

    settled = store.complete_normal_drop_success(
        session,
        reservation=admission.reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=True,
    )

    assert settled.outcome == "unknown"
    assert session.beans_dropped_at_utc is None
    assert store.admit_normal_drop(identity).outcome == "ambiguous"
    with pytest.raises(SessionLifecycleError, match="ambiguous bean drop"):
        store.stop_session()
        store.start_session()


def test_stale_settlement_cannot_clear_a_replacement_or_restart_fence() -> None:
    identifiers = iter(("same-id", "same-id"))
    first_store = RoastSessionStore(session_id_factory=lambda: next(identifiers))
    first_session, first_identity = _ready_drop(first_store)
    first = first_store.admit_normal_drop(first_identity)
    assert first.reservation is not None
    first_cancel = first_store.settle_normal_drop_cancelled(
        first_session, reservation=first.reservation
    )
    assert first_cancel.outcome == "cancelled"
    first_store.stop_session()

    replacement = first_store.start_session()
    first_session.timing_evidence_sequence = 1
    stale_cancel = first_store.settle_normal_drop_cancelled(
        first_session, reservation=first.reservation
    )
    assert stale_cancel.outcome == "stale"
    assert replacement.id == "same-id"

    restarted_store = RoastSessionStore(session_id_factory=lambda: "same-id")
    restarted_session, restarted_identity = _ready_drop(restarted_store)
    old_epoch = NormalDropIdentity(
        session_id=restarted_identity.session_id,
        clock_epoch_id=first_identity.clock_epoch_id,
        ownership_sequence=restarted_identity.ownership_sequence,
        timing_evidence_sequence=restarted_identity.timing_evidence_sequence,
    )
    assert restarted_store.admit_normal_drop(old_epoch).rejection == "clock_epoch_mismatch"
    assert restarted_store.admit_normal_drop(restarted_identity).outcome == "dispatch"
    assert restarted_session.active


def test_identity_and_phase_fail_closed_and_success_is_idempotent() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    session.log_writer = None
    session.timing_evidence_sequence = 1
    pre_roast_identity = store.normal_drop_identity(session)
    assert store.admit_normal_drop(pre_roast_identity).rejection == "phase_invalid"

    store.record_event(session, "beans_added")
    identity = store.normal_drop_identity(session)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None
    success = store.complete_normal_drop_success(
        session,
        reservation=admission.reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=False,
    )

    assert success.outcome == "success"
    assert success.event is not None
    stale_evidence = NormalDropIdentity(
        session_id=identity.session_id,
        clock_epoch_id=identity.clock_epoch_id,
        ownership_sequence=identity.ownership_sequence,
        timing_evidence_sequence=0,
    )
    retry = store.admit_normal_drop(stale_evidence)
    assert retry.outcome == "recorded"
    assert retry.existing_event == success.event
