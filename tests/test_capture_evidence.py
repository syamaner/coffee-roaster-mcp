"""Bounded capture-evidence publication and read tests."""

from __future__ import annotations

import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import coffee_roaster_mcp.capture_evidence as capture_evidence
import coffee_roaster_mcp.mcp_server as mcp_server
from coffee_roaster_mcp.audio import (
    AdditionalRecordingDevice,
    AudioCapturePipeline,
    AudioCaptureSettings,
    AudioInput,
    EvidenceRoastRecorder,
    MultiDeviceRoastRecorder,
    RoastAudioRecorder,
)
from coffee_roaster_mcp.capture_evidence import CaptureEvidencePublisher
from coffee_roaster_mcp.mcp_server import (  # pyright: ignore[reportPrivateUsage]
    CaptureEvidenceInputResult,
    CaptureEvidenceResult,
    ServerContext,
    _read_capture_evidence,  # pyright: ignore[reportPrivateUsage]
)
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore


def _context(store: RoastSessionStore, publisher: CaptureEvidencePublisher) -> ServerContext:
    """Build the minimal inert reader context without runtime dependencies."""
    return cast(
        ServerContext,
        SimpleNamespace(
            capture_evidence_configured=True,
            config=SimpleNamespace(recording=SimpleNamespace(devices=())),
            session_store=store,
            capture_evidence_publisher=publisher,
        ),
    )


def _current_progress_lock(publisher: CaptureEvidencePublisher) -> object:
    """Expose the actual current-generation lock for contention witnesses."""
    authority = publisher._authority  # pyright: ignore[reportPrivateUsage]
    assert authority is not None
    return authority._lock  # pyright: ignore[reportPrivateUsage]


def _two_open_inputs() -> CaptureEvidencePublisher:
    """Return a current generation with two independently opened inputs."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    for slot in (1, 2):
        assert publisher.open_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=slot,
            input_generation=1,
            backend_input_id=slot,
            reported_rate_hz=16_000.0,
        )
    return publisher


def _publish_winner_after_loser_starts(
    publisher: CaptureEvidencePublisher,
    winner: Callable[[], None],
    loser: Callable[[], None],
) -> None:
    """Make a losing nonblocking update observe a winner's immutable result."""
    authority = publisher._authority  # pyright: ignore[reportPrivateUsage]
    assert authority is not None
    original_lock = authority._lock  # pyright: ignore[reportPrivateUsage]
    assert original_lock.acquire(blocking=False)

    class PublishThenRefuseLock:
        """Release the held lock for one linked winner before refusing its loser."""

        def __init__(self) -> None:
            self._published = False

        def acquire(self, blocking: bool = True) -> bool:
            if original_lock.acquire(blocking=blocking):
                return True
            if not self._published:
                self._published = True
                original_lock.release()
                winner()
            return False

        def release(self) -> None:
            original_lock.release()

    object.__setattr__(authority, "_lock", PublishThenRefuseLock())
    try:
        # The loser starts while the original lock is held. The proxy publishes
        # the winner before returning the loser's failed lock attempt.
        loser()
    finally:
        object.__setattr__(authority, "_lock", original_lock)


def test_subject_sequence_advances_and_copy_retains_it() -> None:
    """Replacement ownership remains distinct even when test ids are reused."""
    ids = iter(("same", "same"))
    store = RoastSessionStore(session_id_factory=lambda: next(ids))

    first = store.start_session()
    first_subject = store.try_get_capture_subject()
    store.stop_session()
    second = store.start_session_snapshot()
    second_subject = store.try_get_capture_subject()

    assert first.id == second.id == "same"
    assert first_subject is not None and second_subject is not None
    assert first_subject.ownership_sequence == first.capture_ownership_sequence == 1
    assert second_subject.ownership_sequence == second.capture_ownership_sequence == 2


def test_capture_ownership_sequence_preserves_positional_and_equality_contract() -> None:
    """The internal ownership fence is neither positional nor value identity."""
    from datetime import UTC, datetime

    created = datetime(2026, 1, 1, tzinfo=UTC)
    original = RoastSession("session", created, 1.0, "roast")
    successor = RoastSession("session", created, 1.0, "roast", capture_ownership_sequence=9)

    assert original.purpose == "roast"
    assert original == successor


def test_reader_returns_observed_only_for_exact_current_subject() -> None:
    """A successful snapshot is bracketed by matching exact store ownership."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    publisher.open_input(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        reported_rate_hz=16_000.0,
        wav_header_rate_hz=16_000.0,
    )
    publisher.publish_progress(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=0,
        accepted_frames=0,
        flushed_frames=0,
    )

    observed = _read_capture_evidence(_context(store, publisher), session.id)
    wrong = _read_capture_evidence(_context(store, publisher), "other")

    assert observed.outcome == "observed"
    assert observed.session_id == session.id
    assert observed.inputs[0].input_frames == observed.inputs[0].accepted_frames == 0
    assert observed.inputs[0].identity == "unresolved"
    assert wrong.outcome == "refused"
    assert wrong.reason == "wrong_session"
    assert wrong.inputs == ()


def test_store_reservation_maps_to_start_in_flight_without_a_publication_read() -> None:
    """An actual store reservation supplies no capture subject or evidence."""
    store = RoastSessionStore()
    publisher = CaptureEvidencePublisher()
    reservation = store.reserve_session_start()
    try:
        result = _read_capture_evidence(_context(store, publisher), "expected")
    finally:
        store.clear_session_start_reservation(reservation)

    assert (result.outcome, result.reason, result.session_id, result.inputs) == (
        "unavailable",
        "start_in_flight",
        None,
        (),
    )


def test_active_subject_before_its_runtime_setup_is_capture_pending() -> None:
    """A new active store owner never receives an older recorder projection."""
    store = RoastSessionStore()
    session = store.start_session()

    result = _read_capture_evidence(_context(store, CaptureEvidencePublisher()), session.id)

    assert (result.outcome, result.reason, result.session_id, result.session_state) == (
        "unavailable",
        "capture_pending",
        session.id,
        "active",
    )


def test_stopped_subject_remains_explicit_without_proving_active_capture() -> None:
    """A stopped matching subject retains its own projection and state only."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    store.stop_session()

    result = _read_capture_evidence(_context(store, publisher), session.id)

    assert (result.outcome, result.session_id, result.session_state) == (
        "observed",
        session.id,
        "stopped",
    )


@pytest.mark.parametrize("now", [-1.0, float("inf"), float("nan")])
def test_unreliable_clock_never_clamps_capture_evidence_age(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    """Finite-negative and nonfinite reader clocks are unavailable, not fresh."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher(monotonic_now=lambda: 1.0)
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: now)

    result = _read_capture_evidence(_context(store, publisher), session.id)

    assert (result.outcome, result.reason, result.age_seconds) == (
        "unavailable",
        "clock_unreliable",
        None,
    )


def test_inventory_larger_than_eight_refuses_without_snapshot_truncation() -> None:
    """The reader rejects an oversized configured inventory before sampling evidence."""
    store = RoastSessionStore()
    context = _context(store, CaptureEvidencePublisher())
    object.__setattr__(
        context,
        "config",
        SimpleNamespace(recording=SimpleNamespace(devices=tuple(range(9)))),
    )

    result = _read_capture_evidence(context, "expected")

    assert (result.outcome, result.reason, result.inputs) == (
        "refused",
        "inventory_too_large",
        (),
    )


def test_missing_recorder_after_setup_maps_to_recorder_unavailable() -> None:
    """A matched setup disposition exposes no partial input projection."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    publisher.publish_recorder_unavailable(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
    )

    result = _read_capture_evidence(_context(store, publisher), session.id)

    assert (result.outcome, result.reason, result.session_id, result.inputs) == (
        "unavailable",
        "recorder_unavailable",
        session.id,
        (),
    )


def test_stale_owner_updates_cannot_replace_successor_projection() -> None:
    """Old recorder progress and close cannot alter a newer store owner."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="old", ownership_sequence=1, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    assert publisher.publish_setup(
        session_id="new", ownership_sequence=2, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    publisher.open_input(
        session_id="old",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        reported_rate_hz=16_000.0,
    )
    publisher.close_input(
        session_id="old",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )

    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    assert snapshot.session_id == "new"
    assert snapshot.inputs[0].identity == "unopened"


def test_paused_old_authority_cannot_mutate_immediately_readable_same_id_successor() -> None:
    """A newer exact owner replaces an old paused publication, even with the same id."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="same", ownership_sequence=1, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    old_authority = publisher._authority  # pyright: ignore[reportPrivateUsage]
    assert old_authority is not None
    entered = Event()
    release = Event()

    def pause_old_update() -> None:
        def update(
            snapshot: capture_evidence.CaptureEvidenceSnapshot,
        ) -> capture_evidence.CaptureEvidenceSnapshot:
            entered.set()
            assert release.wait(timeout=1)
            return replace(snapshot, publication_sequence=snapshot.publication_sequence + 1)

        assert old_authority.try_replace(update)

    worker = Thread(target=pause_old_update)
    worker.start()
    assert entered.wait(timeout=1)
    assert publisher.publish_setup(
        session_id="same", ownership_sequence=2, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    successor = publisher.try_snapshot()
    assert successor is not False and successor is not None
    assert (successor.ownership_sequence, successor.publication_sequence) == (2, 2)
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    current = publisher.try_snapshot()
    assert current is not False and current is not None
    assert (current.ownership_sequence, current.publication_sequence) == (2, 2)


def test_same_id_replacement_between_store_samples_is_subject_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement after the snapshot cannot reuse same-id evidence as current."""
    store = RoastSessionStore(session_id_factory=lambda: "same")
    first = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=first.id,
        ownership_sequence=first.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    original_snapshot = publisher.try_snapshot

    def replace_after_snapshot() -> capture_evidence.CaptureEvidenceSnapshot | None | bool:
        snapshot = original_snapshot()
        store.stop_session()
        successor = store.start_session_snapshot()
        assert successor.id == first.id
        assert successor.capture_ownership_sequence != first.capture_ownership_sequence
        return snapshot

    monkeypatch.setattr(publisher, "try_snapshot", replace_after_snapshot)
    result = _read_capture_evidence(_context(store, publisher), first.id)

    assert (result.outcome, result.reason, result.session_id, result.inputs) == (
        "unavailable",
        "subject_changed",
        None,
        (),
    )


def test_setup_failure_is_attributable_to_its_exact_current_owner() -> None:
    """Setup failure changes no ownership coordinate or prior successor state."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    publisher.publish_setup_failure(
        session_id="session", ownership_sequence=1, recorder_generation=1
    )
    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    assert snapshot.setup_failed is True


def test_lost_setup_failure_is_publicly_unavailable_for_its_current_owner() -> None:
    """A contended disposition matches coordinates, not its new field value."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        publisher.publish_setup_failure(
            session_id=session.id,
            ownership_sequence=session.capture_ownership_sequence,
            recorder_generation=1,
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]

    result = _read_capture_evidence(_context(store, publisher), session.id)

    assert result.outcome == "unavailable"
    assert result.reason == "publication_contended"


def test_setup_installs_while_old_generation_reader_is_contended() -> None:
    """A successor setup does not contend with an old generation's reader lock."""
    store = RoastSessionStore(session_id_factory=lambda: "same")
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        store.stop_session()
        successor = store.start_session_snapshot()
        assert publisher.publish_setup(
            session_id=successor.id,
            ownership_sequence=successor.capture_ownership_sequence,
            recorder_generation=2,
            requested_rates_hz=(16_000,),
        )
        result = _read_capture_evidence(_context(store, publisher), successor.id)
    finally:
        progress_lock.release()  # type: ignore[attr-defined]

    assert successor.id == session.id
    assert successor.capture_ownership_sequence != session.capture_ownership_sequence
    assert (result.outcome, result.reason) == ("observed", None)


def test_absolute_progress_restores_lost_counts_before_flush() -> None:
    """A missed delta cannot make a later known flush exceed accepted frames."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        assert not publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=1_600,
            accepted_frames=1_600,
            flushed_frames=0,
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=1_600,
        accepted_frames=1_600,
        flushed_frames=1_600,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].accepted_frames == snapshot.inputs[0].flushed_frames == 1_600


def test_absolute_progress_restores_a_lost_input_read() -> None:
    """A later absolute observation repairs an ordinary contended read update."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        assert not publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=800,
            accepted_frames=800,
            flushed_frames=0,
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=1_600,
        accepted_frames=1_600,
        flushed_frames=0,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].input_frames == snapshot.inputs[0].accepted_frames == 1_600


def test_lost_current_terminal_update_latches_unavailability_without_stale_poisoning() -> None:
    """A lost current failure is safe, while stale failure cannot hide a successor."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="current",
        ownership_sequence=2,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="current",
        ownership_sequence=2,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        publisher.fail_input(
            session_id="current",
            ownership_sequence=2,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="write_failed",
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]
    assert publisher.try_snapshot() is False

    assert publisher.publish_setup(
        session_id="successor",
        ownership_sequence=3,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.session_id == "successor"


def test_unopened_terminal_attempt_fences_late_same_generation_open() -> None:
    """An open failure remains terminal without exposing an opened generation."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="open_failed",
    )

    assert not publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=2,
    )
    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_generation, row.identity, row.state, row.failure) == (
        None,
        "unopened",
        "failed",
        "open_failed",
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=2,
        backend_input_id=2,
    )


def test_contended_pending_open_failure_latches_that_attempt_only() -> None:
    """A lost pending failure cannot be mistaken for a healthy pending row."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        publisher.fail_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="open_failed",
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]

    assert publisher.try_snapshot() is False
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        backend_input_id=3,
    )


def test_lost_close_on_pending_row_fences_later_same_generation_open() -> None:
    """A pending row cannot make a lost close look like a redundant no-op."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        # The close samples the old pending projection when its nonblocking
        # publication loses.  The same attempt's open wins only afterwards.
        publisher.close_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            confirmed=True,
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]

    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=2,
    )
    # The later open cannot re-establish liveness after a close disposition
    # was lost while the row was still pending.
    assert publisher.try_snapshot() is False


def test_same_generation_header_receipt_can_arrive_after_unresolved_open() -> None:
    """A writer receipt refines its own unresolved input without inferring identity."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        reported_rate_hz=12_000.0,
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        wav_header_rate_hz=22_050.0,
    )
    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.identity, row.reported_rate_hz, row.wav_header_rate_hz) == (
        "unresolved",
        12_000.0,
        22_050.0,
    )


def test_stale_reopen_failure_does_not_latch_current_generation() -> None:
    """A stale terminal claim after a reopen remains unable to poison current evidence."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=2,
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        publisher.fail_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="read_failed",
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].input_generation == 2
    assert snapshot.inputs[0].state == "capturing"


def test_confirmed_close_does_not_infer_flush_progress() -> None:
    """A terminal disposition cannot manufacture a writer flush count."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=4,
        accepted_frames=4,
        flushed_frames=0,
    )
    publisher.close_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].state == "closed"
    assert snapshot.inputs[0].accepted_frames == 4
    assert snapshot.inputs[0].flushed_frames == 0


def test_late_failure_cannot_replace_confirmed_close() -> None:
    """A definitive close rejects a later competing failure in the same generation."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    publisher.close_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="read_failed",
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("closed", None)


def test_contended_late_failure_after_confirmed_close_does_not_latch_unavailable() -> None:
    """A stale failure observes closed evidence before it can mark a lost terminal receipt."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    publisher.close_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )
    progress_lock = _current_progress_lock(publisher)
    assert progress_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        publisher.fail_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="read_failed",
        )
    finally:
        progress_lock.release()  # type: ignore[attr-defined]

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("closed", None)


def test_contended_duplicate_failure_after_linked_winner_is_not_lost() -> None:
    """A failed lock attempt sees the winner's identical failure as a no-op."""
    publisher = _two_open_inputs()

    def report_failure() -> None:
        publisher.fail_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="read_failed",
        )

    _publish_winner_after_loser_starts(publisher, report_failure, report_failure)

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("failed", "read_failed")
    assert snapshot.inputs[1].state == "capturing"


def test_contended_duplicate_confirmed_close_after_linked_winner_is_not_lost() -> None:
    """A confirmed close already published by the winner remains observable."""
    publisher = _two_open_inputs()

    def close() -> None:
        publisher.close_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            confirmed=True,
        )

    _publish_winner_after_loser_starts(publisher, close, close)

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("closed", None)
    assert snapshot.inputs[1].state == "capturing"


def test_contended_identical_final_receipt_after_linked_winner_is_not_lost() -> None:
    """An exact final receipt does not latch loss after the winner publishes it."""
    publisher = _two_open_inputs()

    def finalize() -> None:
        publisher.finalize_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=4,
            accepted_frames=4,
            flushed_frames=4,
            confirmed=True,
            wav_header_rate_hz=22_050.0,
        )

    _publish_winner_after_loser_starts(publisher, finalize, finalize)

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.state, row.failure) == ("closed", None)
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (4, 4, 4)
    assert row.wav_header_rate_hz == 22_050.0
    assert snapshot.inputs[1].state == "capturing"


def test_contended_newer_failed_final_receipt_remains_lost_until_successor() -> None:
    """A changed failed-row receipt remains unavailable and survives unrelated updates."""
    publisher = _two_open_inputs()
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="write_failed",
    )

    def older_receipt() -> None:
        publisher.finalize_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=1,
            accepted_frames=1,
            flushed_frames=1,
            confirmed=True,
            wav_header_rate_hz=16_000.0,
        )

    def newer_receipt() -> None:
        publisher.finalize_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=2,
            accepted_frames=2,
            flushed_frames=2,
            confirmed=True,
            wav_header_rate_hz=22_050.0,
        )

    _publish_winner_after_loser_starts(publisher, older_receipt, newer_receipt)
    assert publisher.try_snapshot() is False

    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        input_frames=3,
        accepted_frames=3,
        flushed_frames=3,
    )
    assert publisher.try_snapshot() is False

    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=2,
        backend_input_id=1,
        reported_rate_hz=16_000.0,
    )
    successor = publisher.try_snapshot()
    assert successor is not False and successor is not None
    assert successor.inputs[0].input_generation == 2
    assert successor.inputs[0].state == "capturing"


def test_duplicate_failure_preserves_first_reason_while_closing_failure_remains_admitted() -> None:
    """Real failures still fence active or unresolved-close rows without reason rewrites."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    publisher.close_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=False,
    )
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="write_failed",
    )
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="read_failed",
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("failed", "write_failed")


def test_routine_concurrent_progress_does_not_latch_publication() -> None:
    """A normal concurrent producer and reader retain an observable projection."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    release = Event()
    finished = Event()

    def publish() -> None:
        release.wait(timeout=1)
        publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=3,
            accepted_frames=3,
            flushed_frames=0,
        )
        finished.set()

    worker = Thread(target=publish)
    worker.start()
    release.set()
    assert finished.wait(timeout=1)
    worker.join(timeout=1)
    assert not worker.is_alive()
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].accepted_frames == 3


def test_single_recorder_publishes_actual_writer_receipts_and_close(tmp_path: Path) -> None:
    """The single-stream path reports only its writer's absolute receipts."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=200,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    evidence.begin()
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].wav_header_rate_hz is None
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].input_frames, snapshot.inputs[0].accepted_frames) == (160, 160)
    assert snapshot.inputs[0].flushed_frames == 0
    # Buffered acceptance leaves stdlib's WAV file empty: it is not header proof.
    assert snapshot.inputs[0].wav_header_rate_hz is None
    assert (tmp_path / "roast.wav").stat().st_size == 0

    evidence.write_samples([0.0] * 40)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].flushed_frames == 200
    assert snapshot.inputs[0].wav_header_rate_hz == 16_000.0

    evidence.close()
    evidence.write_samples([0.0])
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (200, 200, 200)
    assert (row.state, row.failure) == ("closed", None)


def test_finalizer_keeps_newer_tee_receipt_while_confirming_real_wav_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late final source receipt cannot turn a confirmed writer close into loss."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=200,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)

    original_finalize = publisher.finalize_input

    def publish_late_tee_receipt(**kwargs: object) -> None:
        # The finalizer has already sampled the first tee total (160). Model
        # the in-flight tee's independent second total before it is published.
        evidence._detector_input_frames += 160  # pyright: ignore[reportPrivateUsage] - tee-boundary witness
        evidence.observe_progress(
            slot=1,
            input_frames=None,
            accepted_frames=160,
            flushed_frames=160,
            wav_header_rate_hz=16_000.0,
        )
        original_finalize(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(publisher, "finalize_input", publish_late_tee_receipt)
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (320, 160, 160)
    assert (row.wav_header_rate_hz, row.state, row.failure) == (16_000.0, "closed", None)
    with wave.open(str(tmp_path / "roast.wav"), "rb") as wav_file:
        assert (wav_file.getframerate(), wav_file.getnframes()) == (16_000, 160)


def test_finalizer_does_not_accept_regressive_writer_receipts() -> None:
    """A source-only exception does not make contradictory writer receipts healthy."""
    publisher = _two_open_inputs()
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=320,
        accepted_frames=160,
        flushed_frames=160,
    )

    publisher.finalize_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=160,
        accepted_frames=159,
        flushed_frames=159,
        confirmed=True,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (320, 160, 160)
    assert (row.state, row.failure) == ("failed", "unknown")


def test_writer_header_rate_is_not_inferred_from_requested_or_backend_rate(tmp_path: Path) -> None:
    """Evidence distinguishes request, backend report, and writer header receipts."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=22_050,
        session_id="session",
        flush_sample_threshold=1,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=3, reported_rate_hz=12_000.0)
    # The writer owner, not the input reader, supplies the committed header
    # receipt alongside an actual writer boundary.
    evidence.write_samples([0.0])
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.requested_rate_hz, row.reported_rate_hz, row.wav_header_rate_hz) == (
        16_000,
        12_000.0,
        22_050.0,
    )


def test_reader_identity_observation_does_not_wait_for_held_writer_lock(tmp_path: Path) -> None:
    """Reader-side identity reports never inspect the WAV writer lock."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    writer_lock = recorder._writer._lock  # pyright: ignore[reportPrivateUsage]
    assert writer_lock.acquire(blocking=False)
    completed = Event()
    try:

        def report_reader_identity() -> None:
            evidence.observe_detector_input(backend_input_id=7, reported_rate_hz=16_000.0)
            # A repeated proved identity is the exact reader duplicate path.
            evidence.observe_detector_input(backend_input_id=7, reported_rate_hz=16_000.0)
            completed.set()

        reader = Thread(target=report_reader_identity)
        reader.start()
        assert completed.wait(timeout=0.2)
        reader.join(timeout=1)
        assert not reader.is_alive()
    finally:
        writer_lock.release()


def test_terminal_uses_tee_input_count_when_writer_rejects_submission(tmp_path: Path) -> None:
    """A rejected writer submission cannot replace the tee's input receipt."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    recorder._writer.write_samples = lambda samples: None  # pyright: ignore[reportPrivateUsage, method-assign]
    evidence.write_samples([0.0] * 160)
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (160, 0, 0)
    assert (row.state, row.failure) == ("closed", None)


def test_single_recorder_final_flush_failure_publishes_unconfirmed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final single-stream flush failure cannot retain healthy evidence."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=200,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(recorder._writer, "_flush_locked", fail_final_flush)  # pyright: ignore[reportPrivateUsage] - writer-boundary witness
    with pytest.raises(OSError, match="final flush"):
        evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (160, 160, 0)
    assert (row.state, row.failure) == ("failed", "close_unconfirmed")
    assert row.wav_header_rate_hz is None


def test_confirmed_empty_close_propagates_header_proof(tmp_path: Path) -> None:
    """A real, attributable zero-frame WAV proves its header only after close."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "empty.wav",
        sidecar_path=tmp_path / "empty.json",
        sample_rate=16_000,
        session_id="session",
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.accepted_frames, row.flushed_frames, row.wav_header_rate_hz) == (0, 0, 16_000.0)
    assert (row.state, row.failure) == ("closed", None)
    with wave.open(str(tmp_path / "empty.wav"), "rb") as wav_file:
        assert (wav_file.getframerate(), wav_file.getnframes()) == (16_000, 0)


def test_failed_first_writer_flush_retains_failure_and_later_close_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write remains failed while its later close reports actual totals."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=160,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    original_flush = recorder._writer._flush_locked  # pyright: ignore[reportPrivateUsage]
    attempts = 0

    def fail_first_flush() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic first writer flush failure")
        original_flush()

    monkeypatch.setattr(recorder._writer, "_flush_locked", fail_first_flush)  # pyright: ignore[reportPrivateUsage] - writer-boundary witness
    with pytest.raises(OSError, match="first writer flush"):
        evidence.write_samples([0.0] * 160)
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (160, 160, 160)
    assert (row.state, row.failure) == ("failed", "write_failed")


def test_stale_late_finalizer_cannot_fill_successor_generation() -> None:
    """A late terminal receipt cannot alter an input reopened by its successor."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    publisher.fail_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        failure="write_failed",
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=2,
    )

    publisher.finalize_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=160,
        accepted_frames=160,
        flushed_frames=160,
        confirmed=True,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_generation, row.input_frames, row.accepted_frames, row.flushed_frames) == (
        2,
        0,
        0,
        0,
    )
    assert (row.state, row.failure) == ("capturing", None)


def test_multi_recorder_final_flush_failures_are_per_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detector and additional writers retain independent terminal receipts."""

    class OneChunkInput(AudioInput):
        def __init__(self) -> None:
            self.read_once = Event()
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self.read_once.set()
            return [0.0] * 40

        def close(self) -> None:
            return None

    additional_input = OneChunkInput()
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: additional_input,
        flush_sample_threshold=200,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    assert additional_input.read_once.wait(timeout=1)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(recorder._detector_writer, "_flush_locked", fail_final_flush)  # pyright: ignore[reportPrivateUsage] - detector writer-boundary witness
    with pytest.raises(OSError, match="final flush"):
        evidence.close()
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.state, detector.failure) == ("failed", "close_unconfirmed")
    # The raised detector close does not claim that a peer is healthy: its
    # existing owner reports the unconfirmed terminal disposition.
    assert (additional.state, additional.failure) == ("closing", "close_unconfirmed")


def test_independent_read_failure_is_attributed_to_only_its_opened_peer(tmp_path: Path) -> None:
    """A later peer read error fences that peer without stopping detector recording."""

    class FailingAfterFirstRead(AudioInput):
        def __init__(self) -> None:
            self.first_read = Event()
            self.failed = Event()
            self._reads = 0

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            self._reads += 1
            if self._reads == 1:
                self.first_read.set()
                return [0.0] * 40
            self.failed.set()
            raise OSError("synthetic peer read failure")

        def close(self) -> None:
            return None

    peer_input = FailingAfterFirstRead()
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: peer_input,
        flush_sample_threshold=200,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    assert peer_input.first_read.wait(timeout=1)
    assert peer_input.failed.wait(timeout=1)
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, peer = snapshot.inputs
    assert (detector.state, detector.failure, detector.accepted_frames) == ("closed", None, 160)
    assert (peer.state, peer.failure, peer.input_frames) == ("failed", "read_failed", 40)


@pytest.mark.parametrize(
    ("first_read_succeeds", "expected_failure"),
    ((False, "unknown"), (True, "read_failed")),
)
def test_pipeline_reader_failure_requires_a_prior_detector_open_observation(
    first_read_succeeds: bool, expected_failure: str
) -> None:
    """The normal reader path does not over-attribute an unproved first read."""

    class CausalInput(AudioInput):
        def __init__(self) -> None:
            self._reads = 0
            self.failed = Event()

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            self._reads += 1
            if first_read_succeeds and self._reads == 1:
                return [0.0]
            self.failed.set()
            raise OSError("synthetic reader failure")

        def close(self) -> None:
            return None

    class InertRecorder:
        @property
        def started_monotonic_seconds(self) -> float | None:
            return None

        @property
        def shutdown_confirmed(self) -> bool:
            return True

        def begin(self) -> None:
            return None

        def write_samples(self, samples: Sequence[float]) -> None:
            del samples

        def close(self) -> None:
            return None

    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(4,),
    )
    recorder = EvidenceRoastRecorder(
        recorder=InertRecorder(),
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(4,),
    )
    source = CausalInput()
    pipeline = AudioCapturePipeline(
        settings=AudioCaptureSettings(input_device=None, sample_rate=4, idle_sleep_seconds=0.001),
        audio_input=source,
        recorder=recorder,
    )

    pipeline.start()
    assert source.failed.wait(timeout=1)
    pipeline.stop()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == (
        "failed",
        expected_failure,
    )


def test_additional_final_flush_failure_is_not_misreported_as_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An additional writer's final flush remains an unconfirmed close receipt."""

    class OneChunkInput(AudioInput):
        def __init__(self) -> None:
            self.read_once = Event()
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self.read_once.set()
            return [0.0] * 40

        def close(self) -> None:
            return None

    additional_input = OneChunkInput()
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: additional_input,
        flush_sample_threshold=200,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    assert additional_input.read_once.wait(timeout=1)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(
        recorder._additional_streams[0].writer,  # pyright: ignore[reportPrivateUsage]
        "_flush_locked",
        fail_final_flush,
    )
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.state, detector.failure) == ("closed", None)
    assert (additional.input_frames, additional.accepted_frames, additional.flushed_frames) == (
        40,
        40,
        0,
    )
    assert (additional.state, additional.failure) == ("failed", "close_unconfirmed")


def test_token_collision_is_unresolved_but_same_backend_is_aliased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opaque collisions disclose neither backend identity nor a false distinction."""

    def identical_token(byte_count: int) -> str:
        """Return a forced opaque token for collision testing."""
        assert byte_count == 16
        return "a" * 32

    monkeypatch.setattr(capture_evidence.secrets, "token_hex", identical_token)
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=1,
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        backend_input_id=2,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert [(row.identity, row.opened_input_token) for row in snapshot.inputs] == [
        ("unresolved", None),
        ("unresolved", None),
    ]

    assert publisher.publish_setup(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    assert publisher.open_input(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=7,
    )
    assert publisher.open_input(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        backend_input_id=7,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert [row.identity for row in snapshot.inputs] == ["aliased", "aliased"]
    assert snapshot.inputs[0].opened_input_token == snapshot.inputs[1].opened_input_token


def test_closed_result_grammar_rejects_crossed_outcomes_and_token_identity() -> None:
    """Public models reject crossed outcome/reason and token identity combinations."""
    with pytest.raises(ValidationError):
        CaptureEvidenceResult(
            capture_protocol="capture_evidence/v1",
            intent="current_session",
            outcome="refused",
            reason="store_contended",
        )
    with pytest.raises(ValidationError):
        CaptureEvidenceResult(
            capture_protocol="capture_evidence/v1",
            intent="current_session",
            outcome="unavailable",
            reason="capture_pending",
        )
    with pytest.raises(ValidationError):
        CaptureEvidenceInputResult(
            slot=1,
            role="detector_teed",
            input_generation=1,
            opened_input_token="a" * 32,
            identity="unresolved",
            requested_rate_hz=16_000,
            reported_rate_hz=None,
            wav_header_rate_hz=None,
            input_frames=0,
            accepted_frames=0,
            flushed_frames=0,
            state="capturing",
            failure=None,
        )
    for invalid_token in ("a" * 31, "a" * 33, "x" + "a" * 32, "a" * 32 + "x"):
        with pytest.raises(ValidationError):
            CaptureEvidenceInputResult(
                slot=1,
                role="detector_teed",
                input_generation=1,
                opened_input_token=invalid_token,
                identity="resolved",
                requested_rate_hz=16_000,
                reported_rate_hz=None,
                wav_header_rate_hz=None,
                input_frames=0,
                accepted_frames=0,
                flushed_frames=0,
                state="capturing",
                failure=None,
            )


def test_multi_input_recorder_publishes_actual_aliases_and_boundaries(tmp_path: Path) -> None:
    """Independent reads publish their own identity and writer-boundary counts."""

    class FakeInput(AudioInput):
        def __init__(self, frames: int, first_read: Event) -> None:
            self._frames = frames
            self._first_read = first_read
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self._first_read.set()
            return [0.0] * self._frames

        def capture_evidence_identity(self) -> tuple[int, float]:
            return (7, 16_000.0)

        def close(self) -> None:
            return None

    first_read = Event()
    inputs = iter((FakeInput(4_000, first_read),))
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: next(inputs),
        flush_sample_threshold=1,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=7, reported_rate_hz=16_000.0)
    evidence.write_samples([0.0] * 1_600)
    assert first_read.wait(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if (
            snapshot is not False
            and snapshot is not None
            and snapshot.inputs[1].input_frames == 4_000
        ):
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for independently captured evidence.")
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.identity, additional.identity) == ("aliased", "aliased")
    assert detector.opened_input_token == additional.opened_input_token
    assert (detector.input_frames, additional.input_frames) == (1_600, 4_000)
    assert (detector.accepted_frames, additional.accepted_frames) == (1_600, 4_000)
    assert (detector.flushed_frames, additional.flushed_frames) == (1_600, 4_000)


class _EventGatedInput(AudioInput):
    """Causal fake input that can block either its first or second read."""

    def __init__(
        self, *, first_chunk: Sequence[float], blocked_chunk: Sequence[float] = ()
    ) -> None:
        self._first_chunk = tuple(first_chunk)
        self._blocked_chunk = tuple(blocked_chunk)
        self._release = Event()
        self.read_started = Event()
        self.closed = Event()
        self._read_count = 0
        self.close_error: Exception | None = None

    def read_samples(self, sample_count: int) -> Sequence[float]:
        del sample_count
        self._read_count += 1
        if self._read_count == 1 and self._first_chunk:
            return self._first_chunk
        self.read_started.set()
        assert self._release.wait(timeout=1.0)
        return self._blocked_chunk

    def release(self) -> None:
        """Allow the current blocked reader call to return."""
        self._release.set()

    def close(self) -> None:
        """Record the reader-owned close without changing read behaviour."""
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


def _evidence_pipeline(
    tmp_path: Path, audio_input: AudioInput, *, multi_device: bool = False
) -> tuple[AudioCapturePipeline, CaptureEvidencePublisher]:
    """Build one real-WAV detector pipeline with bounded evidence enabled."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    if multi_device:
        recorder = MultiDeviceRoastRecorder(
            detector_wav_path=tmp_path / "detector.wav",
            detector_device_label=None,
            sidecar_path=tmp_path / "recording.json",
            sample_rate=16_000,
            session_id="session",
        )
    else:
        recorder = RoastAudioRecorder(
            wav_path=tmp_path / "detector.wav",
            sidecar_path=tmp_path / "recording.json",
            sample_rate=16_000,
            session_id="session",
        )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    return (
        AudioCapturePipeline(
            settings=AudioCaptureSettings(
                input_device=None,
                sample_rate=16_000,
                window_seconds=0.1,
                idle_sleep_seconds=0.001,
            ),
            audio_input=audio_input,
            recorder=evidence,
        ),
        publisher,
    )


def test_writer_finalisation_before_first_reader_observation_fences_late_open(
    tmp_path: Path,
) -> None:
    """A valid empty WAV cannot let a released first read resurrect capture."""
    audio_input = _EventGatedInput(first_chunk=(), blocked_chunk=(0.25,) * 1_600)
    pipeline, publisher = _evidence_pipeline(tmp_path, audio_input)
    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)

    pipeline.stop(timeout_seconds=0.0)
    with wave.open(str(tmp_path / "detector.wav"), "rb") as wav_file:
        assert wav_file.getnframes() == 0
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].input_generation) == ("closing", None)

    audio_input.release()
    assert audio_input.closed.wait(timeout=1.0)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.state, row.failure, row.input_generation) == ("closed", None, 1)


@pytest.mark.parametrize("multi_device", (False, True))
def test_detector_close_requires_reader_and_writer_confirmation(
    tmp_path: Path, multi_device: bool
) -> None:
    """A finalised nonempty WAV stays closing until its reader closes the input."""
    audio_input = _EventGatedInput(first_chunk=(0.25,) * 1_600)
    pipeline, publisher = _evidence_pipeline(tmp_path, audio_input, multi_device=multi_device)
    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if (
            snapshot is not False
            and snapshot is not None
            and snapshot.inputs[0].accepted_frames == 1_600
        ):
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for the detector writer receipt.")

    pipeline.stop(timeout_seconds=0.0)
    with wave.open(str(tmp_path / "detector.wav"), "rb") as wav_file:
        assert wav_file.getnframes() == 1_600
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, audio_input.closed.is_set()) == ("closing", False)

    audio_input.release()
    assert audio_input.closed.wait(timeout=1.0)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].state, snapshot.inputs[0].failure) == ("closed", None)


def test_detector_close_failure_keeps_finalised_wav_explicitly_unconfirmed(tmp_path: Path) -> None:
    """A reader-close exception cannot be upgraded by a valid WAV receipt."""
    audio_input = _EventGatedInput(first_chunk=(0.25,) * 1_600)
    audio_input.close_error = OSError("synthetic input close failure")
    pipeline, publisher = _evidence_pipeline(tmp_path, audio_input)
    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)
    pipeline.stop(timeout_seconds=0.0)

    audio_input.release()
    assert audio_input.closed.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if snapshot is not False and snapshot is not None and snapshot.inputs[0].state == "closing":
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for the failed reader-close disposition.")
    assert snapshot.inputs[0].failure == "close_unconfirmed"


def test_unconfirmed_writer_close_cannot_be_certified_by_late_reader_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader close cannot promote a WAV whose writer close was unconfirmed."""
    audio_input = _EventGatedInput(first_chunk=(0.25,) * 160)
    pipeline, publisher = _evidence_pipeline(tmp_path, audio_input)
    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if (
            snapshot is not False
            and snapshot is not None
            and snapshot.inputs[0].accepted_frames == 160
        ):
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for the detector writer receipt.")

    evidence = pipeline._recorder  # pyright: ignore[reportPrivateUsage] - causal owner witness
    assert isinstance(evidence, EvidenceRoastRecorder)
    recorder = evidence._recorder  # pyright: ignore[reportPrivateUsage] - real WAV owner witness
    assert isinstance(recorder, RoastAudioRecorder)
    wav_file = recorder._writer._wav  # pyright: ignore[reportPrivateUsage] - close boundary witness
    assert wav_file is not None

    def fail_close() -> None:
        raise OSError("synthetic WAV close failure after flush")

    monkeypatch.setattr(wav_file, "close", fail_close)
    pipeline.stop(timeout_seconds=0.0)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.state, row.failure, row.accepted_frames, row.flushed_frames) == (
        "closing",
        "close_unconfirmed",
        160,
        160,
    )

    audio_input.release()
    assert audio_input.closed.wait(timeout=1.0)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.state, row.failure, row.accepted_frames, row.flushed_frames) == (
        "closing",
        "close_unconfirmed",
        160,
        160,
    )


def test_reader_close_waits_for_final_writer_receipt_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader-first close cannot erase a paused real-WAV final receipt."""
    audio_input = _EventGatedInput(first_chunk=(0.25,) * 160)
    pipeline, publisher = _evidence_pipeline(tmp_path, audio_input)
    pipeline.start()
    assert audio_input.read_started.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if (
            snapshot is not False
            and snapshot is not None
            and snapshot.inputs[0].accepted_frames == 160
        ):
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for the detector writer receipt.")

    final_publication_started = Event()
    release_final_publication = Event()
    original_finalize = publisher.finalize_input

    def pause_final_publication(**kwargs: object) -> bool:
        final_publication_started.set()
        assert release_final_publication.wait(timeout=1.0)
        return original_finalize(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(publisher, "finalize_input", pause_final_publication)
    stop_thread = Thread(target=pipeline.stop, kwargs={"timeout_seconds": 0.0})
    stop_thread.start()
    assert final_publication_started.wait(timeout=1.0)

    audio_input.release()
    assert audio_input.closed.wait(timeout=1.0)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].state == "capturing"

    release_final_publication.set()
    stop_thread.join(timeout=1.0)
    assert not stop_thread.is_alive()
    with wave.open(str(tmp_path / "detector.wav"), "rb") as wav_file:
        assert wav_file.getnframes() == 160
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.state, row.failure, row.accepted_frames, row.flushed_frames) == (
        "closed",
        None,
        160,
        160,
    )
