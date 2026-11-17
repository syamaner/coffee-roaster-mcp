"""Bounded immutable evidence projection for manual mock capture.

This module deliberately contains no audio, filesystem, driver, or MCP code.
Producers publish small immutable rows and readers take one non-blocking copy.
"""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Lock
from typing import Literal

CaptureIdentity = Literal["unopened", "unresolved", "resolved", "aliased"]
CaptureState = Literal["pending", "capturing", "closing", "closed", "failed"]
CaptureFailure = Literal[
    "open_failed", "read_failed", "write_failed", "close_unconfirmed", "unknown"
]


@dataclass(frozen=True)
class _PendingFinalReceipt:
    """Private writer receipt retained until an input-open claim can prove it."""

    input_frames: int | None
    accepted_frames: int | None
    flushed_frames: int | None
    confirmed: bool
    failure: CaptureFailure | None
    wav_header_rate_hz: float | None


@dataclass(frozen=True)
class CaptureInputEvidence:
    """One bounded recorder-input projection, with no raw device identity."""

    slot: int
    role: Literal["detector_teed", "additional_independent"]
    requested_rate_hz: int
    input_generation: int | None = None
    # This is deliberately private to the projection.  An unopened failed
    # attempt must fence a late callback without inventing an opened public
    # generation in the wire row.
    attempted_input_generation: int | None = None
    # These receipts are intentionally private while there is no attributable
    # input-open observation.  The wire contract keeps an unopened row fully
    # null, but a same-attempt first open can later attach the writer truth.
    pending_final_receipt: _PendingFinalReceipt | None = None
    attempted_input_close_confirmed: bool | None = None
    opened_input_token: str | None = None
    identity: CaptureIdentity = "unopened"
    reported_rate_hz: float | None = None
    wav_header_rate_hz: float | None = None
    input_frames: int | None = None
    accepted_frames: int | None = None
    flushed_frames: int | None = None
    state: CaptureState = "pending"
    failure: CaptureFailure | None = None
    backend_input_id: int | None = None
    identity_collision: bool = False


@dataclass(frozen=True)
class CaptureEvidenceSnapshot:
    """Current immutable evidence belonging to exactly one session owner."""

    session_id: str
    ownership_sequence: int
    recorder_generation: int
    publication_sequence: int
    published_at_monotonic_seconds: float
    setup_failed: bool
    recorder_unavailable: bool
    inputs: tuple[CaptureInputEvidence, ...]


class _GenerationAuthority:
    """One recorder generation's immutable projection and short update lock.

    An authority is never reused after a newer setup replaces it.  Producers
    which captured an older authority can consequently only alter that old
    object; they cannot erase or poison its successor.
    """

    def __init__(self, snapshot: CaptureEvidenceSnapshot) -> None:
        self._lock = Lock()
        self._loss_lock = Lock()
        self._snapshot = snapshot
        self._terminal_lost = False
        # One bounded terminal-loss marker per current slot.  This replaces a
        # growable generation set and is protected separately from the public
        # snapshot lock so competing terminal reporters cannot lose a marker.
        self._lost_terminal_inputs: list[int | None] = [None] * len(snapshot.inputs)

    def try_snapshot(self) -> CaptureEvidenceSnapshot | Literal[False]:
        """Copy the projection once, or report contention/lost terminal state."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._terminal_lost or any(
                lost_generation is not None
                and (
                    lost_generation == (row.input_generation or row.attempted_input_generation)
                    or (
                        row.input_generation is None
                        and row.attempted_input_generation is None
                        and row.state == "pending"
                    )
                )
                for row, lost_generation in zip(
                    self._snapshot.inputs, self._lost_terminal_inputs, strict=True
                )
            ):
                return False
            return self._snapshot
        finally:
            self._lock.release()

    @property
    def snapshot(self) -> CaptureEvidenceSnapshot:
        """Return the immutable latest projection for setup ownership comparison."""
        return self._snapshot

    def try_replace(
        self,
        update: Callable[[CaptureEvidenceSnapshot], CaptureEvidenceSnapshot | None],
        *,
        terminal: bool = False,
        terminal_input: tuple[int, int] | None = None,
    ) -> bool:
        """Apply one exact-owner update without ever waiting for a reader."""
        if not self._lock.acquire(blocking=False):
            # A terminal reporter can race a winner that has already made its
            # exact receipt part of the immutable projection.  Evaluate the
            # same pure update against the projection observed after the
            # failed acquisition: no replacement means this reporter has no
            # evidence to lose, so it must not poison the generation.
            #
            # This deliberately takes no second publication-lock attempt.
            # A later winner that this observer cannot see remains a
            # conservative false-loss limitation rather than a false claim.
            if terminal and self._terminal_noop_is_already_represented(update, terminal_input):
                return False
            # The owner may not offer an older terminal receipt after a lost
            # terminal publication.  This latch is one-way and belongs only to
            # this authority, so a successor cannot inherit it.
            if terminal:
                if terminal_input is None:
                    self._terminal_lost = True
                else:
                    slot, generation = terminal_input
                    if 1 <= slot <= len(self._lost_terminal_inputs):
                        # This is a bounded, internal coordination lock only;
                        # no owner call or I/O occurs while it is held.
                        with self._loss_lock:
                            previous = self._lost_terminal_inputs[slot - 1]
                            if previous is None or generation > previous:
                                self._lost_terminal_inputs[slot - 1] = generation
            return False
        try:
            updated = update(self._snapshot)
            if updated is None:
                return False
            self._snapshot = updated
            return True
        finally:
            self._lock.release()

    def _terminal_noop_is_already_represented(
        self,
        update: Callable[[CaptureEvidenceSnapshot], CaptureEvidenceSnapshot | None],
        terminal_input: tuple[int, int] | None,
    ) -> bool:
        """Return whether a contended terminal no-op is already safely represented.

        A pending or capturing row is not a terminal receipt.  In particular,
        it cannot prove that a close which lost the publisher lock was
        redundant: the matching open could publish immediately afterwards.
        """
        snapshot = self._snapshot
        if update(snapshot) is not None:
            return False
        if terminal_input is None:
            # Snapshot-wide setup dispositions are monotonic. A no-op here is
            # consequently an already represented repeat, not a lost first
            # disposition.
            return True

        slot, generation = terminal_input
        if not 1 <= slot <= len(snapshot.inputs):
            return False
        row = snapshot.inputs[slot - 1]
        observed_generation = row.input_generation or row.attempted_input_generation
        if observed_generation is not None and observed_generation > generation:
            # The per-slot loss fence for this older generation cannot mask a
            # later generation, so that successor independently makes this
            # stale marker harmless.
            return True
        return observed_generation == generation and row.state in {"closing", "closed", "failed"}


class CaptureEvidencePublisher:
    """Own the current manual-capture projection behind a short publication lock."""

    def __init__(self, *, monotonic_now: Callable[[], float] | None = None) -> None:
        """Create an empty publisher with an injectable monotonic clock."""
        # Setup serialisation is deliberately independent from all reader and
        # recorder-progress locks.  In particular, a public read cannot make a
        # completed runtime setup silently remain pending.
        self._setup_lock = Lock()
        self._monotonic_now = monotonic_now or time.monotonic
        self._authority: _GenerationAuthority | None = None

    def try_snapshot(self) -> CaptureEvidenceSnapshot | None | Literal[False]:
        """Return one immutable snapshot, or ``False`` when publication contends."""
        # Reference replacement is one assignment of a fully constructed
        # authority.  Retaining the local reference pins this read to one
        # generation without sharing a lock with setup or producer updates.
        authority = self._authority
        return None if authority is None else authority.try_snapshot()

    def publish_setup(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        requested_rates_hz: tuple[int, ...],
    ) -> bool:
        """Publish a new recorder generation only for a newer store owner."""
        if not requested_rates_hz or len(requested_rates_hz) > 8:
            return False
        if any(type(rate) is not int or rate <= 0 for rate in requested_rates_hz):
            return False
        # A setup installer may wait only for another setup installer, never
        # for a reader or a recorder progress publication.
        with self._setup_lock:
            authority = self._authority
            current = None if authority is None else authority.snapshot
            if current is not None and (
                ownership_sequence < current.ownership_sequence
                or (
                    ownership_sequence == current.ownership_sequence
                    and recorder_generation <= current.recorder_generation
                )
            ):
                return False
            inputs = tuple(
                CaptureInputEvidence(
                    slot=index + 1,
                    role="detector_teed" if index == 0 else "additional_independent",
                    requested_rate_hz=rate,
                )
                for index, rate in enumerate(requested_rates_hz)
            )
            snapshot = CaptureEvidenceSnapshot(
                session_id=session_id,
                ownership_sequence=ownership_sequence,
                recorder_generation=recorder_generation,
                publication_sequence=1 if current is None else current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
                setup_failed=False,
                recorder_unavailable=False,
                inputs=inputs,
            )
            # Installation is atomic at the authority reference.  Its fresh
            # per-generation lock is not shared with an old recorder.
            self._authority = _GenerationAuthority(snapshot)
            return True

    def publish_setup_failure(
        self, *, session_id: str, ownership_sequence: int, recorder_generation: int
    ) -> None:
        """Publish an attributable setup failure without inventing input rows."""
        self._replace_if_current(
            session_id=session_id,
            ownership_sequence=ownership_sequence,
            recorder_generation=recorder_generation,
            setup_failed=True,
        )

    def publish_recorder_unavailable(
        self, *, session_id: str, ownership_sequence: int, recorder_generation: int
    ) -> None:
        """Publish a recorder-unavailable disposition for the current owner."""
        self._replace_if_current(
            session_id=session_id,
            ownership_sequence=ownership_sequence,
            recorder_generation=recorder_generation,
            recorder_unavailable=True,
        )

    def open_input(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        input_generation: int,
        backend_input_id: int | None = None,
        reported_rate_hz: float | None = None,
        wav_header_rate_hz: float | None = None,
    ) -> bool:
        """Publish one successfully opened input, rejecting stale reopen claims."""

        # Entropy generation is intentionally outside the publication lock.
        # The token is only a candidate: collision resolution happens while
        # atomically installing the immutable row set below.
        candidate_token = secrets.token_hex(16)

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            valid_id = type(backend_input_id) is int and backend_input_id >= 0
            valid_rate = _positive_finite(reported_rate_hz)
            valid_header = _positive_finite(wav_header_rate_hz)
            known_generation = row.input_generation or row.attempted_input_generation or 0
            if input_generation < known_generation:
                return None
            if input_generation == known_generation:
                # A definitive terminal disposition without an attributable
                # open is a fence, not an invitation for a delayed callback
                # to manufacture an opened generation.
                if row.state in {"closed", "failed"}:
                    return None
                if row.state == "closing":
                    # Writer finalisation can win before the reader's first
                    # observation. Record the late actual input proof, but do
                    # not let it reopen a finalised attempt as capturing.
                    receipt = row.pending_final_receipt
                    if (
                        valid_id
                        and row.backend_input_id is not None
                        and row.backend_input_id != backend_input_id
                    ):
                        return None
                    state: CaptureState = "closing"
                    failure: CaptureFailure | None = row.failure or "close_unconfirmed"
                    if receipt is not None:
                        if receipt.failure not in {None, "close_unconfirmed"}:
                            state = "failed"
                            failure = receipt.failure
                        elif receipt.confirmed and row.attempted_input_close_confirmed is not False:
                            state = "closed"
                            failure = None
                    # A late observation may fill only evidence that this
                    # owned attempt has not already proved.  In particular,
                    # an absent or contradictory backend observation cannot
                    # erase a resolved identity or its collision fence.
                    has_proved_identity = row.backend_input_id is not None
                    return replace(
                        row,
                        input_generation=input_generation,
                        opened_input_token=(
                            row.opened_input_token
                            if has_proved_identity
                            else (candidate_token if valid_id else None)
                        ),
                        identity=(
                            row.identity
                            if has_proved_identity
                            else ("unresolved" if not valid_id else "resolved")
                        ),
                        reported_rate_hz=(
                            row.reported_rate_hz
                            if row.reported_rate_hz is not None or not valid_rate
                            else reported_rate_hz
                        ),
                        wav_header_rate_hz=(
                            row.wav_header_rate_hz
                            if row.wav_header_rate_hz is not None
                            else (
                                wav_header_rate_hz
                                if valid_header
                                else (
                                    receipt.wav_header_rate_hz
                                    if receipt is not None
                                    else row.wav_header_rate_hz
                                )
                            )
                        ),
                        input_frames=(
                            receipt.input_frames if receipt is not None else row.input_frames
                        ),
                        accepted_frames=(
                            receipt.accepted_frames if receipt is not None else row.accepted_frames
                        ),
                        flushed_frames=(
                            receipt.flushed_frames if receipt is not None else row.flushed_frames
                        ),
                        state=state,
                        failure=failure,
                        backend_input_id=(
                            row.backend_input_id
                            if has_proved_identity
                            else backend_input_id
                            if valid_id
                            else None
                        ),
                        identity_collision=row.identity_collision,
                    )
                # A generic processor may have published an unresolved row
                # before the actual PortAudio owner reports its proved input.
                # Only that proved identity may refine the same generation.
                if valid_id and row.backend_input_id is None:
                    return replace(
                        row,
                        opened_input_token=candidate_token,
                        identity="resolved",
                        reported_rate_hz=reported_rate_hz if valid_rate else None,
                        wav_header_rate_hz=(
                            wav_header_rate_hz if valid_header else row.wav_header_rate_hz
                        ),
                        backend_input_id=backend_input_id,
                    )
                if (
                    valid_header
                    and row.wav_header_rate_hz is None
                    and (not valid_id or row.backend_input_id == backend_input_id)
                ):
                    return replace(row, wav_header_rate_hz=wav_header_rate_hz)
                if not valid_id or row.backend_input_id is not None:
                    return None
            return replace(
                row,
                input_generation=input_generation,
                attempted_input_generation=input_generation,
                # Private receipts and close confirmation belong only to the
                # exact input generation that produced them.  A successful
                # reopen starts a new attempt, so it must not inherit an
                # earlier attempt's terminal disposition.
                pending_final_receipt=None,
                attempted_input_close_confirmed=None,
                opened_input_token=candidate_token if valid_id else None,
                identity="unresolved" if not valid_id else "resolved",
                reported_rate_hz=reported_rate_hz if valid_rate else None,
                wav_header_rate_hz=wav_header_rate_hz if valid_header else None,
                input_frames=0,
                accepted_frames=0,
                flushed_frames=0,
                state="capturing",
                failure=None,
                backend_input_id=backend_input_id if valid_id else None,
                identity_collision=False,
            )

        return self._update_row(session_id, ownership_sequence, recorder_generation, slot, update)

    def publish_progress(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        input_generation: int,
        input_frames: int | None,
        accepted_frames: int | None,
        flushed_frames: int | None,
        wav_header_rate_hz: float | None = None,
    ) -> bool:
        """Publish source-owned absolute totals at one evidence boundary.

        Callers retain their authoritative totals across a missed nonblocking
        publication.  A later publication can therefore restore an exact
        cumulative value rather than turning a lost delta into false evidence.
        """
        totals = (input_frames, accepted_frames, flushed_frames)
        if any(total is not None and (type(total) is not int or total < 0) for total in totals):
            return False
        if (
            accepted_frames is not None
            and flushed_frames is not None
            and flushed_frames > accepted_frames
        ):
            return False

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            if row.input_generation != input_generation or row.state != "capturing":
                return None
            updated_totals = _monotonic_totals(row, totals)
            if updated_totals is None:
                return None
            header_rate = row.wav_header_rate_hz
            if wav_header_rate_hz is not None and _positive_finite(wav_header_rate_hz):
                header_rate = float(wav_header_rate_hz)
            return replace(
                row,
                input_frames=updated_totals[0],
                accepted_frames=updated_totals[1],
                flushed_frames=updated_totals[2],
                wav_header_rate_hz=header_rate,
            )

        return self._update_row(session_id, ownership_sequence, recorder_generation, slot, update)

    def close_input(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        input_generation: int,
        confirmed: bool,
    ) -> None:
        """Publish owner-observed input close, preserving partial finalisation."""

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            if row.state in {"closed", "failed"}:
                return None
            if row.state == "pending":
                return replace(
                    row,
                    attempted_input_generation=input_generation,
                    attempted_input_close_confirmed=confirmed,
                    state="closing",
                    failure=None if confirmed else "close_unconfirmed",
                )
            if row.input_generation is None:
                if row.attempted_input_generation != input_generation:
                    return None
                receipt = row.pending_final_receipt
                settled = confirmed and receipt is not None and receipt.confirmed
                return replace(
                    row,
                    attempted_input_close_confirmed=confirmed,
                    state="closed" if settled else "closing",
                    failure=None if settled else row.failure or "close_unconfirmed",
                )
            if row.input_generation != input_generation:
                return None
            if row.state not in {"capturing", "closing"}:
                return None
            return replace(
                row,
                state="closed" if confirmed else "closing",
                failure=None if confirmed else row.failure or "close_unconfirmed",
            )

        self._update_row(
            session_id,
            ownership_sequence,
            recorder_generation,
            slot,
            update,
            must_publish=True,
            terminal_input_generation=input_generation,
        )

    def finalize_input(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        input_generation: int,
        input_frames: int | None,
        accepted_frames: int | None,
        flushed_frames: int | None,
        confirmed: bool,
        failure: CaptureFailure | None = None,
        wav_header_rate_hz: float | None = None,
    ) -> bool:
        """Atomically publish final receipts and one terminal owner disposition."""
        totals = (input_frames, accepted_frames, flushed_frames)
        if any(total is not None and (type(total) is not int or total < 0) for total in totals):
            return False
        if (
            accepted_frames is not None
            and flushed_frames is not None
            and flushed_frames > accepted_frames
        ):
            return False

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            if row.state == "pending" or (
                row.state == "closing"
                and row.input_generation is None
                and row.attempted_input_generation == input_generation
            ):
                # The writer can complete while the owning reader is still
                # blocked in its first read. Fence that exact attempt before
                # it returns, retaining its private receipt so a later exact
                # input-open proof can expose it without resurrecting capture.
                receipt = _PendingFinalReceipt(
                    input_frames=input_frames,
                    accepted_frames=accepted_frames,
                    flushed_frames=flushed_frames,
                    confirmed=confirmed,
                    failure=failure,
                    wav_header_rate_hz=(
                        float(wav_header_rate_hz)
                        if wav_header_rate_hz is not None and _positive_finite(wav_header_rate_hz)
                        else None
                    ),
                )
                definite_failure = failure not in {None, "close_unconfirmed"}
                settled = (
                    not definite_failure
                    and confirmed
                    and row.attempted_input_close_confirmed is True
                )
                return replace(
                    row,
                    attempted_input_generation=input_generation,
                    pending_final_receipt=receipt,
                    state="failed" if definite_failure else ("closed" if settled else "closing"),
                    failure=(
                        failure
                        if definite_failure
                        else (None if settled else failure or "close_unconfirmed")
                    ),
                )
            if row.input_generation != input_generation or row.state not in {"capturing", "failed"}:
                return None
            updated_totals = _terminal_totals(row, totals)
            header_rate = row.wav_header_rate_hz
            if wav_header_rate_hz is not None and _positive_finite(wav_header_rate_hz):
                header_rate = float(wav_header_rate_hz)
            if row.state == "failed":
                # A writer can fail its first flush after accepting samples,
                # then later expose definite receipts while its existing close
                # owner finalises it.  Preserve the prior terminal failure;
                # this narrow receipt update must never resurrect the stream.
                if updated_totals is None:
                    return None
                return replace(
                    row,
                    input_frames=updated_totals[0],
                    accepted_frames=updated_totals[1],
                    flushed_frames=updated_totals[2],
                    wav_header_rate_hz=header_rate,
                )
            # A valid owner terminal disposition must never leave a healthy
            # capturing row behind merely because an older or unavailable
            # receipt conflicts with the current totals. Preserve the known
            # receipts and make that uncertainty explicit instead.
            terminal_failure = failure or (
                None if confirmed and updated_totals is not None else "close_unconfirmed"
            )
            if updated_totals is None:
                return replace(row, state="failed", failure=failure or "unknown")
            return replace(
                row,
                input_frames=updated_totals[0],
                accepted_frames=updated_totals[1],
                flushed_frames=updated_totals[2],
                wav_header_rate_hz=header_rate,
                state="failed" if failure is not None else ("closed" if confirmed else "closing"),
                failure=terminal_failure,
            )

        return self._update_row(
            session_id,
            ownership_sequence,
            recorder_generation,
            slot,
            update,
            must_publish=True,
            terminal_input_generation=input_generation,
        )

    def fail_input(
        self,
        *,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        input_generation: int,
        failure: CaptureFailure,
    ) -> None:
        """Fence stale progress by publishing an attributable terminal failure."""

        coordinates = {
            "session_id": session_id,
            "ownership_sequence": ownership_sequence,
            "recorder_generation": recorder_generation,
        }
        authority = self._authority_for(coordinates)
        if authority is None:
            return
        # A confirmed close is a definitive, immutable terminal receipt.  Do
        # this check before attempting the nonblocking publication so a late
        # competing failure cannot turn lock contention into a false terminal
        # loss for an already closed row.
        if 1 <= slot <= len(authority.snapshot.inputs):
            current = authority.snapshot.inputs[slot - 1]
            if current.input_generation == input_generation and current.state == "closed":
                return

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            known_generation = row.input_generation or row.attempted_input_generation
            if known_generation not in {None, input_generation}:
                return None
            # A duplicate failure is not an updated owner disposition.  Keep
            # the first attributable reason, and never rewrite a confirmed
            # close if it won the race after the pre-publication check.
            if row.state in {"closed", "failed"}:
                return None
            return replace(
                row,
                attempted_input_generation=input_generation,
                state="failed",
                failure=failure,
            )

        self._update_row(
            session_id,
            ownership_sequence,
            recorder_generation,
            slot,
            update,
            must_publish=True,
            terminal_input_generation=input_generation,
        )

    def _replace_if_current(self, **changes: object) -> None:
        authority = self._authority_for(_ownership_coordinates(changes))
        if authority is None:
            return
        coordinates = {
            key: changes[key] for key in ("session_id", "ownership_sequence", "recorder_generation")
        }

        def update(current: CaptureEvidenceSnapshot) -> CaptureEvidenceSnapshot | None:
            if not _coordinates_match(current, coordinates):
                return None
            updates = {
                key: value
                for key, value in changes.items()
                if key in {"setup_failed", "recorder_unavailable"}
            }
            if all(getattr(current, key) == value for key, value in updates.items()):
                return None
            return replace(
                current,
                publication_sequence=current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
                **updates,
            )

        authority.try_replace(update, terminal=True)

    def _update_row(
        self,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        update: Callable[[CaptureInputEvidence], CaptureInputEvidence | None],
        *,
        must_publish: bool = False,
        terminal_input_generation: int | None = None,
    ) -> bool:
        coordinates = {
            "session_id": session_id,
            "ownership_sequence": ownership_sequence,
            "recorder_generation": recorder_generation,
        }
        authority = self._authority_for(coordinates)
        if authority is None:
            return False

        def replace_row(current: CaptureEvidenceSnapshot) -> CaptureEvidenceSnapshot | None:
            if not _coordinates_match(current, coordinates):
                return None
            if not 1 <= slot <= len(current.inputs):
                return None
            old = current.inputs[slot - 1]
            new = update(old)
            if new is None or new == old:
                return None
            inputs = list(current.inputs)
            inputs[slot - 1] = new
            inputs = _with_identity_tokens(inputs)
            return replace(
                current,
                inputs=tuple(inputs),
                publication_sequence=current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
            )

        return authority.try_replace(
            replace_row,
            terminal=must_publish,
            terminal_input=(slot, terminal_input_generation)
            if must_publish and terminal_input_generation is not None
            else None,
        )

    def _authority_for(self, values: Mapping[str, object]) -> _GenerationAuthority | None:
        """Return the exact current authority without waiting for any producer."""
        # Holding this local reference prevents a later setup replacement from
        # redirecting an old producer into its successor generation.
        authority = self._authority
        if authority is None:
            return None
        snapshot = authority.snapshot
        return authority if _coordinates_match(snapshot, values) else None


def _coordinates_match(snapshot: CaptureEvidenceSnapshot, values: Mapping[str, object]) -> bool:
    """Return whether all supplied ownership coordinates exactly match."""
    return all(getattr(snapshot, key) == value for key, value in values.items())


def _ownership_coordinates(values: Mapping[str, object]) -> dict[str, object]:
    """Extract only the immutable owner coordinates from a producer claim."""
    return {
        key: values[key]
        for key in ("session_id", "ownership_sequence", "recorder_generation")
        if key in values
    }


def _positive_finite(value: object) -> bool:
    """Return whether a non-boolean numeric value is positive and finite."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _monotonic_totals(
    row: CaptureInputEvidence, totals: tuple[int | None, int | None, int | None]
) -> tuple[int | None, int | None, int | None] | None:
    """Merge receipts without allowing a known total to regress or disappear."""
    previous = (row.input_frames, row.accepted_frames, row.flushed_frames)
    merged: list[int | None] = []
    for old, incoming in zip(previous, totals, strict=True):
        if old is not None and incoming is not None and incoming < old:
            return None
        merged.append(old if incoming is None else incoming)
    if merged[1] is not None and merged[2] is not None and merged[2] > merged[1]:
        return None
    return (merged[0], merged[1], merged[2])


def _terminal_totals(
    row: CaptureInputEvidence, totals: tuple[int | None, int | None, int | None]
) -> tuple[int | None, int | None, int | None] | None:
    """Merge final receipts without letting a late source sample erase progress.

    The detector tee and writer independently observe their boundaries.  A
    final writer receipt can therefore carry an older source total after an
    in-flight tee has already published a newer one.  Keep only that known
    source receipt; writer receipts still use the ordinary monotonic fence.
    """
    input_frames, accepted_frames, flushed_frames = totals
    if (
        row.input_frames is not None
        and input_frames is not None
        and input_frames < row.input_frames
    ):
        input_frames = row.input_frames
    return _monotonic_totals(row, (input_frames, accepted_frames, flushed_frames))


def _with_identity_tokens(rows: list[CaptureInputEvidence]) -> list[CaptureInputEvidence]:
    """Resolve current bounded backend groups without exposing their ids."""
    groups: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        if row.backend_input_id is not None:
            groups.setdefault(row.backend_input_id, []).append(index)
    tokens_to_backend_ids: dict[str, set[int]] = {}
    group_tokens: dict[int, str | None] = {}
    for backend_id, indices in groups.items():
        token = next(
            (rows[index].opened_input_token for index in indices if rows[index].opened_input_token),
            None,
        )
        group_tokens[backend_id] = token
        if token is not None:
            tokens_to_backend_ids.setdefault(token, set()).add(backend_id)
    colliding_backend_ids = {
        backend_id
        for _token, backend_ids in tokens_to_backend_ids.items()
        if len(backend_ids) > 1
        for backend_id in backend_ids
    }
    for backend_id, indices in groups.items():
        if backend_id in colliding_backend_ids or any(
            rows[index].identity_collision for index in indices
        ):
            for index in indices:
                rows[index] = replace(
                    rows[index],
                    opened_input_token=None,
                    identity="unresolved",
                    identity_collision=True,
                )
            continue
        if group_tokens[backend_id] is None:
            # This can only follow a previous unresolved collision, which is
            # retained above rather than silently reidentified.
            continue
        token = group_tokens[backend_id]
        assert token is not None
        identity: CaptureIdentity = "aliased" if len(indices) > 1 else "resolved"
        for index in indices:
            rows[index] = replace(rows[index], opened_input_token=token, identity=identity)
    return rows
