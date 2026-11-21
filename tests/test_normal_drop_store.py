"""Store-only contract tests for the P2 exact normal-drop reservation."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Thread

import pytest

import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.session import (
    LogWriterReference,
    NormalDropIdentity,
    NormalDropNonDispatchProof,
    NormalDropReservation,
    RoastSession,
    RoastSessionStore,
    SessionLifecycleError,
    TimingEvidenceAdmission,
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


def _non_dispatch_proof(reservation: NormalDropReservation) -> NormalDropNonDispatchProof:
    """Return the test-double's positive atomic mock non-dispatch evidence."""
    return NormalDropNonDispatchProof(reservation=reservation, zero_actuation=True)


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
    assert store.admit_normal_drop(identity).outcome == "ambiguous"
    with pytest.raises(SessionLifecycleError, match="unresolved"):
        store.reserve_driver_fault_recovery(session, kind="drop")


def test_exact_admission_without_writer_allows_direct_fault_recording() -> None:
    """An in-memory exact Drop reservation does not suppress a direct fault."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    event = store.record_event(session, "fault", payload={"reason": "test"})

    assert event.kind == "fault"
    assert session.faulted_at_utc is not None


def test_unresolved_drop_blocks_p1_timing_after_token_cancellation() -> None:
    """P1 admission and assembly use the retained P2 fence, not the token."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    prior_admission = TimingEvidenceAdmission(
        session,
        session.capture_ownership_sequence,
        session.id,
    )
    normal_admission = store.admit_normal_drop(identity)
    assert normal_admission.reservation is not None
    store.emergency_stop(session, reason="test")
    store.cancel_nonfinalisation_driver_command(session)

    assert store.admit_timing_evidence(session.id).busy_rejection == "drop_in_progress"
    assembled = store.assemble_timing_evidence(
        prior_admission,
        t0=100.0,
        t1=100.0,
        channels={},
        finalize=lambda record: record,
    )
    assert (assembled.outcome, assembled.reason_code) == ("refused_busy", "drop_in_progress")


def test_legacy_drop_preserves_timing_admission_identity_and_finalisation_precedence() -> None:
    """A legacy Drop does not outrank P1 identity or finalisation refusal."""
    store = RoastSessionStore()
    session, _ = _ready_drop(store)

    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.pending_driver_command_kind = "drop"
        session.phase = "fault"  # type: ignore[assignment]
    faulted = store.admit_timing_evidence(session.id)
    assert faulted.identity_rejection == "session_faulted"

    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.phase = "roasting"  # type: ignore[assignment]
        store._nonterminal_finalisations.add(session.id)  # pyright: ignore[reportPrivateUsage]
    finalising = store.admit_timing_evidence(session.id)
    assert finalising.busy_rejection == "finalisation_in_progress"


def test_legacy_drop_preserves_timing_assembly_identity_and_finalisation_precedence() -> None:
    """A legacy Drop does not outrank P1 assembly identity or finalisation refusal."""
    store = RoastSessionStore()
    session, _ = _ready_drop(store)
    admission = TimingEvidenceAdmission(
        session,
        session.capture_ownership_sequence,
        session.id,
    )

    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.pending_driver_command_kind = "drop"
        session.phase = "fault"  # type: ignore[assignment]
    faulted = store.assemble_timing_evidence(
        admission,
        t0=100.0,
        t1=100.0,
        channels={},
        finalize=lambda record: record,
    )
    assert (faulted.outcome, faulted.reason_code) == (
        "refused_identity",
        "session_changed_during_read",
    )

    with store._lock:  # pyright: ignore[reportPrivateUsage]
        session.phase = "roasting"  # type: ignore[assignment]
        store._nonterminal_finalisations.add(session.id)  # pyright: ignore[reportPrivateUsage]
    finalising = store.assemble_timing_evidence(
        admission,
        t0=100.0,
        t1=100.0,
        channels={},
        finalize=lambda record: record,
    )
    assert (finalising.outcome, finalising.reason_code) == (
        "refused_busy",
        "finalisation_in_progress",
    )


def test_simultaneous_normal_drop_admission_has_one_owner() -> None:
    """Two real threads receive one reservation and one non-dispatch result."""
    store = RoastSessionStore()
    _, identity = _ready_drop(store)
    barrier = Barrier(3)
    outcomes: list[str] = []

    def admit() -> None:
        barrier.wait()
        outcomes.append(store.admit_normal_drop(identity).outcome)

    threads = [Thread(target=admit), Thread(target=admit)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["dispatch", "in_progress"]


def test_known_cancellation_releases_only_its_exact_admission() -> None:
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    cancelled = store.settle_normal_drop_cancelled(
        session,
        reservation=admission.reservation,
        proof=_non_dispatch_proof(admission.reservation),
    )

    assert cancelled.outcome == "cancelled"
    assert store.admit_normal_drop(identity).outcome == "dispatch"
    duplicate = store.settle_normal_drop_cancelled(
        session,
        reservation=admission.reservation,
        proof=_non_dispatch_proof(admission.reservation),
    )
    assert duplicate.outcome == "stale"


@pytest.mark.parametrize("settlement", ["cancelled", "unknown"])
def test_unstarted_drop_turn_is_discharged_before_a_later_emergency(
    settlement: str,
) -> None:
    """An emergency never waits behind an exact Drop that cannot append."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    if settlement == "cancelled":
        result = store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=_non_dispatch_proof(admission.reservation),
        )
    else:
        result = store.retain_normal_drop_unknown(session, reservation=admission.reservation)

    assert result.outcome == settlement
    assert not store.has_pending_normal_drop_persistence(session)
    fault = store.emergency_stop(session, reason=f"after-{settlement}")
    assert fault.kind == "fault"


def test_append_probe_rejects_a_nonregular_replacement_after_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded probe validates its opened descriptor after a path replacement."""
    store = RoastSessionStore()
    session, _ = _ready_drop(store)
    session.log_writer = LogWriterReference(session_id=session.id, log_dir=tmp_path)
    log_path = tmp_path / "roast.jsonl"
    log_path.write_bytes(b"{}\n")
    original_open = session_module.os.open

    def replace_before_open(path: str | os.PathLike[str], flags: int) -> int:
        assert Path(path) == log_path
        log_path.unlink()
        os.mkfifo(log_path)
        return original_open(path, flags)

    monkeypatch.setattr(session_module.os, "open", replace_before_open)

    evidence = session_module._capture_append_failure_evidence(session)  # pyright: ignore[reportPrivateUsage]

    assert evidence.complete_tail is False


def test_recursive_tail_parse_failure_marks_drop_append_torn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser recursion failure cannot be treated as a clean failed append."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    session.log_writer = LogWriterReference(session_id=session.id, log_dir=tmp_path)
    log_path = tmp_path / "roast.jsonl"
    log_path.write_bytes((b"[" * 2_000) + b"\n")
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    def fail_append(_session: RoastSession, _event: object) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(session_module, "_append_event_log_row", fail_append)
    settled = store.complete_normal_drop_success(
        session,
        reservation=admission.reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=True,
    )

    assert settled.outcome == "unknown"
    assert store.normal_drop_persistence_is_torn(session)
    assert store._normal_drop_persistence_last_completion.is_set()  # pyright: ignore[reportPrivateUsage]


def test_emergency_cleared_token_can_settle_only_with_exact_positive_proof() -> None:
    """A faulted exact owner reports pending ambiguity until mock proof arrives."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    store.emergency_stop(session, reason="test")
    store.cancel_nonfinalisation_driver_command(session)

    assert store.admit_normal_drop(identity).outcome == "ambiguous"
    assert (
        store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=None,
        ).outcome
        == "stale"
    )
    cancelled = store.settle_normal_drop_cancelled(
        session,
        reservation=admission.reservation,
        proof=_non_dispatch_proof(admission.reservation),
    )
    assert cancelled.outcome == "cancelled"
    assert store.admit_normal_drop(identity).rejection == "session_faulted"


def test_foreign_event_cannot_claim_pending_normal_drop_completion() -> None:
    """Only the exact P2 settlement can append the protected endpoint marker."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    with pytest.raises(SessionLifecycleError, match="owned normal-drop settlement"):
        store.record_event(session, "beans_dropped")

    assert session.beans_dropped_at_utc is None
    assert store.admit_normal_drop(identity).outcome == "in_progress"


@pytest.mark.parametrize(
    ("heat_level_percent", "fan_level_percent", "cooling_on"),
    [
        (1, 100, True),
        (0, 99, True),
        (0, 100, False),
        (True, 100, True),
        (0, "100", True),
        (0, 100, 1),
    ],
)
def test_contradictory_or_malformed_mock_drop_projection_is_unknown(
    heat_level_percent: object,
    fan_level_percent: object,
    cooling_on: object,
) -> None:
    """Bad mock reports cannot write a confirmed endpoint or mutate controls."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None
    before = (session.heat_level_percent, session.fan_level_percent, session.cooling_on)

    settled = store.complete_normal_drop_success(
        session,
        reservation=admission.reservation,
        heat_level_percent=heat_level_percent,  # type: ignore[arg-type]
        fan_level_percent=fan_level_percent,  # type: ignore[arg-type]
        cooling_on=cooling_on,  # type: ignore[arg-type]
    )

    assert settled.outcome == "unknown"
    assert session.beans_dropped_at_utc is None
    assert (session.heat_level_percent, session.fan_level_percent, session.cooling_on) == before
    assert store.admit_normal_drop(identity).outcome == "ambiguous"


def test_driver_uncertainty_makes_same_operation_unknown_terminal() -> None:
    """A later cancellation or success cannot reverse retained uncertainty."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    uncertain = store.retain_normal_drop_unknown(session, reservation=admission.reservation)

    assert uncertain.outcome == "unknown"
    assert (
        store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=_non_dispatch_proof(admission.reservation),
        ).outcome
        == "stale"
    )
    assert (
        store.complete_normal_drop_success(
            session,
            reservation=admission.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        ).outcome
        == "unknown"
    )
    assert store.admit_normal_drop(identity).outcome == "ambiguous"


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
    assert (
        store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=_non_dispatch_proof(admission.reservation),
        ).outcome
        == "stale"
    )
    assert (
        store.complete_normal_drop_success(
            session,
            reservation=admission.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        ).outcome
        == "unknown"
    )
    assert store.admit_normal_drop(identity).outcome == "ambiguous"


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
    assert (
        store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=_non_dispatch_proof(admission.reservation),
        ).outcome
        == "stale"
    )
    assert (
        store.complete_normal_drop_success(
            session,
            reservation=admission.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        ).outcome
        == "unknown"
    )
    assert store.admit_normal_drop(identity).outcome == "ambiguous"
    with pytest.raises(SessionLifecycleError, match="ambiguous bean drop"):
        store.stop_session()
        store.start_session()


def test_emergency_cannot_make_terminal_unknown_cancellable() -> None:
    """Emergency cancellation of the token leaves an uncertain drop fenced."""
    store = RoastSessionStore()
    session, identity = _ready_drop(store)
    admission = store.admit_normal_drop(identity)
    assert admission.reservation is not None

    assert (
        store.retain_normal_drop_unknown(session, reservation=admission.reservation).outcome
        == "unknown"
    )
    store.emergency_stop(session, reason="test")
    store.cancel_nonfinalisation_driver_command(session)

    assert (
        store.settle_normal_drop_cancelled(
            session,
            reservation=admission.reservation,
            proof=_non_dispatch_proof(admission.reservation),
        ).outcome
        == "stale"
    )
    assert (
        store.complete_normal_drop_success(
            session,
            reservation=admission.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        ).outcome
        == "unknown"
    )
    with pytest.raises(SessionLifecycleError, match="ambiguous bean drop"):
        store.start_session()


def test_stale_settlement_cannot_clear_a_replacement_or_restart_fence() -> None:
    identifiers = iter(("same-id", "same-id"))
    first_store = RoastSessionStore(session_id_factory=lambda: next(identifiers))
    first_session, first_identity = _ready_drop(first_store)
    first = first_store.admit_normal_drop(first_identity)
    assert first.reservation is not None
    first_cancel = first_store.settle_normal_drop_cancelled(
        first_session,
        reservation=first.reservation,
        proof=_non_dispatch_proof(first.reservation),
    )
    assert first_cancel.outcome == "cancelled"
    first_store.stop_session()

    replacement = first_store.start_session()
    first_session.timing_evidence_sequence = 1
    stale_cancel = first_store.settle_normal_drop_cancelled(
        first_session,
        reservation=first.reservation,
        proof=_non_dispatch_proof(first.reservation),
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
        cooling_on=True,
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


def test_cold_characterisation_normal_drop_is_refused() -> None:
    """The P2 normal Drop path is unavailable outside roast sessions."""
    store = RoastSessionStore()
    session = store.start_session(purpose="cold_characterisation")
    session.log_writer = None
    session.timing_evidence_sequence = 1
    identity = store.normal_drop_identity(session)

    assert store.admit_normal_drop(identity).rejection == "session_purpose_not_eligible"
