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
class CaptureInputEvidence:
    """One bounded recorder-input projection, with no raw device identity."""

    slot: int
    role: Literal["detector_teed", "additional_independent"]
    requested_rate_hz: int
    input_generation: int | None = None
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


class CaptureEvidencePublisher:
    """Own the current manual-capture projection behind a short publication lock."""

    def __init__(self, *, monotonic_now: Callable[[], float] | None = None) -> None:
        """Create an empty publisher with an injectable monotonic clock."""
        self._lock = Lock()
        self._monotonic_now = monotonic_now or time.monotonic
        self._current: CaptureEvidenceSnapshot | None = None
        # A producer must never silently lose a terminal or progress update.
        # When its bounded publication attempt contends, retain a coordinate
        # fence so readers conservatively report publication unavailability
        # until a newer recorder generation replaces it.
        # A generation whose relevant terminal update was lost cannot safely
        # offer its older projection.  This immutable latch is deliberately
        # only ever consulted for the current projection; stale producers
        # therefore cannot obscure a successor.
        self._contended_latch: frozenset[tuple[str, int, int]] = frozenset()

    def try_snapshot(self) -> CaptureEvidenceSnapshot | None | Literal[False]:
        """Return one immutable snapshot, or ``False`` when publication contends."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            current = self._current
            if current is not None and _coordinates(current) in self._contended_latch:
                return False
            return current
        finally:
            self._lock.release()

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
        if not self._lock.acquire(blocking=False):
            return False
        try:
            current = self._current
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
            self._current = CaptureEvidenceSnapshot(
                session_id=session_id,
                ownership_sequence=ownership_sequence,
                recorder_generation=recorder_generation,
                publication_sequence=1 if current is None else current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
                setup_failed=False,
                recorder_unavailable=False,
                inputs=inputs,
            )
            self._contended_latch = frozenset()
            return True
        finally:
            self._lock.release()

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
            if input_generation <= (row.input_generation or 0):
                return None
            valid_id = type(backend_input_id) is int and backend_input_id >= 0
            valid_rate = _positive_finite(reported_rate_hz)
            valid_header = _positive_finite(wav_header_rate_hz)
            return replace(
                row,
                input_generation=input_generation,
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
            return replace(
                row,
                input_frames=input_frames,
                accepted_frames=accepted_frames,
                flushed_frames=flushed_frames,
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
            if row.input_generation != input_generation or row.state == "failed":
                return None
            return replace(
                row,
                state="closed" if confirmed else "closing",
                failure=None if confirmed else "close_unconfirmed",
            )

        self._update_row(
            session_id, ownership_sequence, recorder_generation, slot, update, must_publish=True
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

        def update(row: CaptureInputEvidence) -> CaptureInputEvidence | None:
            if row.input_generation not in {None, input_generation}:
                return None
            return replace(row, state="failed", failure=failure)

        self._update_row(
            session_id, ownership_sequence, recorder_generation, slot, update, must_publish=True
        )

    def _replace_if_current(self, **changes: object) -> None:
        if not self._lock.acquire(blocking=False):
            self._mark_contended(changes, relevant=lambda _current: True)
            return
        try:
            current = self._current
            coordinates = {
                key: changes[key]
                for key in ("session_id", "ownership_sequence", "recorder_generation")
            }
            if current is None or not _coordinates_match(current, coordinates):
                return
            updates = {
                key: value
                for key, value in changes.items()
                if key in {"setup_failed", "recorder_unavailable"}
            }
            self._current = replace(
                current,
                publication_sequence=current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
                **updates,
            )
        finally:
            self._lock.release()

    def _update_row(
        self,
        session_id: str,
        ownership_sequence: int,
        recorder_generation: int,
        slot: int,
        update: Callable[[CaptureInputEvidence], CaptureInputEvidence | None],
        *,
        must_publish: bool = False,
    ) -> bool:
        if not self._lock.acquire(blocking=False):
            if must_publish:
                coordinates = {
                    "session_id": session_id,
                    "ownership_sequence": ownership_sequence,
                    "recorder_generation": recorder_generation,
                }
                self._mark_contended(
                    coordinates,
                    relevant=lambda current: (
                        1 <= slot <= len(current.inputs)
                        and update(current.inputs[slot - 1]) is not None
                    ),
                )
            return False
        try:
            current = self._current
            if current is None or not _coordinates_match(
                current,
                {
                    "session_id": session_id,
                    "ownership_sequence": ownership_sequence,
                    "recorder_generation": recorder_generation,
                },
            ):
                return False
            if not 1 <= slot <= len(current.inputs):
                return False
            old = current.inputs[slot - 1]
            new = update(old)
            if new is None:
                return False
            inputs = list(current.inputs)
            inputs[slot - 1] = new
            inputs = _with_identity_tokens(inputs)
            self._current = replace(
                current,
                inputs=tuple(inputs),
                publication_sequence=current.publication_sequence + 1,
                published_at_monotonic_seconds=self._monotonic_now(),
            )
            return True
        finally:
            self._lock.release()

    def _mark_contended(
        self,
        values: Mapping[str, object],
        *,
        relevant: Callable[[CaptureEvidenceSnapshot], bool],
    ) -> None:
        """Fence an exact producer when its nonblocking publication contends."""
        session_id = values.get("session_id")
        ownership_sequence = values.get("ownership_sequence")
        recorder_generation = values.get("recorder_generation")
        if (
            isinstance(session_id, str)
            and type(ownership_sequence) is int
            and type(recorder_generation) is int
        ):
            coordinates = {
                "session_id": session_id,
                "ownership_sequence": ownership_sequence,
                "recorder_generation": recorder_generation,
            }
            current = self._current
            if (
                current is not None
                and _coordinates_match(current, coordinates)
                and relevant(current)
            ):
                # Copy-on-write avoids a last-writer-wins marker: a delayed
                # stale producer cannot erase an already latched current loss.
                self._contended_latch = self._contended_latch | {
                    (session_id, ownership_sequence, recorder_generation)
                }


def _coordinates_match(snapshot: CaptureEvidenceSnapshot, values: Mapping[str, object]) -> bool:
    """Return whether all supplied ownership coordinates exactly match."""
    return all(getattr(snapshot, key) == value for key, value in values.items())


def _coordinates(snapshot: CaptureEvidenceSnapshot) -> tuple[str, int, int]:
    """Return the immutable ownership coordinates for a snapshot."""
    return (snapshot.session_id, snapshot.ownership_sequence, snapshot.recorder_generation)


def _positive_finite(value: object) -> bool:
    """Return whether a non-boolean numeric value is positive and finite."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


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
