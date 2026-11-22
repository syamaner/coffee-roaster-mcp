"""Roast session lifecycle and event-timeline models for RoastPilot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, RLock, current_thread
from typing import Literal, TypeVar
from uuid import uuid4

from coffee_roaster_mcp.controls import validate_control_percent

RoastPhase = Literal[
    "pre_roast",
    "roasting",
    "development",
    "dropped",
    "cooling",
    "complete",
    "fault",
]

RoastEventKind = Literal[
    "beans_added",
    "first_crack_detected",
    "beans_dropped",
    "cooling_started",
    "cooling_stopped",
    "fault",
]
EventPayloadValue = str | int | float | bool | None
_AppendTurnResult = TypeVar("_AppendTurnResult")
SessionPurpose = Literal["roast", "cold_characterisation"]
SessionPresence = Literal["none", "starting", "active", "stopped"]
DriverCommandKind = Literal[
    "control",
    "drop",
    "start_cooling",
    "stop_cooling",
    "finalisation",
    "heat_containment",
]
FixedZeroIdentityRejection = Literal[
    "unknown_session",
    "not_latest_session",
    "session_not_active",
    "session_faulted",
]
FixedZeroBusyRejection = Literal["finalisation_in_progress", "command_in_progress"]
TimingEvidenceIdentityRejection = Literal[
    "unknown_session",
    "not_latest_session",
    "session_faulted",
    "session_not_active",
    "session_purpose_not_eligible",
]
TimingEvidenceBusyRejection = Literal["finalisation_in_progress", "drop_in_progress"]
TimingEvidenceAssemblyReason = Literal[
    "session_changed_during_read",
    "finalisation_in_progress",
    "drop_in_progress",
    "clock_invalid",
    "clock_regression",
    "out_of_order_read",
    "sequence_exhausted",
    "record_invalid",
]
NormalDropAdmissionOutcome = Literal[
    "dispatch",
    "recorded",
    "in_progress",
    "ambiguous",
    "refused",
]
NormalDropSettlementOutcome = Literal["success", "cancelled", "unknown", "stale"]
NormalDropFenceState = Literal["pending", "unknown"]
NormalDropRejection = Literal[
    "invalid_identity",
    "unknown_session",
    "not_latest_session",
    "session_not_active",
    "session_faulted",
    "session_purpose_not_eligible",
    "clock_epoch_mismatch",
    "ownership_sequence_mismatch",
    "timing_evidence_missing",
    "timing_evidence_superseded",
    "finalisation_in_progress",
    "command_in_progress",
    "phase_invalid",
    "prior_drop_transition_unaccounted",
]

_SINGLETON_EVENT_KINDS: frozenset[RoastEventKind] = frozenset(
    {
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    }
)

_ALLOWED_PHASES_BY_EVENT: dict[RoastEventKind, frozenset[RoastPhase]] = {
    "beans_added": frozenset({"pre_roast"}),
    "first_crack_detected": frozenset({"roasting"}),
    "beans_dropped": frozenset({"roasting", "development"}),
    "cooling_started": frozenset({"dropped"}),
    "cooling_stopped": frozenset({"cooling"}),
    "fault": frozenset(
        {
            "pre_roast",
            "roasting",
            "development",
            "dropped",
            "cooling",
            "complete",
            "fault",
        }
    ),
}

_PHASE_PROGRESSION_ORDER: tuple[RoastPhase, ...] = (
    "pre_roast",
    "roasting",
    "development",
    "dropped",
    "cooling",
    "complete",
    "fault",
)


def _event_payload_default() -> dict[str, EventPayloadValue]:
    """Return an empty typed event payload mapping."""
    return {}


def _event_timeline_default() -> list[RoastEvent]:
    """Return an empty typed event timeline."""
    return []


def _generate_clock_epoch_id() -> str:
    """Return one opaque store-lifetime timing clock epoch identifier."""
    return uuid4().hex


def _telemetry_buffer_default() -> deque[TelemetrySample]:
    """Return an empty typed telemetry buffer."""
    return deque()


def _append_telemetry_with_limit(
    session: RoastSession,
    sample: TelemetrySample,
    *,
    max_samples: int,
    log_interval_seconds: float,
) -> None:
    """Append telemetry to one session with an explicit retention limit."""
    if max_samples < 0:
        raise ValueError("max_samples must be >= 0.")
    if (
        session.telemetry_buffer
        and sample.monotonic_seconds < session.telemetry_buffer[-1].monotonic_seconds
    ):
        raise SessionLifecycleError("Telemetry samples must be appended in timestamp order.")

    telemetry_row = _telemetry_log_row_if_due(
        session,
        sample,
        log_interval_seconds=log_interval_seconds,
    )
    if telemetry_row is not None:
        # Polling and sampler observers must not occupy the store lock while
        # an older canonical turn is stalled. The reservation is still ordered
        # and capacity-bounded; completion failure latches incomplete evidence.
        # A rejected canonical telemetry turn is recording evidence only.  It
        # must not suppress the live sample or turn a healthy driver read into
        # an emergency dispatch on the next polling tick.
        if session.canonical_writer is not None:
            with suppress(EventLogRecordingError):
                # The writer has already latched a bounded omission/incomplete
                # generation.  Keep the independently observed telemetry in
                # memory; standard export remains unavailable from that latch.
                session.canonical_writer.append_later(telemetry_row, lane="telemetry")
        else:
            _append_jsonl_log_row(session, telemetry_row)

    session.telemetry_buffer.append(sample)
    if telemetry_row is not None:
        session.last_logged_telemetry_monotonic_seconds = sample.monotonic_seconds
    while len(session.telemetry_buffer) > max_samples:
        session.telemetry_buffer.popleft()


def _append_jsonl_log_row(session: RoastSession, row: Mapping[str, object]) -> None:
    """Append one JSON row to the session's durable roast log."""
    if session.log_writer is None:
        return
    writer = session.canonical_writer
    if writer is not None and not session.canonical_writer_bypass:
        row_type = row.get("type")
        writer.append(row, lane="telemetry" if row_type == "telemetry" else "event")
        return
    path = session.log_writer.log_dir / "roast.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, sort_keys=True))
        output.write("\n")


def _append_event_log_row(session: RoastSession, event: RoastEvent) -> None:
    """Append one event row to the session's durable roast log."""
    _append_jsonl_log_row(session, _event_log_row(session, event))


def _event_log_row(session: RoastSession, event: RoastEvent) -> dict[str, object]:
    """Return the exact canonical JSONL row for one lifecycle event."""
    return {
        "session_id": session.id,
        "type": "event",
        "kind": event.kind,
        "recorded_at_utc": event.recorded_at_utc.isoformat(),
        "monotonic_seconds": event.monotonic_seconds,
        "payload": dict(event.payload),
    }


class EventLogRecordingError(OSError):
    """An event's validated append failed before its history could be recorded."""


class _CanonicalAdmissionRefusedError(EventLogRecordingError):
    """A canonical row was refused before its writer was dispatched."""

    def __init__(self, message: str, *, known_zero: bool = False) -> None:
        """Create refusal evidence for one unsubmitted canonical row group."""
        super().__init__(message)
        self.known_zero = known_zero


_APPEND_FAILURE_TAIL_BYTES = 4096
_CANONICAL_TELEMETRY_ROW_LIMIT = 64
_CANONICAL_EVENT_ROW_LIMIT = 16
_CANONICAL_LANE_BYTE_LIMIT = 64 * 1024


class _CanonicalGenerationWriter:
    """One bounded, ordered JSONL writer for one configured mock generation.

    The writer deliberately owns filesystem identity and completion evidence.
    Store callers may reserve work while holding the store lock, but filesystem
    work is always performed by this worker.  Capacity includes a running turn
    so a held append cannot manufacture unbounded queued work.
    """

    def __init__(self, *, session: RoastSession) -> None:
        self._session = session
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canonical-log")
        self._lock = Lock()
        self._identity: tuple[int, int] | None = None
        self._incomplete = False
        self._verified_byte_count = 0
        self._verified_digest = hashlib.sha256()
        self._telemetry_rows = 0
        self._telemetry_bytes = 0
        self._event_rows = 0
        self._event_bytes = 0

    def append(self, row: Mapping[str, object], *, lane: Literal["telemetry", "event"]) -> None:
        """Append and verify one framed row in canonical admission order."""
        future = self.submit(row, lane=lane)
        future.result()

    def submit(
        self, row: Mapping[str, object], *, lane: Literal["telemetry", "event"]
    ) -> Future[None]:
        """Reserve and submit one ordered turn without waiting for storage.

        The returned future remains owned by this writer until it settles.  A
        caller that needs a durable result must wait only after releasing its
        store or lifecycle admission lock.
        """
        frame = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if current_thread().name.startswith("canonical-log"):
            completed: Future[None] = Future()
            try:
                self._append_verified(frame)
            except Exception as error:
                self._latch_incomplete()
                completed.set_exception(error)
            else:
                completed.set_result(None)
            return completed
        return self.submit_group((row,), lane=lane)[0]

    def submit_group(
        self,
        rows: tuple[Mapping[str, object], ...],
        *,
        lane: Literal["telemetry", "event"],
    ) -> tuple[Future[None], ...]:
        """Atomically reserve and submit one ordered same-lane row group.

        Coupled canonical facts must obtain their whole bounded capacity before
        any member reaches the worker.  This is admission, not a filesystem
        transaction: each returned future still owns its individual I/O turn.
        """
        if not rows:
            return ()
        frames = tuple(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            for row in rows
        )
        self._reserve_group(lane=lane, sizes=tuple(len(frame) for frame in frames))
        futures = tuple(self._executor.submit(self._run_turn, frame) for frame in frames)
        for future, frame in zip(futures, frames, strict=True):

            def settled(completion: Future[None], *, size: int = len(frame)) -> None:
                try:
                    completion.result()
                except Exception:
                    self._latch_incomplete()
                finally:
                    self._release(lane=lane, size=size)

            future.add_done_callback(settled)
        return futures

    def _run_turn(self, frame: bytes) -> None:
        """Run one admitted turn unless an earlier turn made this generation incomplete."""
        with self._lock:
            if self._incomplete:
                raise _CanonicalAdmissionRefusedError("Canonical writer generation is incomplete.")
        try:
            self._append_verified(frame)
        except BaseException:
            # This runs on the single ordered worker, before it can take a
            # following queued turn.  Completion callbacks are bookkeeping
            # only: installing them after a submitted group must not leave a
            # window in which a later row can append behind a torn one.
            self._latch_incomplete()
            raise

    def _latch_incomplete(self) -> None:
        """Permanently reject the generation after a failed admitted turn."""
        with self._lock:
            self._incomplete = True
        # The session carries an explicit incomplete fact. It is consulted by
        # export and never authorises a driver operation.
        self._session.mock_log_torn = True

    def _reserve_group(
        self, *, lane: Literal["telemetry", "event"], sizes: tuple[int, ...]
    ) -> None:
        """Claim capacity for an inseparable same-lane canonical group."""
        count = len(sizes)
        total_size = sum(sizes)
        with self._lock:
            if self._incomplete:
                # This refusal happens before this group reserves capacity or
                # reaches the executor. It proves zero bytes for this exact
                # group, unlike an already-submitted turn rejected in
                # ``_run_turn``.
                raise _CanonicalAdmissionRefusedError(
                    "Canonical writer generation is incomplete.", known_zero=True
                )
            rows = self._telemetry_rows if lane == "telemetry" else self._event_rows
            used = self._telemetry_bytes if lane == "telemetry" else self._event_bytes
            row_limit = (
                _CANONICAL_TELEMETRY_ROW_LIMIT
                if lane == "telemetry"
                else _CANONICAL_EVENT_ROW_LIMIT
            )
            if rows + count > row_limit or used + total_size > _CANONICAL_LANE_BYTE_LIMIT:
                self._incomplete = True
                self._session.mock_log_torn = True
                raise _CanonicalAdmissionRefusedError(
                    "Canonical writer capacity is exhausted.", known_zero=True
                )
            if lane == "telemetry":
                self._telemetry_rows += count
                self._telemetry_bytes += total_size
            else:
                self._event_rows += count
                self._event_bytes += total_size

    def append_later(
        self, row: Mapping[str, object], *, lane: Literal["telemetry", "event"]
    ) -> None:
        """Reserve an ordered turn without making an observer wait for storage."""
        self.submit(row, lane=lane)

    def _reserve(self, *, lane: Literal["telemetry", "event"], size: int) -> None:
        """Claim bounded queued-plus-running capacity for one exact frame."""
        self._reserve_group(lane=lane, sizes=(size,))

    def _release(self, *, lane: Literal["telemetry", "event"], size: int) -> None:
        """Release one settled capacity claim."""
        with self._lock:
            if lane == "telemetry":
                self._telemetry_rows -= 1
                self._telemetry_bytes -= size
            else:
                self._event_rows -= 1
                self._event_bytes -= size

    def _append_verified(self, frame: bytes) -> None:
        """Append one complete frame and prove its identity and exact bytes."""
        try:
            if self._session.log_writer is None:
                raise EventLogRecordingError("Canonical record has no log target.")
            path = self._session.log_writer.log_dir / "roast.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_APPEND | os.O_WRONLY
            with self._lock:
                first_creation = self._identity is None
            if first_creation:
                # A generation owns its canonical target only by creating it.
                # Never adopt an occupied target, even one containing valid JSONL:
                # its bytes and identity belong to somebody else.
                flags |= os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise EventLogRecordingError("Canonical log target is not a regular file.")
                identity = (before.st_dev, before.st_ino)
                with self._lock:
                    if self._identity is None:
                        self._identity = identity
                    elif self._identity != identity:
                        raise EventLogRecordingError("Canonical log identity changed.")
                offset = before.st_size
                written = 0
                while written < len(frame):
                    count = os.write(descriptor, frame[written:])
                    if count <= 0:
                        raise EventLogRecordingError("Canonical log write was incomplete.")
                    written += count
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                if (after.st_dev, after.st_ino) != identity or after.st_size != offset + len(frame):
                    raise EventLogRecordingError("Canonical log identity or size changed.")
            finally:
                os.close(descriptor)
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            reader = os.open(path, read_flags)
            try:
                checked = os.fstat(reader)
                if (checked.st_dev, checked.st_ino) != identity:
                    raise EventLogRecordingError(
                        "Canonical log identity changed during verification."
                    )
                os.lseek(reader, offset, os.SEEK_SET)
                if os.read(reader, len(frame)) != frame:
                    raise EventLogRecordingError("Canonical log frame verification failed.")
            finally:
                os.close(reader)
            with self._lock:
                self._verified_byte_count += len(frame)
                self._verified_digest.update(frame)
        except EventLogRecordingError:
            raise
        except OSError as error:
            # The writer owns every syscall from target establishment through
            # read-back verification. Driver OSErrors never enter this boundary.
            raise EventLogRecordingError("Canonical log storage or verification failed.") from error

    def shutdown(self) -> None:
        """Stop accepting new turns without cancelling filesystem ownership."""
        self._executor.shutdown(wait=False, cancel_futures=False)

    def wait_for_idle(self, *, timeout_seconds: float) -> bool:
        """Wait for every admitted turn to settle without cancelling it.

        This is deliberately a writer-owned settlement observation.  It is
        used only by the mock replay publisher after lifecycle admission has
        finished; it has no driver or lifecycle side effects.
        """
        if timeout_seconds < 0:
            return False
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                if self._telemetry_rows == 0 and self._event_rows == 0:
                    return not self._incomplete
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, 0.005))

    def read_sealed_bytes(self) -> bytes:
        """Return the verified raw JSONL bytes for an idle owned generation."""
        if self._session.log_writer is None:
            raise EventLogRecordingError("Canonical record has no log target.")
        path = self._session.log_writer.log_dir / "roast.jsonl"
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            with self._lock:
                identity = self._identity
                incomplete = self._incomplete
                expected_byte_count = self._verified_byte_count
                expected_digest = self._verified_digest.copy()
            if identity is None or incomplete:
                raise EventLogRecordingError("Canonical log identity is not sealable.")
            descriptor = os.open(path, flags)
            try:
                checked = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(checked.st_mode)
                    or (checked.st_dev, checked.st_ino) != identity
                ):
                    raise EventLogRecordingError("Canonical log identity is not sealable.")
                chunks: list[bytes] = []
                actual_digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    actual_digest.update(chunk)
                after = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or (after.st_dev, after.st_ino) != identity
                    or sum(map(len, chunks)) != expected_byte_count
                    or actual_digest.digest() != expected_digest.digest()
                ):
                    raise EventLogRecordingError("Canonical log integrity changed during sealing.")
                with self._lock:
                    if self._verified_byte_count != expected_byte_count:
                        raise EventLogRecordingError("Canonical log changed during sealing.")
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        except EventLogRecordingError:
            raise
        except OSError as error:
            raise EventLogRecordingError("Canonical log storage or verification failed.") from error


@dataclass(frozen=True)
class _AppendFailureEvidence:
    """Bounded pre-write evidence for one mock Drop-window log append."""

    path: Path | None
    identity: tuple[int, int, int, int, int] | None
    complete_tail: bool


def _capture_append_failure_evidence(session: RoastSession) -> _AppendFailureEvidence:
    """Capture bounded pre-write evidence without reading roast-log history."""
    if session.log_writer is None:
        return _AppendFailureEvidence(path=None, identity=None, complete_tail=True)
    path = session.log_writer.log_dir / "roast.jsonl"
    try:
        file_status = path.lstat()
    except FileNotFoundError:
        return _AppendFailureEvidence(path=path, identity=None, complete_tail=True)
    except OSError:
        return _AppendFailureEvidence(path=path, identity=None, complete_tail=False)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_size < 0:
        return _AppendFailureEvidence(path=path, identity=None, complete_tail=False)
    identity = (
        file_status.st_dev,
        file_status.st_ino,
        file_status.st_size,
        file_status.st_mtime_ns,
        file_status.st_ctime_ns,
    )
    tail_size = min(file_status.st_size, _APPEND_FAILURE_TAIL_BYTES)
    open_flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, open_flags)
    except OSError:
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
    try:
        opened_status = os.fstat(descriptor)
        opened_identity = (
            opened_status.st_dev,
            opened_status.st_ino,
            opened_status.st_size,
            opened_status.st_mtime_ns,
            opened_status.st_ctime_ns,
        )
        if not stat.S_ISREG(opened_status.st_mode) or opened_identity != identity:
            return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
        os.lseek(descriptor, opened_status.st_size - tail_size, os.SEEK_SET)
        tail = os.read(descriptor, tail_size)
    except (OSError, ValueError):
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
    finally:
        os.close(descriptor)
    if len(tail) != tail_size or (tail and not tail.endswith(b"\n")):
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
    if opened_status.st_size == 0:
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=True)
    final_line_start = tail.rfind(b"\n", 0, len(tail) - 1) + 1
    if final_line_start == 0 and file_status.st_size > tail_size:
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
    final_line = tail[final_line_start:-1]
    if not final_line:
        return _AppendFailureEvidence(path=path, identity=identity, complete_tail=False)
    try:
        complete_tail = isinstance(json.loads(final_line), dict)
    except Exception:  # noqa: BLE001 - malformed nested JSON must fail closed.
        complete_tail = False
    return _AppendFailureEvidence(path=path, identity=identity, complete_tail=complete_tail)


def _append_failure_may_have_damaged_log(evidence: _AppendFailureEvidence) -> bool:
    """Fail closed unless a failed append left known-clean pre-write bytes unchanged."""
    if evidence.path is None:
        return False
    if not evidence.complete_tail:
        return True
    try:
        file_status = evidence.path.lstat()
    except FileNotFoundError:
        return evidence.identity is not None
    except OSError:
        return True
    if evidence.identity is None or not stat.S_ISREG(file_status.st_mode):
        return True
    identity = (
        file_status.st_dev,
        file_status.st_ino,
        file_status.st_size,
        file_status.st_mtime_ns,
        file_status.st_ctime_ns,
    )
    return identity != evidence.identity


def _append_event_log_row_with_failure_evidence(session: RoastSession, event: RoastEvent) -> None:
    """Append one protected row and expose bounded evidence if its write raises."""
    if session.canonical_writer is not None:
        # Exact Drop, its paired cooling row, and a queued emergency share the
        # configured generation's ordered worker.  These callers already run
        # their turn outside the store lock; bypassing that worker would let a
        # later protected row overtake an admitted canonical row.
        try:
            _append_event_log_row(session, event)
        except Exception as error:
            # A bounded-lane refusal occurs before the executor receives this
            # row. It makes the generation incomplete, but proves this exact
            # endpoint append wrote zero bytes. Keep its unknown command fence
            # without relabelling the raw log as torn.
            if isinstance(error, _CanonicalAdmissionRefusedError):
                raise
            raise _MockDropAppendMayBeTornError from error
        return

    evidence = _capture_append_failure_evidence(session)
    session.canonical_writer_bypass = True
    try:
        _append_event_log_row(session, event)
    except Exception as error:
        if _append_failure_may_have_damaged_log(evidence):
            raise _MockDropAppendMayBeTornError from error
        raise
    finally:
        session.canonical_writer_bypass = False


class _MockDropAppendMayBeTornError(Exception):
    """A protected mock Drop-window append failed after ambiguous file evidence."""


def _telemetry_log_row_if_due(
    session: RoastSession,
    sample: TelemetrySample,
    *,
    log_interval_seconds: float,
) -> Mapping[str, object] | None:
    """Return one telemetry log row when the configured logging interval is due."""
    last_logged = session.last_logged_telemetry_monotonic_seconds
    if last_logged is not None and sample.monotonic_seconds - last_logged < log_interval_seconds:
        return None
    return {
        "session_id": session.id,
        "type": "telemetry",
        "recorded_at_utc": sample.recorded_at_utc.isoformat(),
        "monotonic_seconds": sample.monotonic_seconds,
        "bean_temp_c": sample.bean_temp_c,
        "env_temp_c": sample.env_temp_c,
        "heat_level_percent": sample.heat_level_percent,
        "fan_level_percent": sample.fan_level_percent,
        "cooling_on": sample.cooling_on,
    }


@dataclass(frozen=True)
class LogWriterReference:
    """Reference to the append-only roast log target.

    Attributes:
        session_id: Session identifier that owns the log target.
        log_dir: Base directory for this session's log exports.
    """

    session_id: str
    log_dir: Path


@dataclass(frozen=True)
class RoastEvent:
    """Recorded roast event placeholder for the authoritative session timeline.

    Attributes:
        kind: Event type.
        recorded_at_utc: Wall-clock timestamp when the event was recorded.
        monotonic_seconds: Monotonic seconds since session start.
        payload: Optional structured event details.
    """

    kind: RoastEventKind
    recorded_at_utc: datetime
    monotonic_seconds: float
    payload: dict[str, EventPayloadValue] = field(default_factory=_event_payload_default)


@dataclass(frozen=True)
class TelemetrySample:
    """Normalized telemetry sample placeholder for later metrics work.

    Attributes:
        recorded_at_utc: Wall-clock timestamp for the sample.
        monotonic_seconds: Monotonic seconds since session start.
        bean_temp_c: Optional normalized bean temperature.
        env_temp_c: Optional normalized environment temperature.
        heat_level_percent: Optional heat control level.
        fan_level_percent: Optional fan control level.
        cooling_on: Optional cooling state.
    """

    recorded_at_utc: datetime
    monotonic_seconds: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    heat_level_percent: int | None = None
    fan_level_percent: int | None = None
    cooling_on: bool | None = None


@dataclass(frozen=True)
class SessionStartReservation:
    """Reservation for preparing a driver before creating a session."""

    token: str


@dataclass
class StagedSessionStartPublication:
    """Caller-owned identity result for one staged session publication."""

    session: RoastSession | None


@dataclass(frozen=True)
class CaptureSubject:
    """Nonblocking identity sample for a capture-evidence read.

    ``None`` from :meth:`RoastSessionStore.try_get_capture_subject` means the
    store lock contended. A ``presence`` of ``none`` is an acquired empty sample.
    """

    presence: SessionPresence
    session_id: str | None
    ownership_sequence: int | None


@dataclass(frozen=True)
class DriverCommandReservation:
    """Reservation for one non-emergency driver command.

    Attributes:
        session_id: Session that owns the reservation.
        token: Opaque reservation token.
        kind: Reserved command kind.
    """

    session_id: str
    token: str
    kind: DriverCommandKind


@dataclass(frozen=True)
class FixedZeroContainmentAdmission:
    """Locked exact-session admission for one fixed-zero containment command."""

    session: RoastSession | None
    reservation: DriverCommandReservation | None
    identity_rejection: FixedZeroIdentityRejection | None = None
    busy_rejection: FixedZeroBusyRejection | None = None


@dataclass(frozen=True)
class DriverDropReservation:
    """Result of reserving the driver drop command.

    Attributes:
        reservation: Reservation to complete when a new drop command is needed.
        existing_event: Existing singleton drop event for idempotent retries.
        snapshot: Session snapshot returned for idempotent retries.
    """

    reservation: DriverCommandReservation | None
    existing_event: RoastEvent | None = None
    snapshot: RoastSession | None = None


@dataclass(frozen=True)
class DriverEventReservation:
    """Result of reserving a driver command that records a singleton event."""

    reservation: DriverCommandReservation | None
    existing_event: RoastEvent | None = None
    snapshot: RoastSession | None = None


@dataclass(frozen=True)
class NormalDropIdentity:
    """Exact store identity required to admit one normal bean-drop operation.

    A session id is intentionally insufficient: the process clock epoch,
    session ownership sequence, and timing-evidence sequence bind this value to
    one live store-owned command context.
    """

    session_id: str
    clock_epoch_id: str
    ownership_sequence: int
    timing_evidence_sequence: int


@dataclass(frozen=True)
class NormalDropReservation:
    """Owned reservation and in-flight fence for one normal bean-drop command."""

    identity: NormalDropIdentity
    token: str


@dataclass(frozen=True)
class NormalDropNonDispatchProof:
    """Positive mock-driver proof that one exact Drop did not actuate.

    This is intentionally narrower than an emergency, a cleared ordinary
    token, a timeout, or a driver exception.  The mock driver's atomic
    emergency-before-actuation transition is the only producer of this proof;
    it binds its zero-actuation observation to the exact store reservation.
    The store validates that binding again before releasing admission.
    """

    reservation: NormalDropReservation
    zero_actuation: Literal[True]


@dataclass(frozen=True)
class NormalDropAdmission:
    """Closed store-side result of normal bean-drop admission."""

    outcome: NormalDropAdmissionOutcome
    reservation: NormalDropReservation | None = None
    existing_event: RoastEvent | None = None
    snapshot: RoastSession | None = None
    rejection: NormalDropRejection | None = None


@dataclass(frozen=True)
class NormalDropSettlement:
    """Closed store-side result of settling a normal bean-drop operation."""

    outcome: NormalDropSettlementOutcome
    event: RoastEvent | None = None
    snapshot: RoastSession | None = None


@dataclass(frozen=True)
class MockDropPersistenceTicket:
    """Private ordered append turn for the exact mock Drop persistence window."""

    sequence: int
    previous_completion: Event
    completion: Event


@dataclass(frozen=True)
class ManualFirstCrackAttempt:
    """One owned manual-first-crack write attempt.

    The attempt is deliberately narrower than a session event: until its
    writer verifies the canonical row, it cannot change the live roast phase.
    """

    token: str
    started_monotonic_seconds: float
    deadline_monotonic_seconds: float
    completion: Future[ManualFirstCrackSettlement]
    observer_publication_token: object | None = None
    on_live: Callable[[RoastSession, object], None] | None = None


@dataclass(frozen=True)
class _ManualFirstCrackAuditIdentity:
    """Immutable identity carried by every private audit row for one mark."""

    recorded_at_utc: datetime
    monotonic_seconds: float
    capture_ownership_sequence: int
    session_id: str
    writer_generation: int


@dataclass(frozen=True)
class ManualFirstCrackSettlement:
    """Closed result of one manual first-crack writer turn."""

    outcome: Literal["live", "historical", "failed", "duplicate"]
    event: RoastEvent | None = None
    snapshot: RoastSession | None = None


@dataclass(frozen=True)
class RoastMetrics:
    """Timestamp-derived roast metrics for the current session snapshot.

    Attributes:
        roast_elapsed_seconds: Seconds from beans added to drop or now.
        development_time_seconds: Seconds from first crack to drop, stop, or now.
        development_percent: Development time as a percentage of roast elapsed time.
        bean_temp_delta_60s_c: Bean-temperature delta across the rolling 60s window.
        env_temp_delta_60s_c: Environment-temperature delta across the rolling 60s window.
        bean_ror_c_per_min: Bean-temperature rate of rise in Celsius per minute.
        env_ror_c_per_min: Environment-temperature rate of rise in Celsius per minute.
    """

    roast_elapsed_seconds: float | None
    development_time_seconds: float | None
    development_percent: float | None
    bean_temp_delta_60s_c: float | None
    env_temp_delta_60s_c: float | None
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None


@dataclass
class RoastSession:
    """Authoritative in-process roast session state.

    This object is mutable and not thread-safe by itself. Runtime code should
    mutate it through `RoastSessionStore` methods so store-owned locking stays
    authoritative as concurrent MCP tool handlers are added.

    Attributes:
        id: Stable unique session identifier.
        created_at_utc: Wall-clock UTC creation time.
        monotonic_start: Monotonic clock value captured when the session starts.
        phase: Current roast phase.
        beans_added_at_utc: Wall-clock UTC timestamp for the authoritative T0 event.
        beans_added_monotonic_seconds: Monotonic elapsed seconds for T0.
        first_crack_at_utc: Wall-clock UTC timestamp for the first-crack event.
        first_crack_monotonic_seconds: Monotonic elapsed seconds for first crack.
        beans_dropped_at_utc: Wall-clock UTC timestamp for bean drop.
        beans_dropped_monotonic_seconds: Monotonic elapsed seconds for bean drop.
        cooling_started_at_utc: Wall-clock UTC timestamp for cooling start.
        cooling_started_monotonic_seconds: Monotonic elapsed seconds for cooling start.
        cooling_stopped_at_utc: Wall-clock UTC timestamp for cooling stop.
        cooling_stopped_monotonic_seconds: Monotonic elapsed seconds for cooling stop.
        faulted_at_utc: Wall-clock UTC timestamp for the first recorded fault.
        faulted_monotonic_seconds: Monotonic elapsed seconds for the first fault.
        heat_level_percent: Latest in-memory heat setting for the active mock path.
        fan_level_percent: Latest in-memory fan setting for the active mock path.
        cooling_on: Whether cooling is currently active in the session state.
        auto_t0_charge_temperature_c: Max preheat/charge bean temperature before T0.
        auto_t0_charge_temperature_monotonic_seconds: Monotonic elapsed seconds
            of the candidate turning point — the most recent local-max
            bean-temperature sample before the sustained decline. Automatic T0
            is backdated to this timestamp on confirmation (#167).
        auto_t0_current_drop_c: Current drop from the tracked charge temperature.
        auto_t0_preheat_sample_count: Number of valid pre-T0 bean-temperature samples.
        event_timeline: Shared ordered event timeline for future runtime stories.
        telemetry_buffer: Rolling telemetry sample buffer.
        log_writer: Append-only log writer reference when available.
        last_logged_telemetry_monotonic_seconds: Latest telemetry timestamp
            written to the append-only JSONL log.
        fc_window_count: Number of audio windows the first-crack detector
            processed during this session (#175 observability).
        fc_max_confidence: Highest per-window first-crack confidence seen so
            far, or `None` when no window reported a confidence.
        fc_max_positive_window_count: Highest rolling positive-window count the
            detector reached, even when confirmation was never met.
        fc_model_confirmed: Whether the audio model confirmed first crack for
            this session (false when first crack was operator-marked or never
            fired).
        stopped_at_utc: Wall-clock UTC stop time once the session is stopped.
        monotonic_stop: Monotonic clock value captured when the session stops.
    """

    id: str
    created_at_utc: datetime
    monotonic_start: float
    capture_ownership_sequence: int = field(default=0, kw_only=True, compare=False, repr=False)
    beans_added_source: Literal["manual_mark", "auto_t0"] | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    first_crack_source: Literal["manual_mark", "model_detection"] | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    timing_evidence_sequence: int = field(default=0, kw_only=True, compare=False, repr=False)
    timing_evidence_last_lo: float | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    timing_evidence_last_hi: float | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    hottop_reception_evidence_sequence: int = field(
        default=0, kw_only=True, compare=False, repr=False
    )
    hottop_reception_evidence_last_lo: float | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    hottop_reception_evidence_last_hi: float | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    purpose: SessionPurpose = "roast"
    phase: RoastPhase = "pre_roast"
    beans_added_at_utc: datetime | None = None
    beans_added_monotonic_seconds: float | None = None
    first_crack_at_utc: datetime | None = None
    first_crack_monotonic_seconds: float | None = None
    beans_dropped_at_utc: datetime | None = None
    beans_dropped_monotonic_seconds: float | None = None
    cooling_started_at_utc: datetime | None = None
    cooling_started_monotonic_seconds: float | None = None
    cooling_stopped_at_utc: datetime | None = None
    cooling_stopped_monotonic_seconds: float | None = None
    faulted_at_utc: datetime | None = None
    faulted_monotonic_seconds: float | None = None
    heat_level_percent: int = 0
    fan_level_percent: int = 0
    cooling_on: bool = False
    auto_t0_charge_temperature_c: float | None = None
    auto_t0_charge_temperature_monotonic_seconds: float | None = None
    auto_t0_current_drop_c: float | None = None
    auto_t0_preheat_sample_count: int = 0
    event_timeline: list[RoastEvent] = field(default_factory=_event_timeline_default)
    telemetry_buffer: deque[TelemetrySample] = field(default_factory=_telemetry_buffer_default)
    log_writer: LogWriterReference | None = None
    # Private mock-composition ownership. Read snapshots deliberately omit this
    # non-copyable worker; it never grants lifecycle or driver authority.
    canonical_writer: _CanonicalGenerationWriter | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    canonical_writer_bypass: bool = field(default=False, kw_only=True, compare=False, repr=False)
    last_logged_telemetry_monotonic_seconds: float | None = None
    fc_window_count: int = 0
    fc_max_confidence: float | None = None
    fc_max_positive_window_count: int = 0
    fc_model_confirmed: bool = False
    stopped_at_utc: datetime | None = None
    monotonic_stop: float | None = None
    pending_driver_command_token: str | None = None
    pending_driver_command_kind: DriverCommandKind | None = None
    # This is pre-dispatch ownership only. It deliberately says nothing about
    # whether the driver returned or changed physical state.
    ordinary_driver_drop_dispatch_owned: bool = False
    verified_ordinary_driver_drop: bool = False
    unrecorded_driver_drop: bool = False
    # This narrow evidence exists only when the coupled ordinary-Drop group
    # was refused before submission after its driver return was verified. It
    # never projects a canonical Drop row or grants another Drop attempt.
    ordinary_drop_persistence_zero: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    ordinary_drop_persistence_zero_cooling_settled: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # The concrete finalisation result is deliberately owned by mcp_server.
    # Store only a deep-copyable opaque value here to keep the lifecycle store
    # independent of the server/tool module.
    finalisation: object | None = None
    # Store-owned persistence facts travel with retained sessions so export
    # cannot lose a damage fence when a successor session starts.
    mock_log_torn: bool = field(default=False, kw_only=True, compare=False, repr=False)
    unpersisted_mock_drop_fault_count: int = field(
        default=0, kw_only=True, compare=False, repr=False
    )
    torn_start_cooling_attempted: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    unconfirmed_torn_start_cooling_attempted: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    unrecorded_fault_recovery_drop_accepted: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # These facts are intentionally local to the configured manual/mock
    # writer. They never grant a driver action or make a historical row live.
    manual_first_crack_pending: bool = field(default=False, kw_only=True, compare=False, repr=False)
    manual_first_crack_export_disqualified: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # A timed-out or uncertain singleton receipt is terminal for this exact
    # session.  It is deliberately separate from the durable-row evidence:
    # an expiry before append proves zero canonical bytes, while a later
    # expiry may preserve a verified historical row.
    manual_first_crack_admission_expired: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    manual_first_crack_canonical_evidence: Literal["none", "verified", "zero", "uncertain"] = field(
        default="none", kw_only=True, compare=False, repr=False
    )
    # This is private audit evidence, not canonical persistence evidence.  It
    # makes a spent mark visibly incomplete without treating an unsubmitted
    # canonical row as a torn canonical generation.
    manual_first_crack_audit_unresolved: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # An admitted lifecycle endpoint closes new manual-mark admission before
    # its driver result is published.  This is composition-local evidence: it
    # never grants a command or changes an already-live mark.
    manual_first_crack_endpoint_admitted: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # A manual charge accepted by the composed canonical writer cannot be
    # treated as an empty batch until its exact turn has either been verified
    # or visibly latched incomplete.  These facts never grant an action.
    manual_charge_persistence_pending: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    manual_charge_persistence_uncertain: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )
    # An admission refusal before an auto-charge turn reaches the writer is
    # known not to have produced canonical bytes. It remains distinct from an
    # executing turn whose bytes cannot be proved either way.
    manual_charge_persistence_zero: bool = field(
        default=False, kw_only=True, compare=False, repr=False
    )

    @property
    def active(self) -> bool:
        """Return whether the session is still active."""
        return self.monotonic_stop is None

    def elapsed_monotonic_seconds(
        self,
        monotonic_now: Callable[[], float],
    ) -> float:
        """Return monotonic elapsed seconds for this session.

        Args:
            monotonic_now: Monotonic clock supplier for active sessions.

        Returns:
            Elapsed monotonic seconds from session start to now or stop time.
        """
        end_value = self.monotonic_stop if self.monotonic_stop is not None else monotonic_now()
        return max(0.0, end_value - self.monotonic_start)

    def stop(
        self,
        *,
        utc_now: Callable[[], datetime],
        monotonic_now: Callable[[], float],
        phase: RoastPhase = "complete",
    ) -> None:
        """Stop the session cleanly if it is still active.

        Args:
            utc_now: Wall-clock UTC timestamp supplier.
            monotonic_now: Monotonic clock supplier.
            phase: Final phase to set when stopping the session.
        """
        if not self.active:
            return
        self.stopped_at_utc = utc_now()
        self.monotonic_stop = monotonic_now()
        self.phase = phase


@dataclass(frozen=True)
class TimingEvidenceAdmission:
    """Opaque live-session admission for one read-only timing-evidence read."""

    session: RoastSession | None
    ownership_sequence: int | None
    session_id: str | None
    identity_rejection: TimingEvidenceIdentityRejection | None = None
    busy_rejection: TimingEvidenceBusyRejection | None = None
    canonical_charge: RoastEvent | None = None
    human_first_crack: RoastEvent | None = None


@dataclass(frozen=True)
class TimingEvidenceAssembly:
    """Closed store-side outcome for a timing-evidence assembly attempt."""

    outcome: Literal["evidence", "refused_identity", "refused_busy", "unavailable"]
    reason_code: TimingEvidenceAssemblyReason | None
    result: object | None = None


def compute_roast_metrics(
    session: RoastSession,
    *,
    monotonic_now: Callable[[], float] | None = None,
    ror_window_seconds: float = 60.0,
    ror_min_sample_seconds: float = 10.0,
) -> RoastMetrics:
    """Compute timestamp-derived metrics for one session snapshot.

    Args:
        session: Session snapshot to inspect.
        monotonic_now: Optional monotonic clock supplier for active sessions.
        ror_window_seconds: Rolling telemetry window for RoR calculations.
        ror_min_sample_seconds: Minimum valid sensor sample span required for RoR.

    Returns:
        Minimal roast metrics available from the current event timestamps.
    """
    import time

    clock = monotonic_now or time.monotonic
    roast_elapsed_seconds = compute_roast_elapsed_seconds(
        session,
        monotonic_now=clock,
    )
    development_time_seconds = compute_development_time_seconds(
        session,
        monotonic_now=clock,
    )
    return RoastMetrics(
        roast_elapsed_seconds=roast_elapsed_seconds,
        development_time_seconds=development_time_seconds,
        development_percent=_compute_development_percent_from_values(
            roast_elapsed_seconds=roast_elapsed_seconds,
            development_time_seconds=development_time_seconds,
        ),
        bean_temp_delta_60s_c=compute_bean_temp_delta_60s_c(session),
        env_temp_delta_60s_c=compute_env_temp_delta_60s_c(session),
        bean_ror_c_per_min=compute_bean_ror_c_per_min(
            session,
            window_seconds=ror_window_seconds,
            min_sample_seconds=ror_min_sample_seconds,
        ),
        env_ror_c_per_min=compute_env_ror_c_per_min(
            session,
            window_seconds=ror_window_seconds,
            min_sample_seconds=ror_min_sample_seconds,
        ),
    )


def compute_roast_elapsed_seconds(
    session: RoastSession,
    *,
    monotonic_now: Callable[[], float] | None = None,
) -> float | None:
    """Compute roast elapsed seconds from authoritative T0 to drop or now.

    Args:
        session: Session snapshot to inspect.
        monotonic_now: Optional monotonic clock supplier for active sessions.

    Returns:
        Seconds from `beans_added` to `beans_dropped` when drop exists, from
        `beans_added` to the current session clock otherwise, or `None` before
        beans are added.
    """
    import time

    clock = monotonic_now or time.monotonic
    if session.beans_added_monotonic_seconds is None:
        return None
    if session.beans_dropped_monotonic_seconds is not None:
        end_seconds = session.beans_dropped_monotonic_seconds
    else:
        end_seconds = session.elapsed_monotonic_seconds(clock)
    return round(max(0.0, end_seconds - session.beans_added_monotonic_seconds), 3)


def compute_development_time_seconds(
    session: RoastSession,
    *,
    monotonic_now: Callable[[], float] | None = None,
) -> float | None:
    """Compute development seconds from first crack to drop or now.

    Args:
        session: Session snapshot to inspect.
        monotonic_now: Optional monotonic clock supplier for active sessions.

    Returns:
        Seconds from `first_crack_detected` to `beans_dropped` when drop
        exists, from `first_crack_detected` to the current session clock
        otherwise, or `None` before first crack.
    """
    import time

    clock = monotonic_now or time.monotonic
    return _elapsed_since(
        session,
        start_seconds=session.first_crack_monotonic_seconds,
        monotonic_now=clock,
    )


def compute_development_percent(
    session: RoastSession,
    *,
    monotonic_now: Callable[[], float] | None = None,
) -> float | None:
    """Compute development time as a percentage of roast elapsed time.

    Args:
        session: Session snapshot to inspect.
        monotonic_now: Optional monotonic clock supplier for active sessions.

    Returns:
        `development_time_seconds / roast_elapsed_seconds * 100`, rounded to
        three decimal places, or `None` before both values are available.
    """
    import time

    clock = monotonic_now or time.monotonic
    roast_elapsed_seconds = compute_roast_elapsed_seconds(
        session,
        monotonic_now=clock,
    )
    development_time_seconds = compute_development_time_seconds(
        session,
        monotonic_now=clock,
    )
    return _compute_development_percent_from_values(
        roast_elapsed_seconds=roast_elapsed_seconds,
        development_time_seconds=development_time_seconds,
    )


def compute_bean_temp_delta_60s_c(session: RoastSession) -> float | None:
    """Compute bean-temperature delta across the latest rolling 60-second window.

    Args:
        session: Session snapshot with retained telemetry samples.

    Returns:
        Latest minus oldest bean temperature in the 60-second window ending at
        the latest retained telemetry sample, or `None` when no bean
        temperature sample is available in that window.
    """
    return _compute_temperature_delta_60s_c(
        session,
        temperature_field="bean_temp_c",
    )


def compute_env_temp_delta_60s_c(session: RoastSession) -> float | None:
    """Compute environment-temperature delta across the latest rolling 60-second window.

    Args:
        session: Session snapshot with retained telemetry samples.

    Returns:
        Latest minus oldest environment temperature in the 60-second window
        ending at the latest retained telemetry sample, or `None` when no
        environment temperature sample is available in that window.
    """
    return _compute_temperature_delta_60s_c(
        session,
        temperature_field="env_temp_c",
    )


def compute_bean_ror_c_per_min(
    session: RoastSession,
    *,
    window_seconds: float = 60.0,
    min_sample_seconds: float = 10.0,
) -> float | None:
    """Compute bean-temperature rate of rise in Celsius per minute.

    Args:
        session: Session snapshot with retained telemetry samples.
        window_seconds: Rolling telemetry window ending at the latest sample.
        min_sample_seconds: Minimum valid bean sample span required for a value.

    Returns:
        Bean-temperature slope over the latest valid rolling sample span,
        normalized to Celsius per minute, or `None` before enough samples exist.
    """
    return _compute_temperature_ror_c_per_min(
        session,
        temperature_field="bean_temp_c",
        window_seconds=window_seconds,
        min_sample_seconds=min_sample_seconds,
    )


def compute_env_ror_c_per_min(
    session: RoastSession,
    *,
    window_seconds: float = 60.0,
    min_sample_seconds: float = 10.0,
) -> float | None:
    """Compute environment-temperature rate of rise in Celsius per minute.

    Args:
        session: Session snapshot with retained telemetry samples.
        window_seconds: Rolling telemetry window ending at the latest sample.
        min_sample_seconds: Minimum valid environment sample span required for a value.

    Returns:
        Environment-temperature slope over the latest valid rolling sample span,
        normalized to Celsius per minute, or `None` before enough samples exist.
    """
    return _compute_temperature_ror_c_per_min(
        session,
        temperature_field="env_temp_c",
        window_seconds=window_seconds,
        min_sample_seconds=min_sample_seconds,
    )


def _compute_development_percent_from_values(
    *,
    roast_elapsed_seconds: float | None,
    development_time_seconds: float | None,
) -> float | None:
    """Return development percent from precomputed elapsed values."""
    if (
        roast_elapsed_seconds is None
        or roast_elapsed_seconds <= 0
        or development_time_seconds is None
    ):
        return None
    return round((development_time_seconds / roast_elapsed_seconds) * 100, 3)


def _compute_temperature_delta_60s_c(
    session: RoastSession,
    *,
    temperature_field: Literal["bean_temp_c", "env_temp_c"],
) -> float | None:
    """Return latest minus oldest temperature in the retained 60s sample window."""
    if not session.telemetry_buffer:
        return None
    latest_sample_seconds = session.telemetry_buffer[-1].monotonic_seconds
    window_start_seconds = latest_sample_seconds - 60.0
    oldest_temp_c: float | None = None
    latest_temp_c: float | None = None
    valid_sample_count = 0
    for sample in session.telemetry_buffer:
        if sample.monotonic_seconds < window_start_seconds:
            continue
        temp_c = getattr(sample, temperature_field)
        if temp_c is None:
            continue
        valid_sample_count += 1
        if oldest_temp_c is None:
            oldest_temp_c = temp_c
        latest_temp_c = temp_c
    if oldest_temp_c is None or latest_temp_c is None or valid_sample_count < 2:
        return None
    return round(latest_temp_c - oldest_temp_c, 3)


def _compute_temperature_ror_c_per_min(
    session: RoastSession,
    *,
    temperature_field: Literal["bean_temp_c", "env_temp_c"],
    window_seconds: float,
    min_sample_seconds: float,
) -> float | None:
    """Return temperature slope over the latest rolling window as C/min."""
    if not session.telemetry_buffer:
        return None
    latest_sample_seconds = session.telemetry_buffer[-1].monotonic_seconds
    window_start_seconds = latest_sample_seconds - window_seconds
    oldest_temp_c: float | None = None
    oldest_sample_seconds: float | None = None
    latest_temp_c: float | None = None
    latest_temp_seconds: float | None = None
    for sample in session.telemetry_buffer:
        if sample.monotonic_seconds < window_start_seconds:
            continue
        temp_c = getattr(sample, temperature_field)
        if temp_c is None:
            continue
        if oldest_temp_c is None:
            oldest_temp_c = temp_c
            oldest_sample_seconds = sample.monotonic_seconds
        latest_temp_c = temp_c
        latest_temp_seconds = sample.monotonic_seconds
    if (
        oldest_temp_c is None
        or oldest_sample_seconds is None
        or latest_temp_c is None
        or latest_temp_seconds is None
    ):
        return None
    sample_span_seconds = latest_temp_seconds - oldest_sample_seconds
    if sample_span_seconds < min_sample_seconds or sample_span_seconds <= 0:
        return None
    return round(((latest_temp_c - oldest_temp_c) / sample_span_seconds) * 60.0, 3)


class SessionLifecycleError(RuntimeError):
    """Raised when a roast session lifecycle transition is invalid."""


class RecoveryPersistenceSupersededError(SessionLifecycleError):
    """A later emergency revoked a recovery reservation during its owned write."""


class RoastSessionStore:
    """Single-owner in-process roast session registry.

    This keeps the active-session ownership explicit for Epic 2 while allowing
    tests and later tool stories to resolve the latest session state cleanly.

    `RoastSession` instances returned from this store are not independently
    thread-safe. Callers should treat this store as the authoritative mutation
    boundary and use store-owned methods for lifecycle and future event or
    telemetry writes.
    """

    def __init__(
        self,
        *,
        telemetry_buffer_limit: int = 300,
        session_history_limit: int = 8,
        utc_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        default_log_dir: Path = Path("./logs/roasts"),
        telemetry_log_interval_seconds: float = 1.0,
        manual_first_crack_writer_enabled: bool = False,
        manual_first_crack_budget_seconds: float = 1.0,
    ) -> None:
        """Initialize the single-session store.

        Args:
            telemetry_buffer_limit: Maximum retained telemetry samples per session.
            session_history_limit: Maximum retained sessions addressable by id.
            utc_now: Optional UTC timestamp supplier.
            monotonic_now: Optional monotonic clock supplier.
            session_id_factory: Optional session id supplier.
            default_log_dir: Base directory for future log writer references.
            telemetry_log_interval_seconds: Minimum seconds between telemetry
                rows in the append-only JSONL log.
            manual_first_crack_writer_enabled: Enable the bounded, private
                writer used only by the composed manual/mock recording route.
            manual_first_crack_budget_seconds: Injected producer budget for
                that private writer; production composition fixes it at one.
        """
        import time

        if telemetry_buffer_limit < 0:
            raise ValueError("telemetry_buffer_limit must be >= 0.")
        if session_history_limit < 1:
            raise ValueError("session_history_limit must be >= 1.")
        if not math.isfinite(telemetry_log_interval_seconds) or telemetry_log_interval_seconds <= 0:
            raise ValueError("telemetry_log_interval_seconds must be greater than 0.")
        if (
            not math.isfinite(manual_first_crack_budget_seconds)
            or manual_first_crack_budget_seconds <= 0
        ):
            raise ValueError("manual_first_crack_budget_seconds must be greater than 0.")

        self._telemetry_buffer_limit = telemetry_buffer_limit
        self._telemetry_log_interval_seconds = telemetry_log_interval_seconds
        self._session_history_limit = session_history_limit
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._monotonic_now = monotonic_now or time.monotonic
        self._session_id_factory = session_id_factory or _generate_session_id
        self._default_log_dir = default_log_dir
        self._lock = RLock()
        self._manual_first_crack_writer_enabled = manual_first_crack_writer_enabled
        self._manual_first_crack_budget_seconds = manual_first_crack_budget_seconds
        # A stuck audit from a retired generation must not occupy the fresh
        # generation's lane.  Two owned lanes cover the live/recovery overlap;
        # further unavailable lanes fail closed instead of spawning work.
        self._manual_first_crack_writers: dict[str, ThreadPoolExecutor] = {}
        self._manual_first_crack_writer_capacity = 2
        self._manual_first_crack_attempt: ManualFirstCrackAttempt | None = None
        self._manual_first_crack_attempt_session_id: str | None = None
        self._manual_first_crack_audit_identities: dict[str, _ManualFirstCrackAuditIdentity] = {}
        self._manual_first_crack_live_revoked_tokens: set[str] = set()
        self._emergency_fault_completions: dict[int, tuple[RoastEvent, Future[None]]] = {}
        # Ordinary canonical-event admission is distinct from writer
        # completion.  The exact key prevents a concurrent caller from
        # queueing a duplicate singleton while the first durable turn is
        # unresolved; it is never shared with a successor generation.
        self._canonical_event_admissions: set[tuple[str, RoastEventKind]] = set()
        # A sampler that was admitted for the current manual/mock generation
        # owns a read-to-settlement turn. It may still append telemetry or
        # enter containment after Stop retired live sampling, so a replay seal
        # must not snapshot around it.
        self._manual_mock_sampler_admissions: dict[str, int] = {}
        self._clock_epoch_id = _generate_clock_epoch_id()
        if (
            type(self._clock_epoch_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self._clock_epoch_id, flags=re.ASCII) is None
        ):
            raise ValueError(
                "Timing clock epoch identifier must be 32 lowercase hexadecimal characters."
            )
        self._latest_session: RoastSession | None = None
        self._capture_ownership_sequence = 0
        self._sessions_by_id: dict[str, RoastSession] = {}
        self._session_id_order: deque[str] = deque()
        self._pending_session_start_token: str | None = None
        self._session_start_in_flight_token: str | None = None
        # Unlike an ordinary driver token, this fence survives an emergency
        # cancellation until the exact operation is settled. It prevents a
        # later call from treating a cleared token as evidence of non-dispatch.
        self._normal_drop_in_flight: NormalDropReservation | None = None
        # UNKNOWN is terminal for one retained reservation.  A later duplicate
        # driver completion cannot reinterpret that operation as cancellation.
        self._normal_drop_fence_state: NormalDropFenceState | None = None
        self._normal_drop_persistence_next = 0
        self._normal_drop_persistence_last_completion = Event()
        self._normal_drop_persistence_last_completion.set()
        self._normal_drop_persistence_drop_ticket: MockDropPersistenceTicket | None = None
        self._normal_drop_persistence_drop_started = False
        self._normal_drop_persistence_failed = False
        self._normal_drop_persistence_torn = False
        self._normal_drop_unpersisted_fault_count = 0
        self._fault_recovery_in_flight_tokens: dict[str, str] = {}
        self._fault_recovery_admission_blocks: set[str] = set()
        self._fault_drop_cooling_cycle_required: set[str] = set()
        # A recovery Drop whose verified row was superseded before it could
        # project state remains historical evidence only.  In particular it is
        # not the policy decision that can retire this stopped session for a
        # fresh Start.
        self._historical_fault_drop_start_blocks: set[str] = set()
        self._fault_drop_ambiguity_required: set[str] = set()
        self._finalisation_generation = 0
        self._finalisation_tokens: dict[str, str] = {}
        self._finalisation_in_progress: set[str] = set()
        self._nonterminal_finalisations: set[str] = set()

    def start_session(self, *, purpose: SessionPurpose = "roast") -> RoastSession:
        """Start one new active session.

        Returns:
            The created active roast session.

        Raises:
            SessionLifecycleError: If an active session already exists or a
                faulted stopped session still has cooling active.
        """
        with self._lock:
            if (
                self._pending_session_start_token is not None
                or self._session_start_in_flight_token is not None
            ):
                raise SessionLifecycleError("A roast session start is already in progress.")
            return self._start_session_locked(purpose=purpose)

    def start_session_snapshot(self, *, purpose: SessionPurpose = "roast") -> RoastSession:
        """Start one new session and return an atomic lightweight snapshot."""
        with self._lock:
            session = self.start_session(purpose=purpose)
            return _copy_session_for_read(session)

    def operational_monotonic_now(self) -> float:
        """Return the operational monotonic time used for session lifecycle events.

        State projections must use this clock rather than independently reading
        the host clock so their elapsed values share the event timeline's
        clock domain.
        """
        return self._monotonic_now()

    def reserve_session_start(self) -> SessionStartReservation:
        """Reserve session startup before preparing the configured driver."""
        with self._lock:
            self._assert_no_pending_fault_cooling_recovery_locked()
            if self._latest_session is not None and self._latest_session.active:
                raise SessionLifecycleError("An active roast session already exists.")
            if (
                self._pending_session_start_token is not None
                or self._session_start_in_flight_token is not None
            ):
                raise SessionLifecycleError("A roast session start is already in progress.")
            reservation = SessionStartReservation(token=_generate_session_id())
            self._pending_session_start_token = reservation.token
            self._session_start_in_flight_token = reservation.token
            return reservation

    def complete_session_start_snapshot(
        self,
        reservation: SessionStartReservation,
        *,
        purpose: SessionPurpose = "roast",
    ) -> RoastSession:
        """Create the reserved session and return an atomic lightweight snapshot."""
        with self._lock:
            self._assert_session_start_reservation(reservation)
            try:
                session = self._start_session_locked(purpose=purpose)
            finally:
                self._pending_session_start_token = None
                if self._session_start_in_flight_token == reservation.token:
                    self._session_start_in_flight_token = None
            return _copy_session_for_read(session)

    def complete_staged_session_start_snapshot(
        self,
        reservation: SessionStartReservation,
        *,
        publication: StagedSessionStartPublication,
        purpose: SessionPurpose = "roast",
    ) -> RoastSession:
        """Publish a reserved session while retaining its exact identity on handoff failure.

        The caller-owned publication result is bound in ``finally`` before the
        snapshot copy. A caller that receives an exception can therefore retain
        the exact published object without deriving it from a return value or a
        later latest-session lookup.
        """
        with self._lock:
            self._assert_session_start_reservation(reservation)
            previous = self._latest_session
            try:
                session = self._start_session_locked(purpose=purpose)
            finally:
                published = self._latest_session
                if published is not previous and published is not None:
                    publication.session = published
                    if self._pending_session_start_token == reservation.token:
                        self._pending_session_start_token = None
                    if self._session_start_in_flight_token == reservation.token:
                        self._session_start_in_flight_token = None
            return _copy_session_for_read(session)

    def assert_staged_start_claimable(self, session: RoastSession) -> None:
        """Require the exact live session to remain eligible for staged entry."""
        with self._lock:
            self._assert_staged_start_claimable_locked(session)

    def snapshot_staged_start_claimable(self, session: RoastSession) -> RoastSession:
        """Return an isolated exact-session snapshot after staged Start eligibility checks.

        The caller holds the lifecycle barrier while deciding whether a staged
        Start can succeed.  Copying under this same store critical section
        keeps the ordinary reply independent from an immediately following
        emergency mutation without holding the barrier during serialization.
        """
        with self._lock:
            self._assert_staged_start_claimable_locked(session)
            return _copy_session_for_read(session)

    def clear_session_start_reservation(
        self,
        reservation: SessionStartReservation,
    ) -> None:
        """Clear a pending session-start reservation if it is still current."""
        with self._lock:
            if self._pending_session_start_token == reservation.token:
                self._pending_session_start_token = None
            if self._session_start_in_flight_token == reservation.token:
                self._session_start_in_flight_token = None

    def revoke_session_start(self, reservation: SessionStartReservation) -> bool:
        """Invalidate exact pending admission while retaining its in-flight owner.

        The server caller holds the lifecycle barrier before entering the store.
        This operation performs no transport work and claims no physical stop.
        """
        with self._lock:
            if (
                self._pending_session_start_token != reservation.token
                or self._session_start_in_flight_token != reservation.token
            ):
                return False
            self._pending_session_start_token = None
            return True

    def cancel_session_start_for_emergency_stop(self) -> None:
        """Invalidate pending start admission while its driver connect remains in flight."""
        with self._lock:
            self._pending_session_start_token = None

    def stop_session(self, *, phase: RoastPhase = "complete") -> RoastSession | None:
        """Stop the active session if one exists.

        Args:
            phase: Final phase to set on the stopped session.

        Returns:
            The active session after stopping, or `None` when no active session exists.
        """
        with self._lock:
            if self._latest_session is None or not self._latest_session.active:
                return None
            self._latest_session.stop(
                utc_now=self._utc_now,
                monotonic_now=self._monotonic_now,
                phase=phase,
            )
            return self._latest_session

    def append_telemetry(
        self,
        session: RoastSession,
        sample: TelemetrySample,
    ) -> None:
        """Append telemetry under store-owned locking and retention policy.

        Args:
            session: Session to mutate.
            sample: Telemetry sample to append.

        Raises:
            SessionLifecycleError: If the session is not the latest session in this store.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            _append_telemetry_with_limit(
                session,
                sample,
                max_samples=self._telemetry_buffer_limit,
                log_interval_seconds=self._telemetry_log_interval_seconds,
            )

    def record_telemetry_sample(
        self,
        session: RoastSession,
        *,
        bean_temp_c: float | None,
        env_temp_c: float | None,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> RoastSession:
        """Record one normalized telemetry sample using the session clock.

        Args:
            session: Latest active session that owns the sample.
            bean_temp_c: Normalized bean temperature in Celsius, when available.
            env_temp_c: Normalized environment temperature in Celsius, when available.
            heat_level_percent: Current heat control level.
            fan_level_percent: Current fan control level.
            cooling_on: Current cooling state.

        Returns:
            A lightweight read snapshot after appending the sample.

        Raises:
            SessionLifecycleError: If the session is not the latest active session.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            self._assert_finalisation_allows_mutation(session)
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            sample = TelemetrySample(
                recorded_at_utc=self._utc_now(),
                monotonic_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            _append_telemetry_with_limit(
                session,
                sample,
                max_samples=self._telemetry_buffer_limit,
                log_interval_seconds=self._telemetry_log_interval_seconds,
            )
            return _copy_session_for_read(session)

    def record_active_telemetry_sample(
        self,
        *,
        session_id: str | None,
        bean_temp_c: float | None,
        env_temp_c: float | None,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> RoastSession | None:
        """Record telemetry for the latest active session when it still matches.

        Args:
            session_id: Requested session id, or `None` for the active session.
            bean_temp_c: Normalized bean temperature in Celsius, when available.
            env_temp_c: Normalized environment temperature in Celsius, when available.
            heat_level_percent: Current heat control level.
            fan_level_percent: Current fan control level.
            cooling_on: Current cooling state.

        Returns:
            A lightweight read snapshot after appending the sample, or `None`
            when no matching active session still exists.
        """
        with self._lock:
            if self._latest_session is None or not self._latest_session.active:
                return None
            if self._finalisation_blocks_mutation(self._latest_session):
                return None
            if (
                self._normal_drop_persistence_torn
                or not self._normal_drop_persistence_last_completion.is_set()
            ):
                return None
            if session_id is not None and self._latest_session.id != session_id:
                return None
            sample = TelemetrySample(
                recorded_at_utc=self._utc_now(),
                monotonic_seconds=self._latest_session.elapsed_monotonic_seconds(
                    self._monotonic_now
                ),
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            _append_telemetry_with_limit(
                self._latest_session,
                sample,
                max_samples=self._telemetry_buffer_limit,
                log_interval_seconds=self._telemetry_log_interval_seconds,
            )
            return _copy_session_for_read(self._latest_session)

    def admit_manual_mock_sampler(self, session_id: str) -> RoastSession | None:
        """Admit one exact active-session sampler turn for manual/mock recording.

        The caller settles a returned session only after its driver read,
        telemetry handling, and any resulting containment have finished.
        """
        with self._lock:
            session = self._latest_session
            if session is None or session.id != session_id or not session.active:
                return None
            self._manual_mock_sampler_admissions[session_id] = (
                self._manual_mock_sampler_admissions.get(session_id, 0) + 1
            )
            return session

    def settle_manual_mock_sampler(self, session: RoastSession) -> None:
        """Release one previously admitted manual/mock sampler turn."""
        with self._lock:
            count = self._manual_mock_sampler_admissions.get(session.id, 0)
            if count <= 1:
                self._manual_mock_sampler_admissions.pop(session.id, None)
            else:
                self._manual_mock_sampler_admissions[session.id] = count - 1

    def set_heat(
        self,
        session: RoastSession,
        *,
        heat_level_percent: int,
    ) -> RoastSession:
        """Set the latest in-memory heat value for one active session."""
        with self._lock:
            self._assert_latest_active_session(session)
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            if session.faulted_at_utc is not None and validated_heat > 0:
                raise SessionLifecycleError("Heat cannot be increased after a fault.")
            session.heat_level_percent = validated_heat
            return session

    def set_heat_snapshot(
        self,
        session: RoastSession,
        *,
        heat_level_percent: int,
    ) -> RoastSession:
        """Apply heat and return an atomic lightweight snapshot."""
        with self._lock:
            self.set_heat(session, heat_level_percent=heat_level_percent)
            return _copy_session_for_read(session)

    def set_fan(
        self,
        session: RoastSession,
        *,
        fan_level_percent: int,
    ) -> RoastSession:
        """Set the latest in-memory fan value for one active session."""
        with self._lock:
            self._assert_latest_active_session(session)
            session.fan_level_percent = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )
            return session

    def set_fan_snapshot(
        self,
        session: RoastSession,
        *,
        fan_level_percent: int,
    ) -> RoastSession:
        """Apply fan and return an atomic lightweight snapshot."""
        with self._lock:
            self.set_fan(session, fan_level_percent=fan_level_percent)
            return _copy_session_for_read(session)

    def apply_driver_control_state(
        self,
        session: RoastSession,
        *,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> RoastSession:
        """Apply the normalized control state returned by the configured driver."""
        with self._lock:
            self._assert_latest_active_session(session)
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            if session.faulted_at_utc is not None and validated_heat > 0:
                raise SessionLifecycleError("Heat cannot be increased after a fault.")
            session.heat_level_percent = validated_heat
            session.fan_level_percent = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )
            session.cooling_on = cooling_on
            return session

    def apply_driver_control_state_snapshot(
        self,
        session: RoastSession,
        *,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> RoastSession:
        """Apply driver controls and return an atomic lightweight snapshot."""
        with self._lock:
            self.apply_driver_control_state(
                session,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            return _copy_session_for_read(session)

    def reserve_driver_command(
        self,
        session: RoastSession,
        *,
        kind: DriverCommandKind,
    ) -> DriverCommandReservation:
        """Reserve one non-emergency driver command for an active session."""
        with self._lock:
            self._assert_latest_active_session(session)
            # Lever adjustments neither own a lifecycle endpoint nor replace
            # the session.  A pending human mark remains eligible across an
            # admitted control turn; endpoint paths retain their explicit
            # revocation at their existing admission points.
            if kind != "control":
                self._revoke_unconsumed_manual_first_crack_live_locked(session)
            return self._reserve_driver_command_locked(session, kind=kind)

    def inspect_fixed_zero_containment_identity(
        self, expected_session_id: str
    ) -> FixedZeroIdentityRejection | None:
        """Classify exact-session containment identity without reserving work."""
        with self._lock:
            return self._fixed_zero_identity_rejection_locked(expected_session_id)

    def inspect_timing_evidence_identity(
        self, expected_session_id: str
    ) -> TimingEvidenceIdentityRejection | None:
        """Classify timing-evidence identity before any driver read."""
        with self._lock:
            return self._timing_evidence_identity_rejection_locked(expected_session_id)

    def _timing_evidence_observations_locked(
        self, session: RoastSession
    ) -> tuple[RoastEvent | None, RoastEvent | None]:
        """Return the canonical charge and human first-crack state under ownership."""
        charge: RoastEvent | None = None
        first_crack: RoastEvent | None = None
        for event in session.event_timeline:
            if event.kind == "beans_added" and charge is None:
                charge = event
            elif event.kind == "first_crack_detected" and first_crack is None:
                first_crack = event
        human_first_crack = first_crack if session.first_crack_source == "manual_mark" else None
        return charge, human_first_crack

    def admit_timing_evidence(self, expected_session_id: str) -> TimingEvidenceAdmission:
        """Revalidate an exact roast session without reserving or mutating it."""
        with self._lock:
            latest = self._latest_session
            if (
                latest is not None
                and latest.id == expected_session_id
                and self._normal_drop_in_flight is not None
            ):
                return TimingEvidenceAdmission(
                    latest,
                    latest.capture_ownership_sequence,
                    latest.id,
                    busy_rejection="drop_in_progress",
                )
            identity_rejection = self._timing_evidence_identity_rejection_locked(
                expected_session_id
            )
            if identity_rejection is not None:
                return TimingEvidenceAdmission(
                    None, None, None, identity_rejection=identity_rejection
                )
            session = self._latest_session
            assert session is not None
            if self._finalisation_blocks_mutation(session):
                return TimingEvidenceAdmission(
                    session,
                    session.capture_ownership_sequence,
                    session.id,
                    busy_rejection="finalisation_in_progress",
                )
            if session.pending_driver_command_kind == "drop":
                return TimingEvidenceAdmission(
                    session,
                    session.capture_ownership_sequence,
                    session.id,
                    busy_rejection="drop_in_progress",
                )
            canonical_charge, human_first_crack = self._timing_evidence_observations_locked(session)
            return TimingEvidenceAdmission(
                session,
                session.capture_ownership_sequence,
                session.id,
                canonical_charge=canonical_charge,
                human_first_crack=human_first_crack,
            )

    def read_timing_evidence_clock(self) -> float:
        """Read the injected store clock without taking the store lock."""
        return self._monotonic_now()

    def assemble_timing_evidence(
        self,
        admission: TimingEvidenceAdmission,
        *,
        t0: float,
        t1: float,
        channels: Mapping[str, Mapping[str, object]],
        finalize: Callable[[dict[str, object]], object],
    ) -> TimingEvidenceAssembly:
        """Atomically assemble and commit one validated synthetic timing record.

        The supplied finalizer is intentionally pure validation owned by the
        presentation boundary. It is invoked while the store lock is held and
        must not perform I/O or call back into the store.
        """
        with self._lock:
            session = admission.session
            if (
                session is None
                or self._latest_session is not session
                or admission.ownership_sequence is None
                or session.capture_ownership_sequence != admission.ownership_sequence
                or admission.session_id is None
                or session.id != admission.session_id
            ):
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")
            if self._normal_drop_in_flight is not None:
                return TimingEvidenceAssembly("refused_busy", "drop_in_progress")
            if session.faulted_at_utc is not None or session.phase == "fault" or not session.active:
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")
            if self._finalisation_blocks_mutation(session):
                return TimingEvidenceAssembly("refused_busy", "finalisation_in_progress")
            if session.pending_driver_command_kind == "drop":
                return TimingEvidenceAssembly("refused_busy", "drop_in_progress")

            lo = t0 - session.monotonic_start
            hi = t1 - session.monotonic_start
            if not math.isfinite(lo) or not math.isfinite(hi):
                return TimingEvidenceAssembly("unavailable", "clock_invalid")
            if lo < 0:
                return TimingEvidenceAssembly("unavailable", "clock_regression")
            if (
                session.timing_evidence_last_lo is not None and lo < session.timing_evidence_last_lo
            ) or (
                session.timing_evidence_last_hi is not None and hi < session.timing_evidence_last_hi
            ):
                return TimingEvidenceAssembly("unavailable", "out_of_order_read")
            maximum = 2**53 - 1
            if session.timing_evidence_sequence >= maximum:
                return TimingEvidenceAssembly("unavailable", "sequence_exhausted")
            if not 1 <= session.capture_ownership_sequence <= maximum:
                return TimingEvidenceAssembly("unavailable", "record_invalid")

            event_by_kind: dict[str, tuple[int, RoastEvent]] = {}
            for index, event in enumerate(session.event_timeline):
                if event.kind in {"beans_added", "first_crack_detected", "beans_dropped"}:
                    event_by_kind.setdefault(event.kind, (index, event))
            if (session.beans_added_source is not None and "beans_added" not in event_by_kind) or (
                session.first_crack_source is not None
                and "first_crack_detected" not in event_by_kind
            ):
                return TimingEvidenceAssembly("unavailable", "record_invalid")

            def event_evidence(kind: str, source: str) -> dict[str, object] | None:
                indexed_event = event_by_kind.get(kind)
                if indexed_event is None:
                    return None
                index, event = indexed_event
                if (
                    type(index) is not int
                    or not 0 <= index <= maximum
                    or type(event.monotonic_seconds) is not float
                    or not math.isfinite(event.monotonic_seconds)
                    or event.monotonic_seconds < 0
                ):
                    raise ValueError("Invalid canonical timing event.")
                return {
                    "kind": kind,
                    "event_index": index,
                    "canonical_seconds": event.monotonic_seconds,
                    "clock_epoch_id": self._clock_epoch_id,
                    "source": source,
                    "time_basis": (
                        "detector_clock_rebased"
                        if source == "model_detection"
                        else "producer_canonical_record"
                    ),
                    "physical_time_bound": "not_provided",
                }

            def channel_evidence(channel: Mapping[str, object]) -> Mapping[str, object]:
                if channel.get("status") != "valid":
                    return channel
                return {
                    "status": "valid",
                    "celsius": channel["celsius"],
                    "acquisition_lo_seconds": lo,
                    "acquisition_hi_seconds": hi,
                    "clock_epoch_id": self._clock_epoch_id,
                    "basis": "in_call_synthesis",
                    "qualification": "synthetic_software_only",
                }

            try:
                record: dict[str, object] = {
                    "session_id": session.id,
                    "session_ownership_sequence": session.capture_ownership_sequence,
                    "driver": "mock",
                    "clock": {
                        "epoch_id": self._clock_epoch_id,
                        "domain": "session_relative_store_monotonic",
                        "suspend_behaviour": "unqualified",
                    },
                    "evidence_sequence": session.timing_evidence_sequence + 1,
                    "phase": session.phase,
                    "charge": event_evidence(
                        "beans_added", session.beans_added_source or "unattributed"
                    ),
                    "first_crack": event_evidence(
                        "first_crack_detected",
                        session.first_crack_source or "unattributed",
                    ),
                    "drop": event_evidence("beans_dropped", "unattributed"),
                    "bean": channel_evidence(channels["bean"]),
                    "environment": channel_evidence(channels["environment"]),
                    "time_basis": "canonical_operational_session_relative",
                    "physical_qualification": "none_synthetic_mock_only",
                }
                result = finalize(record)
            except Exception:  # noqa: BLE001 - validation is an unavailable business outcome.
                return TimingEvidenceAssembly("unavailable", "record_invalid")
            session.timing_evidence_sequence += 1
            session.timing_evidence_last_lo = lo
            session.timing_evidence_last_hi = hi
            return TimingEvidenceAssembly("evidence", None, result)

    def assemble_hottop_reception_evidence(
        self,
        admission: TimingEvidenceAdmission,
        *,
        t0: float,
        t1: float,
        finalize: Callable[[dict[str, object]], object],
    ) -> TimingEvidenceAssembly:
        """Atomically assemble one simulated-Hottop reception record.

        This deliberately has its own sequence and ordering state.  Generic
        mock timing evidence therefore remains neither an input nor a freshness
        upgrade for the Hottop exact-session protocol.
        """
        with self._lock:
            session = admission.session
            if (
                session is None
                or self._latest_session is not session
                or admission.ownership_sequence is None
                or session.capture_ownership_sequence != admission.ownership_sequence
                or admission.session_id is None
                or session.id != admission.session_id
            ):
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")
            if (
                self._normal_drop_in_flight is not None
                or session.pending_driver_command_kind == "drop"
            ):
                return TimingEvidenceAssembly("refused_busy", "drop_in_progress")
            if session.faulted_at_utc is not None or session.phase == "fault" or not session.active:
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")
            if self._finalisation_blocks_mutation(session):
                return TimingEvidenceAssembly("refused_busy", "finalisation_in_progress")

            lo = t0 - session.monotonic_start
            hi = t1 - session.monotonic_start
            if not math.isfinite(lo) or not math.isfinite(hi):
                return TimingEvidenceAssembly("unavailable", "clock_invalid")
            if lo < 0 or hi < lo:
                return TimingEvidenceAssembly("unavailable", "clock_regression")
            if (
                session.hottop_reception_evidence_last_lo is not None
                and lo < session.hottop_reception_evidence_last_lo
            ) or (
                session.hottop_reception_evidence_last_hi is not None
                and hi < session.hottop_reception_evidence_last_hi
            ):
                return TimingEvidenceAssembly("unavailable", "out_of_order_read")
            maximum = 2**53 - 1
            if session.hottop_reception_evidence_sequence >= maximum:
                return TimingEvidenceAssembly("unavailable", "sequence_exhausted")
            if not 1 <= session.capture_ownership_sequence <= maximum:
                return TimingEvidenceAssembly("unavailable", "record_invalid")

            event_by_kind: dict[str, tuple[int, RoastEvent]] = {}
            for index, event in enumerate(session.event_timeline):
                if event.kind in {"beans_added", "first_crack_detected", "beans_dropped"}:
                    event_by_kind.setdefault(event.kind, (index, event))
            if (session.beans_added_source is not None and "beans_added" not in event_by_kind) or (
                session.first_crack_source is not None
                and "first_crack_detected" not in event_by_kind
            ):
                return TimingEvidenceAssembly("unavailable", "record_invalid")

            # The driver candidate is classified over [lo, hi].  A charge or
            # manual first-crack mark committed after that completed bracket
            # cannot share a record with the pre-mark receipt.
            charge = event_by_kind.get("beans_added")
            manual_first_crack = event_by_kind.get("first_crack_detected")
            current_charge = charge[1] if charge is not None else None
            current_human_first_crack = (
                manual_first_crack[1]
                if session.first_crack_source == "manual_mark" and manual_first_crack is not None
                else None
            )
            if (
                admission.canonical_charge != current_charge
                or admission.human_first_crack != current_human_first_crack
            ):
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")
            if (charge is not None and charge[1].monotonic_seconds > hi) or (
                session.first_crack_source == "manual_mark"
                and manual_first_crack is not None
                and manual_first_crack[1].monotonic_seconds > hi
            ):
                return TimingEvidenceAssembly("refused_identity", "session_changed_during_read")

            def event_evidence(kind: str, source: str) -> dict[str, object] | None:
                indexed_event = event_by_kind.get(kind)
                if indexed_event is None:
                    return None
                index, event = indexed_event
                if (
                    type(index) is not int
                    or not 0 <= index <= maximum
                    or type(event.monotonic_seconds) is not float
                    or not math.isfinite(event.monotonic_seconds)
                    or event.monotonic_seconds < 0
                ):
                    raise ValueError("Invalid canonical timing event.")
                return {
                    "kind": kind,
                    "event_index": index,
                    "canonical_seconds": event.monotonic_seconds,
                    "clock_epoch_id": self._clock_epoch_id,
                    "source": source,
                    "time_basis": (
                        "detector_clock_rebased"
                        if source == "model_detection"
                        else "producer_canonical_record"
                    ),
                    "physical_time_bound": "not_provided",
                }

            try:
                result = finalize(
                    {
                        "session_id": session.id,
                        "session_ownership_sequence": session.capture_ownership_sequence,
                        "clock": {
                            "epoch_id": self._clock_epoch_id,
                            "domain": "session_relative_store_monotonic",
                            "suspend_behaviour": "unqualified",
                        },
                        "reception_evidence_sequence": (
                            session.hottop_reception_evidence_sequence + 1
                        ),
                        "phase": session.phase,
                        "charge": event_evidence(
                            "beans_added", session.beans_added_source or "unattributed"
                        ),
                        "first_crack": event_evidence(
                            "first_crack_detected", session.first_crack_source or "unattributed"
                        ),
                        "drop": event_evidence("beans_dropped", "unattributed"),
                        "decision": {
                            "operational_lo_seconds": lo,
                            "operational_hi_seconds": hi,
                            "clock_epoch_id": self._clock_epoch_id,
                            "basis": "producer_classification_bracket",
                        },
                    }
                )
            except Exception:  # noqa: BLE001 - validation is an unavailable business outcome.
                return TimingEvidenceAssembly("unavailable", "record_invalid")
            session.hottop_reception_evidence_sequence += 1
            session.hottop_reception_evidence_last_lo = lo
            session.hottop_reception_evidence_last_hi = hi
            return TimingEvidenceAssembly("evidence", None, result)

    def reserve_fixed_zero_containment(
        self, expected_session_id: str
    ) -> FixedZeroContainmentAdmission:
        """Revalidate and reserve one fixed-zero containment command.

        The returned live session is intentionally used only to invoke the driver
        outside the store lock.  The result must be completed through the
        matching fixed-zero completion method, which repeats all ownership checks.
        """
        with self._lock:
            identity_rejection = self._fixed_zero_identity_rejection_locked(expected_session_id)
            if identity_rejection is not None:
                return FixedZeroContainmentAdmission(
                    session=None,
                    reservation=None,
                    identity_rejection=identity_rejection,
                )
            session = self._latest_session
            assert session is not None
            if self._finalisation_blocks_mutation(session):
                return FixedZeroContainmentAdmission(
                    session=session,
                    reservation=None,
                    busy_rejection="finalisation_in_progress",
                )
            if session.pending_driver_command_token is not None:
                return FixedZeroContainmentAdmission(
                    session=session,
                    reservation=None,
                    busy_rejection="command_in_progress",
                )
            return FixedZeroContainmentAdmission(
                session=session,
                reservation=self._reserve_driver_command_locked(session, kind="heat_containment"),
            )

    def complete_fixed_zero_containment_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
    ) -> RoastSession:
        """Commit only an admitted fixed-zero heat projection and clear its token.

        All mutation precedes the success-only token clear.  Exceptions leave a
        matching reservation in place so the caller can conservatively report an
        unconfirmed completion rather than manufacture settlement.
        """
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            if reservation.kind != "heat_containment":
                raise SessionLifecycleError("Reservation is not fixed-zero containment.")
            session.heat_level_percent = 0
            snapshot = _copy_session_for_read(session)
            self._clear_driver_command_reservation_locked(session, reservation)
            return snapshot

    def fixed_zero_containment_ownership_matches(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> bool:
        """Return whether an unresolved containment token is still the current owner."""
        with self._lock:
            return (
                self._latest_session is session
                and session.active
                and reservation.session_id == session.id
                and reservation.kind == "heat_containment"
                and session.pending_driver_command_token == reservation.token
                and session.pending_driver_command_kind == reservation.kind
                and not self._finalisation_blocks_mutation(session)
            )

    def reserve_driver_drop(self, session: RoastSession) -> DriverDropReservation:
        """Reserve a driver drop command or return the existing drop event."""
        with self._lock:
            self._assert_latest_active_session(session)
            if (
                session.ordinary_driver_drop_dispatch_owned
                or session.verified_ordinary_driver_drop
                or session.unrecorded_driver_drop
            ):
                raise SessionLifecycleError("A prior bean-drop outcome is unresolved.")
            if self._normal_drop_in_flight is not None:
                raise SessionLifecycleError("A normal bean-drop operation is unresolved.")
            existing_event = self._get_existing_singleton_event(session, "beans_dropped")
            if existing_event is not None:
                return DriverDropReservation(
                    reservation=None,
                    existing_event=existing_event,
                    snapshot=_copy_session_for_read(session),
                )
            _validate_event_transition(session, "beans_dropped")
            # An ineligible Drop must not displace an otherwise valid mark.
            # Once the exact endpoint reservation wins, it closes both an
            # existing pending mark and admission of a new one before driver
            # entry.
            reservation = self._reserve_driver_command_locked(session, kind="drop")
            self._admit_manual_first_crack_endpoint_locked(session)
            return DriverDropReservation(reservation=reservation)

    def normal_drop_identity(self, session: RoastSession) -> NormalDropIdentity:
        """Return the current exact identity for an active latest roast session.

        The identity is a store-produced value for a caller that has already
        established its exact session context. It does not itself admit a
        command, and it becomes stale when timing evidence advances.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            return NormalDropIdentity(
                session_id=session.id,
                clock_epoch_id=self._clock_epoch_id,
                ownership_sequence=session.capture_ownership_sequence,
                timing_evidence_sequence=session.timing_evidence_sequence,
            )

    def record_verified_ordinary_driver_drop(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
    ) -> None:
        """Record one returned ordinary mock Drop before persistence.

        The fact is exact-session containment evidence only. It cannot project
        a canonical event or grant lifecycle authority, but it prevents every
        later Drop route from replaying a returned command after emergency
        supersession. It is independent of the cancelable ordinary command
        reservation and remains until verified canonical completion discharges
        it. Pre-dispatch ownership is represented separately so this method
        never claims a driver return before one exists.

        Args:
            session: Session that owned the ordinary Drop reservation.
            reservation: Exact ordinary Drop reservation whose driver call returned.

        Raises:
            SessionLifecycleError: If the supplied reservation is not an exact
                ordinary Drop reservation for this retained session.
        """
        with self._lock:
            if (
                reservation.kind != "drop"
                or reservation.session_id != session.id
                or self._sessions_by_id.get(session.id) is not session
                or not session.ordinary_driver_drop_dispatch_owned
            ):
                raise SessionLifecycleError("Driver drop return does not match its session.")
            session.ordinary_driver_drop_dispatch_owned = False
            session.verified_ordinary_driver_drop = True

    def admit_ordinary_driver_drop_dispatch(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
    ) -> None:
        """Atomically establish exact ordinary-Drop ownership before dispatch.

        This is admission evidence only, not evidence of a driver return. It
        survives cancellation of the ordinary command token so stopped-fault
        recovery cannot replay a Drop whose first call may still be entering.
        """
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            if session.ordinary_driver_drop_dispatch_owned:
                raise SessionLifecycleError("A prior bean-drop outcome is unresolved.")
            if session.verified_ordinary_driver_drop or session.unrecorded_driver_drop:
                raise SessionLifecycleError("A prior bean-drop outcome is unresolved.")
            session.ordinary_driver_drop_dispatch_owned = True

    def retain_ordinary_driver_drop_unknown(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
    ) -> None:
        """Retain a dispatched ordinary Drop whose driver result is unavailable."""
        with self._lock:
            if (
                reservation.kind != "drop"
                or reservation.session_id != session.id
                or self._sessions_by_id.get(session.id) is not session
                or not session.ordinary_driver_drop_dispatch_owned
            ):
                return
            session.ordinary_driver_drop_dispatch_owned = False
            session.unrecorded_driver_drop = True

    def admit_normal_drop(self, identity: object) -> NormalDropAdmission:
        """Atomically admit one exact normal bean drop or return a closed result.

        A recorded event wins over timing-evidence freshness so a truthful
        idempotent retry never reaches dispatch. An unresolved P2 operation is
        reported as ambiguous, including when its first endpoint event was
        recorded before a later projection write failed.
        """
        with self._lock:
            if not isinstance(identity, NormalDropIdentity):
                return NormalDropAdmission("refused", rejection="invalid_identity")
            rejection = self._normal_drop_exact_identity_rejection_locked(identity)
            if rejection is not None:
                return NormalDropAdmission("refused", rejection=rejection)
            session = self._latest_session
            assert session is not None

            existing_event = self._get_existing_singleton_event(session, "beans_dropped")
            if session.ordinary_driver_drop_dispatch_owned:
                return NormalDropAdmission("refused", rejection="command_in_progress")
            if session.verified_ordinary_driver_drop or session.unrecorded_driver_drop:
                return NormalDropAdmission("refused", rejection="prior_drop_transition_unaccounted")
            if self._normal_drop_in_flight is not None:
                return NormalDropAdmission(
                    self._normal_drop_admission_outcome_for_in_flight_locked(session),
                    existing_event=existing_event,
                    snapshot=_copy_session_for_read(session),
                )
            if existing_event is not None:
                return NormalDropAdmission(
                    "recorded",
                    existing_event=existing_event,
                    snapshot=_copy_session_for_read(session),
                )
            rejection = self._normal_drop_lifecycle_rejection_locked(session)
            if rejection is not None:
                return NormalDropAdmission("refused", rejection=rejection)
            evidence_rejection = self._normal_drop_evidence_rejection_locked(session, identity)
            if evidence_rejection is not None:
                return NormalDropAdmission("refused", rejection=evidence_rejection)
            if self._finalisation_blocks_mutation(session):
                return NormalDropAdmission("refused", rejection="finalisation_in_progress")
            if session.pending_driver_command_token is not None:
                return NormalDropAdmission("refused", rejection="command_in_progress")
            try:
                _validate_event_transition(session, "beans_dropped")
            except SessionLifecycleError:
                return NormalDropAdmission("refused", rejection="phase_invalid")

            reservation = NormalDropReservation(identity=identity, token=_generate_session_id())
            session.pending_driver_command_token = reservation.token
            session.pending_driver_command_kind = "drop"
            self._normal_drop_in_flight = reservation
            self._normal_drop_fence_state = "pending"
            self._normal_drop_persistence_next = 0
            self._normal_drop_persistence_last_completion = Event()
            self._normal_drop_persistence_last_completion.set()
            self._normal_drop_persistence_drop_ticket = self._reserve_mock_drop_ticket_locked()
            self._normal_drop_persistence_drop_started = False
            self._normal_drop_persistence_failed = False
            self._normal_drop_persistence_torn = False
            self._normal_drop_unpersisted_fault_count = 0
            return NormalDropAdmission("dispatch", reservation=reservation)

    def settle_normal_drop_cancelled(
        self,
        session: RoastSession,
        *,
        reservation: object,
        proof: object,
    ) -> NormalDropSettlement:
        """Release one exact operation only with positive zero-actuation proof.

        An emergency may already have cleared the ordinary command token and
        faulted the session.  Neither fact is proof of non-dispatch, so this
        settlement accepts only the matching atomic mock-driver proof while
        the retained operation is still pending.
        """
        with self._lock:
            if not isinstance(reservation, NormalDropReservation):
                return NormalDropSettlement("stale")
            if (
                not isinstance(proof, NormalDropNonDispatchProof)
                or proof.zero_actuation is not True
                or proof.reservation != reservation
            ):
                return NormalDropSettlement("stale")
            if not self._normal_drop_reservation_matches_locked(session, reservation):
                return NormalDropSettlement("stale")
            if self._normal_drop_fence_state != "pending":
                return NormalDropSettlement("stale")
            self._discharge_unused_mock_drop_turn_locked(session, reservation)
            self._clear_normal_drop_token_locked(session, reservation)
            self._normal_drop_in_flight = None
            self._normal_drop_fence_state = None
            return NormalDropSettlement("cancelled", snapshot=_copy_session_for_read(session))

    def retain_normal_drop_unknown(
        self,
        session: RoastSession,
        *,
        reservation: object,
    ) -> NormalDropSettlement:
        """Retain the exact drop fence after driver or projection uncertainty."""
        with self._lock:
            if not isinstance(reservation, NormalDropReservation):
                return NormalDropSettlement("stale")
            if not self._normal_drop_reservation_matches_locked(session, reservation):
                return NormalDropSettlement("stale")
            self._discharge_unused_mock_drop_turn_locked(session, reservation)
            self._retain_normal_drop_unknown_locked(session, reservation)
            return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))

    def has_pending_normal_drop_persistence(self, session: RoastSession) -> bool:
        """Return whether an exact mock normal-drop append window remains open.

        The lifecycle fence can become ``unknown`` as soon as an intervening
        emergency stops the session.  That does not release append ownership:
        fault rows already admitted to the same log window must still retain
        their turns until the last writer completes.
        """
        with self._lock:
            if self._latest_session is not session:
                return False
            return (
                self._normal_drop_persistence_torn
                or not self._normal_drop_persistence_last_completion.is_set()
            )

    def normal_drop_persistence_is_torn(self, session: RoastSession) -> bool:
        """Return whether this session's exact mock Drop log has an uncertain tail."""
        with self._lock:
            return (
                self._latest_session is not None
                and self._latest_session.id == session.id
                and session.mock_log_torn
            )

    def has_unpersisted_mock_drop_fault(self, session: RoastSession) -> bool:
        """Return whether this session has a dispatched but unrecorded mock fault."""
        with self._lock:
            return (
                self._latest_session is not None
                and self._latest_session.id == session.id
                and session.unpersisted_mock_drop_fault_count > 0
            )

    def observe_export_readiness(
        self, *, session_id: str | None = None
    ) -> tuple[RoastSession, bool]:
        """Capture one export snapshot and its persistence fence atomically."""
        with self._lock:
            if self._latest_session is None:
                raise SessionLifecycleError("No roast session exists.")
            session = self._latest_session
            if session_id is not None:
                session = self._sessions_by_id.get(session_id)
                if session is None:
                    raise SessionLifecycleError(f"Unknown session_id: {session_id}")
            snapshot = _copy_session_for_read(session)
            return snapshot, (
                session.mock_log_torn
                or session.unpersisted_mock_drop_fault_count > 0
                or self._manual_mock_sampler_admissions.get(session.id, 0) > 0
                or session.manual_first_crack_pending
                or session.manual_first_crack_export_disqualified
            )

    def get_retained_capture_ownership_keys(self) -> tuple[tuple[str, int], ...]:
        """Return retained session capture keys without performing filesystem work."""
        with self._lock:
            return tuple(
                (session_id, self._sessions_by_id[session_id].capture_ownership_sequence)
                for session_id in self._session_id_order
                if session_id in self._sessions_by_id
            )

    def prepare_manual_mock_replay_export(
        self, *, session_id: str, timeout_seconds: float
    ) -> tuple[RoastSession, _CanonicalGenerationWriter] | None:
        """Return one terminal, settled manual/mock generation for replay sealing.

        The short lock sections establish eligibility before and after the
        writer-owned wait.  No filesystem work occurs here, and a timeout
        leaves the executing writer entirely untouched.
        """
        deadline = self._monotonic_now() + timeout_seconds
        with self._lock:
            session = self._sessions_by_id.get(session_id)
            if session is None or not self._manual_mock_replay_eligible_locked(session):
                return None
            writer = session.canonical_writer
            if writer is None:
                return None
        remaining = deadline - self._monotonic_now()
        if remaining <= 0 or not writer.wait_for_idle(timeout_seconds=remaining):
            return None
        with self._lock:
            if self._sessions_by_id.get(session_id) is not session:
                return None
            if not self._manual_mock_replay_eligible_locked(session):
                return None
            return _copy_session_for_read(session), writer

    def _manual_mock_replay_eligible_locked(self, session: RoastSession) -> bool:
        """Return whether one retained generation can be sealed for mock replay."""
        # A fault-only generation has no replay marker to disqualify.  Once its
        # exact recovery right is closed and cooling is verified stopped it is
        # immutable evidence just like a normally completed generation.  A
        # fault after any human FC remains permanently ineligible below.
        fault_only_closed = (
            session.phase == "fault"
            and not session.cooling_on
            and session.first_crack_at_utc is None
            and session.first_crack_source is None
            # While it remains the latest stopped session, its exact guarded
            # recovery commands are still available.  A successor closes
            # those mutation rights; only then is a fault-only record sealed.
            and self._latest_session is not session
        )
        if session.active or (session.phase != "complete" and not fault_only_closed):
            return False
        if (
            session.mock_log_torn
            or session.unpersisted_mock_drop_fault_count > 0
            or session.manual_first_crack_pending
            or session.manual_first_crack_export_disqualified
            or session.manual_charge_persistence_pending
            or session.manual_charge_persistence_uncertain
            or self._manual_mock_sampler_admissions.get(session.id, 0) > 0
            or session.pending_driver_command_token is not None
            or session.id in self._fault_recovery_admission_blocks
            or session.id in self._fault_recovery_in_flight_tokens
        ):
            return False
        return not (
            self._latest_session is session
            and (
                self._normal_drop_persistence_torn
                or not self._normal_drop_persistence_last_completion.is_set()
            )
        )

    def complete_normal_drop_success(
        self,
        session: RoastSession,
        *,
        reservation: object,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> NormalDropSettlement:
        """Record the complete normal-drop projection under its owned reservation.

        Any driver-state validation or event persistence failure becomes an
        unknown result and retains the in-flight fence. This deliberately
        avoids inferring cancellation from a cleared ordinary command token.
        """
        with self._lock:
            if not isinstance(reservation, NormalDropReservation):
                return NormalDropSettlement("stale")
            if not self._normal_drop_reservation_matches_locked(session, reservation):
                return NormalDropSettlement("stale")
            if self._normal_drop_fence_state == "unknown":
                self._discharge_unused_mock_drop_turn_locked(session, reservation)
                return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))
            if not session.active or session.faulted_at_utc is not None or session.phase == "fault":
                self._discharge_unused_mock_drop_turn_locked(session, reservation)
                self._retain_normal_drop_unknown_locked(session, reservation)
                return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))
            if (
                session.pending_driver_command_token != reservation.token
                or session.pending_driver_command_kind != "drop"
            ):
                self._discharge_unused_mock_drop_turn_locked(session, reservation)
                self._retain_normal_drop_unknown_locked(session, reservation)
                return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))
            if not _is_valid_mock_drop_projection(
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            ):
                self._discharge_unused_mock_drop_turn_locked(session, reservation)
                self._retain_normal_drop_unknown_locked(session, reservation)
                return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))

            payload: dict[str, EventPayloadValue] = {
                "heat_level_percent": heat_level_percent,
                "fan_level_percent": fan_level_percent,
                "cooling_on": cooling_on,
            }
            try:
                drop_event = self._new_event_locked(session, "beans_dropped", payload=payload)
                # The read snapshot intentionally excludes the generation-owned
                # writer, which is not part of lifecycle planning and cannot be
                # deep-copied while it owns an executing filesystem turn.
                planned = _copy_session_for_read(session)
                _apply_event_timestamp(planned, drop_event)
                _validate_event_transition(planned, "cooling_started")
                cooling_event = self._new_event_locked(planned, "cooling_started", payload=payload)
                persistence_ticket = self._normal_drop_persistence_drop_ticket
                if persistence_ticket is None:
                    raise SessionLifecycleError("Normal mock bean-drop save has no append ticket.")
            except Exception:  # noqa: BLE001 - close an invalid projection conservatively.
                self._discharge_unused_mock_drop_turn_locked(session, reservation)
                self._retain_normal_drop_unknown_locked(session, reservation)
                return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))
            self._normal_drop_persistence_drop_started = True

        # No lifecycle/store lock is held while a filesystem writer is stalled.
        # This caller owns both reserved endpoint turns; no emergency writer
        # drains or fabricates either row on its behalf.
        drop_row_saved = False
        cooling_row_saved = False
        torn_log = False

        def settle_failure(_error: Exception) -> NormalDropSettlement:
            with self._lock:
                if self._normal_drop_reservation_matches_locked(session, reservation):
                    if torn_log:
                        self._normal_drop_persistence_torn = True
                        session.mock_log_torn = True
                    if drop_row_saved:
                        # The first endpoint is durable evidence, not an
                        # optimistic success: retain it without fabricating the
                        # unwritten cooling endpoint, then fence the operation.
                        # A queued emergency has already published the fault
                        # projection, so retain only the Drop timestamps.
                        session.event_timeline.append(drop_event)
                        if session.faulted_at_utc is not None or not session.active:
                            session.beans_dropped_at_utc = drop_event.recorded_at_utc
                            session.beans_dropped_monotonic_seconds = drop_event.monotonic_seconds
                        else:
                            _apply_event_timestamp(session, drop_event)
                            session.heat_level_percent = heat_level_percent
                            session.fan_level_percent = fan_level_percent
                            session.cooling_on = cooling_on
                    self._retain_normal_drop_unknown_locked(session, reservation)
                    return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))
            return NormalDropSettlement("stale")

        def settle_success() -> NormalDropSettlement:
            with self._lock:
                if not self._normal_drop_reservation_matches_locked(session, reservation):
                    return NormalDropSettlement("stale")
                if session.faulted_at_utc is not None or not session.active:
                    # The rows are confirmed, but an intervention won the lifecycle
                    # race. Retain only durable timestamp evidence without reviving
                    # phase or projected controls, and keep the operation unresolved.
                    session.event_timeline.append(drop_event)
                    if cooling_row_saved:
                        session.event_timeline.append(cooling_event)
                    session.beans_dropped_at_utc = drop_event.recorded_at_utc
                    session.beans_dropped_monotonic_seconds = drop_event.monotonic_seconds
                    self._retain_normal_drop_unknown_locked(session, reservation)
                    return NormalDropSettlement("unknown", snapshot=_copy_session_for_read(session))

                session.heat_level_percent = heat_level_percent
                session.fan_level_percent = fan_level_percent
                session.cooling_on = cooling_on
                session.event_timeline.extend((drop_event, cooling_event))
                _apply_event_timestamp(session, drop_event)
                _apply_event_timestamp(session, cooling_event)
                self._clear_normal_drop_token_locked(session, reservation)
                self._normal_drop_in_flight = None
                self._normal_drop_fence_state = None
                return NormalDropSettlement(
                    "success", event=drop_event, snapshot=_copy_session_for_read(session)
                )

        def append_endpoints() -> None:
            nonlocal cooling_row_saved, drop_row_saved, torn_log
            try:
                _append_event_log_row_with_failure_evidence(session, drop_event)
                drop_row_saved = True
            except _MockDropAppendMayBeTornError:
                torn_log = True
                raise
            # An emergency can stop the session while a stalled Drop row
            # owns this turn.  Its fault is already admitted behind the
            # Drop row, so never manufacture a later cooling endpoint.
            with self._lock:
                if (
                    not self._normal_drop_reservation_matches_locked(session, reservation)
                    or session.faulted_at_utc is not None
                    or not session.active
                ):
                    return
            try:
                _append_event_log_row_with_failure_evidence(session, cooling_event)
                cooling_row_saved = True
            except _MockDropAppendMayBeTornError:
                torn_log = True
                raise

        settlement = self._append_mock_drop_turn(
            persistence_ticket,
            append_endpoints,
            settle_success=settle_success,
            settle_failure=settle_failure,
        )
        assert settlement is not None
        return settlement

    def _new_event_locked(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
    ) -> RoastEvent:
        """Build one validated event without writing or changing session state."""
        recorded_at_utc = _normalize_event_recorded_at_utc(session, recorded_at_utc=self._utc_now())
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
        )
        _validate_event_monotonic_order(session, kind=kind, monotonic_seconds=monotonic_seconds)
        return RoastEvent(
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload={} if payload is None else dict(payload),
        )

    def reserve_driver_start_cooling(self, session: RoastSession) -> DriverEventReservation:
        """Reserve a cooling-start command or return the existing event."""
        with self._lock:
            self._assert_latest_active_session(session)
            existing_event = self._get_existing_singleton_event(session, "cooling_started")
            if existing_event is not None:
                return DriverEventReservation(
                    reservation=None,
                    existing_event=existing_event,
                    snapshot=_copy_session_for_read(session),
                )
            if session.torn_start_cooling_attempted:
                raise SessionLifecycleError(
                    "Cooling command completed, but roast log recording is unavailable."
                )
            if session.unconfirmed_torn_start_cooling_attempted:
                raise SessionLifecycleError(
                    "Cooling could not be confirmed after roast log recording failed."
                )
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling can only start after beans are dropped.")
            _validate_event_transition(session, "cooling_started")
            self._revoke_unconsumed_manual_first_crack_live_locked(session)
            return DriverEventReservation(
                reservation=self._reserve_driver_command_locked(session, kind="start_cooling")
            )

    def settle_known_torn_start_cooling(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
        connected: bool | None = None,
    ) -> RoastSession | None:
        """Retain a known cooling result when its exact P2 log cannot be extended.

        The normal Drop's retained UNKNOWN fence remains the recovery owner.
        This records the driver-returned control projection only: no
        ``cooling_started`` history row can be claimed once the append-only
        JSONL has a torn tail.
        """
        with self._lock:
            if not self._normal_drop_persistence_torn:
                return None
            try:
                self._assert_latest_session(session)
                if (
                    reservation.session_id != session.id
                    or session.pending_driver_command_token != reservation.token
                    or session.pending_driver_command_kind != reservation.kind
                    or reservation.kind != "start_cooling"
                    or not cooling_on
                ):
                    return None
                stopped_fault_recovery = (
                    not session.active
                    and session.faulted_at_utc is not None
                    and session.phase == "fault"
                )
                if not stopped_fault_recovery:
                    self._assert_driver_command_reservation(session, reservation)
                heat = validate_control_percent(heat_level_percent, label="heat_level_percent")
                fan = validate_control_percent(fan_level_percent, label="fan_level_percent")
            except SessionLifecycleError:
                return None
            if heat != 0 or fan != 100 or connected is not True:
                return None
            session.heat_level_percent = heat
            session.fan_level_percent = fan
            session.cooling_on = True
            session.torn_start_cooling_attempted = True
            self._clear_driver_command_reservation_locked(session, reservation)
            if stopped_fault_recovery:
                self._clear_fault_recovery_in_flight_locked(session, reservation)
            return _copy_session_for_read(session)

    def settle_unconfirmed_torn_start_cooling(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
    ) -> bool:
        """Retain one unconfirmed torn-P2 cooling attempt without a cooling claim."""
        with self._lock:
            if not self._normal_drop_persistence_torn:
                return False
            try:
                self._assert_driver_command_reservation(session, reservation)
            except SessionLifecycleError:
                return False
            if reservation.kind != "start_cooling":
                return False
            session.unconfirmed_torn_start_cooling_attempted = True
            self._clear_driver_command_reservation_locked(session, reservation)
            return True

    def settle_known_torn_stop_cooling_recovery(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
        connected: bool,
    ) -> RoastSession | None:
        """Retain a known stopped-fault cooling result without inventing a log row.

        This narrow settlement applies only to an already-admitted exact
        stopped-fault cooling recovery.  The normal Drop persistence fence is
        deliberately left intact: the returned controls are known, but the
        append-only log cannot truthfully claim ``cooling_stopped``.
        """
        with self._lock:
            if not self._normal_drop_persistence_torn:
                return None
            try:
                self._assert_latest_session(session)
                if (
                    reservation.kind != "stop_cooling"
                    or reservation.session_id != session.id
                    or session.pending_driver_command_token != reservation.token
                    or session.pending_driver_command_kind != reservation.kind
                    or session.active
                    or session.faulted_at_utc is None
                    or session.phase != "fault"
                    or cooling_on
                    or connected is not True
                ):
                    return None
                heat = validate_control_percent(heat_level_percent, label="heat_level_percent")
                fan = validate_control_percent(fan_level_percent, label="fan_level_percent")
            except SessionLifecycleError:
                return None
            if heat != 0:
                return None
            session.heat_level_percent = heat
            session.fan_level_percent = fan
            session.cooling_on = False
            self._clear_driver_command_reservation_locked(session, reservation)
            self._clear_fault_recovery_in_flight_locked(session, reservation)
            return _copy_session_for_read(session)

    def reserve_driver_stop_cooling(self, session: RoastSession) -> DriverCommandReservation:
        """Reserve a cooling-stop command for an active cooling session."""
        with self._lock:
            self._assert_latest_active_session(session)
            if _has_unrecorded_charge_attempt(session):
                raise SessionLifecycleError(
                    "Cooling cannot stop while a charge attempt has no canonical record."
                )
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling cannot stop before beans are dropped.")
            if session.cooling_started_at_utc is None or not session.cooling_on:
                raise SessionLifecycleError("Cooling must be started before it can be stopped.")
            self._revoke_unconsumed_manual_first_crack_live_locked(session)
            return self._reserve_driver_command_locked(session, kind="stop_cooling")

    def reserve_driver_stop_cooling_recovery(
        self,
        session: RoastSession,
    ) -> DriverCommandReservation:
        """Reserve cooling-stop recovery for the latest faulted session.

        Args:
            session: Latest session to recover after emergency stop.

        Returns:
            Driver command reservation that must be completed with
            `complete_reserved_driver_stop_cooling_recovery_snapshot` or cleared
            if the driver command fails.

        Raises:
            SessionLifecycleError: If the session is not the latest session, is
                still active, is not faulted, is not in `fault` phase, has no
                active cooling state, or another driver command is in progress.

        Notes:
            This recovery path is intentionally narrower than normal
            `stop_cooling`: it exists only after emergency stop has already
            stopped the session while leaving cooling active as the fail-closed
            hardware state.
        """
        with self._lock:
            self._assert_guarded_fault_recovery_admission_locked(session)
            if not session.cooling_on:
                raise SessionLifecycleError("Cooling must be active before it can be stopped.")
            if _has_unrecorded_charge_attempt(session):
                raise SessionLifecycleError(
                    "Cooling cannot stop while a charge attempt has no canonical record."
                )
            if session.beans_added_at_utc is not None and not _has_recorded_beans_dropped(session):
                raise SessionLifecycleError("Cooling cannot stop before beans are dropped.")
            return self._reserve_driver_command_locked(session, kind="stop_cooling")

    def reserve_driver_fault_recovery(
        self,
        session: RoastSession,
        *,
        kind: Literal["drop", "start_cooling", "stop_cooling"],
    ) -> DriverCommandReservation:
        """Reserve one exact stopped-fault recovery driver command.

        Args:
            session: The latest stopped fault session selected by its exact ID.
            kind: Recovery command to run.

        Returns:
            A reservation that must be completed with
            :meth:`complete_reserved_driver_fault_recovery_snapshot` or cleared.

        Raises:
            SessionLifecycleError: If the session is not the latest stopped fault
                session, retained heat is non-zero, command admission is unsafe,
                or the requested recovery action has no valid precondition.
        """
        with self._lock:
            if kind == "drop" and self._normal_drop_in_flight is not None:
                raise SessionLifecycleError("A normal bean-drop operation is unresolved.")
            self._assert_guarded_fault_recovery_admission_locked(session)
            if kind == "drop":
                if (
                    session.ordinary_driver_drop_dispatch_owned
                    or session.verified_ordinary_driver_drop
                    or session.unrecorded_driver_drop
                ):
                    raise SessionLifecycleError("A prior bean-drop outcome is unresolved.")
                if session.id in self._fault_drop_ambiguity_required:
                    raise SessionLifecycleError(
                        "Recovery bean drop is ambiguous and cannot be retried."
                    )
                if session.beans_added_at_utc is None:
                    raise SessionLifecycleError("Recovery bean drop requires beans to be added.")
                if (
                    _has_recorded_beans_dropped(session)
                    or session.unrecorded_fault_recovery_drop_accepted
                ):
                    raise SessionLifecycleError(
                        "Recovery bean drop is only allowed before beans are dropped."
                    )
            elif kind == "stop_cooling":
                if not session.cooling_on:
                    raise SessionLifecycleError("Cooling must be active before it can be stopped.")
                if _has_unrecorded_charge_attempt(session):
                    raise SessionLifecycleError(
                        "Cooling cannot stop while a charge attempt has no canonical record."
                    )
                if (
                    session.beans_added_at_utc is not None
                    and not _has_recorded_beans_dropped(session)
                    and not session.unrecorded_fault_recovery_drop_accepted
                    and not self._has_verified_known_zero_ordinary_discharge_locked(session)
                ):
                    raise SessionLifecycleError("Cooling cannot stop before beans are dropped.")
            reservation = self._reserve_driver_command_locked(session, kind=kind)
            self._revoke_unconsumed_manual_first_crack_live_locked(session)
            self._fault_recovery_in_flight_tokens[session.id] = reservation.token
            return reservation

    def assert_guarded_fault_recovery_admission(self, session: RoastSession) -> None:
        """Validate exact stopped-fault recovery admission without reserving a command."""
        with self._lock:
            self._assert_guarded_fault_recovery_admission_locked(session)

    def assert_guarded_fault_emergency_stop_admission(self, session: RoastSession) -> None:
        """Validate stopped-fault admission for an emergency-stop reassertion.

        Emergency stop remains available while an earlier guarded recovery
        driver call is still in flight.
        """
        with self._lock:
            self._assert_latest_session(session)
            if session.active:
                raise SessionLifecycleError("Recovery command requires a stopped session.")
            if session.faulted_at_utc is None or session.phase != "fault":
                raise SessionLifecycleError(
                    "Recovery command is only allowed after an emergency stop."
                )

    def complete_reserved_driver_control_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> RoastSession:
        """Complete a reserved control command and return a session snapshot."""
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            self.apply_driver_control_state(
                session,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            self._clear_driver_command_reservation_locked(session, reservation)
            return _copy_session_for_read(session)

    def _complete_reserved_canonical_driver_events(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        events: tuple[tuple[RoastEventKind, dict[str, EventPayloadValue]], ...],
        apply_verified: Callable[[], None],
    ) -> tuple[RoastEvent, RoastSession]:
        """Persist a returned mock-driver transition without retaining the store lock.

        The driver reservation remains the owner until the exact canonical rows
        verify.  Submitting the rows is admission only; waiting for the owned
        writer must occur outside ``_lock`` so a state read and the independent
        emergency driver path retain their established admission priority.
        """
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            writer = session.canonical_writer
            if writer is None:  # pragma: no cover - callers select this path.
                raise SessionLifecycleError("Canonical driver completion has no writer.")
            planned = _copy_session_for_read(session)
            recorded: list[RoastEvent] = []
            for kind, payload in events:
                event = self._new_event_locked(planned, kind, payload=payload)
                _apply_event_timestamp(planned, event)
                recorded.append(event)
            try:
                completions = writer.submit_group(
                    tuple(_event_log_row(session, event) for event in recorded), lane="event"
                )
            except Exception as error:
                if reservation.kind == "drop":
                    if (
                        isinstance(error, _CanonicalAdmissionRefusedError)
                        and error.known_zero
                        and session.verified_ordinary_driver_drop
                    ):
                        # Record this before releasing the exact reservation,
                        # while no later emergency can have displaced it.
                        session.ordinary_drop_persistence_zero = True
                    else:
                        session.unrecorded_driver_drop = True
                self._clear_driver_command_reservation_locked(session, reservation)
                raise

        try:
            for completion in completions:
                completion.result()
        except Exception:
            with self._lock:
                if reservation.kind == "drop":
                    session.unrecorded_driver_drop = True
                self._clear_driver_command_reservation_locked(session, reservation)
            raise

        with self._lock:
            reservation_current = (
                self._latest_session is session
                and session.active
                and session.faulted_at_utc is None
                and session.pending_driver_command_token == reservation.token
                and session.pending_driver_command_kind == reservation.kind
            )
            if not reservation_current:
                # The driver result is already verified and its rows are now
                # durable.  An emergency may have displaced live projection
                # while storage was held, but it cannot turn that accepted
                # driver effect into a repeatable command or revive its phase.
                for event in recorded:
                    insert_at = next(
                        (
                            index
                            for index, existing in enumerate(session.event_timeline)
                            if existing.recorded_at_utc > event.recorded_at_utc
                        ),
                        len(session.event_timeline),
                    )
                    session.event_timeline.insert(insert_at, event)
                    if event.kind == "beans_dropped":
                        session.beans_dropped_at_utc = event.recorded_at_utc
                        session.beans_dropped_monotonic_seconds = event.monotonic_seconds
                    elif event.kind == "cooling_started":
                        session.cooling_started_at_utc = event.recorded_at_utc
                        session.cooling_started_monotonic_seconds = event.monotonic_seconds
                    elif event.kind == "cooling_stopped":
                        session.cooling_stopped_at_utc = event.recorded_at_utc
                        session.cooling_stopped_monotonic_seconds = event.monotonic_seconds
                self._clear_driver_command_reservation_locked(session, reservation)
                raise RecoveryPersistenceSupersededError(
                    "Driver command completed after its live session authority changed."
                )
            apply_verified()
            session.event_timeline.extend(recorded)
            for event in recorded:
                _apply_event_timestamp(session, event)
            self._clear_driver_command_reservation_locked(session, reservation)
            return recorded[0], _copy_session_for_read(session)

    def complete_reserved_driver_drop_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> tuple[RoastEvent, RoastSession]:
        """Complete a reserved drop command and record resulting events.

        Args:
            session: Session to mutate.
            reservation: Active drop command reservation.
            heat_level_percent: Driver heat level after the drop command.
            fan_level_percent: Driver fan level after the drop command.
            cooling_on: Driver cooling state after the drop command.

        Returns:
            The bean-drop event plus an atomic lightweight session snapshot.
        """
        if session.canonical_writer is not None:
            event_payload: dict[str, EventPayloadValue] = {
                "heat_level_percent": heat_level_percent,
                "fan_level_percent": fan_level_percent,
                "cooling_on": cooling_on,
            }
            events: list[tuple[RoastEventKind, dict[str, EventPayloadValue]]] = [
                ("beans_dropped", event_payload)
            ]
            if cooling_on:
                events.append(("cooling_started", event_payload))

            def apply_verified() -> None:
                self.apply_driver_control_state(
                    session,
                    heat_level_percent=heat_level_percent,
                    fan_level_percent=fan_level_percent,
                    cooling_on=cooling_on,
                )
                # A complete coupled projection accounts for the returned
                # ordinary Drop.  Only an unrecorded or partial projection
                # retains the control fence.
                session.verified_ordinary_driver_drop = False

            return self._complete_reserved_canonical_driver_events(
                session,
                reservation=reservation,
                events=tuple(events),
                apply_verified=apply_verified,
            )
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            previous_control_state = (
                session.heat_level_percent,
                session.fan_level_percent,
                session.cooling_on,
            )
            self.apply_driver_control_state(
                session,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            event_payload: dict[str, EventPayloadValue] = {
                "heat_level_percent": heat_level_percent,
                "fan_level_percent": fan_level_percent,
                "cooling_on": cooling_on,
            }
            try:
                event = self.record_event(session, "beans_dropped", payload=event_payload)
                if cooling_on:
                    self.record_event(session, "cooling_started", payload=event_payload)
                session.verified_ordinary_driver_drop = False
            except Exception:
                (
                    session.heat_level_percent,
                    session.fan_level_percent,
                    session.cooling_on,
                ) = previous_control_state
                raise
            finally:
                self._clear_driver_command_reservation_locked(session, reservation)
            return event, _copy_session_for_read(session)

    def complete_reserved_driver_start_cooling_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> tuple[RoastEvent, RoastSession]:
        """Complete a reserved cooling-start command and record the event."""
        if session.canonical_writer is not None:
            if not cooling_on:
                with self._lock:
                    self._assert_driver_command_reservation(session, reservation)
                    self._clear_driver_command_reservation_locked(session, reservation)
                raise SessionLifecycleError(
                    "Driver still reports cooling inactive after start_cooling."
                )

            def apply_verified() -> None:
                self.apply_driver_control_state(
                    session,
                    heat_level_percent=heat_level_percent,
                    fan_level_percent=fan_level_percent,
                    cooling_on=cooling_on,
                )

            return self._complete_reserved_canonical_driver_events(
                session,
                reservation=reservation,
                events=(
                    (
                        "cooling_started",
                        {
                            "heat_level_percent": heat_level_percent,
                            "fan_level_percent": fan_level_percent,
                            "cooling_on": cooling_on,
                        },
                    ),
                ),
                apply_verified=apply_verified,
            )
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            if not cooling_on:
                self._clear_driver_command_reservation_locked(session, reservation)
                raise SessionLifecycleError(
                    "Driver still reports cooling inactive after start_cooling."
                )
            previous_control_state = (
                session.heat_level_percent,
                session.fan_level_percent,
                session.cooling_on,
            )
            self.apply_driver_control_state(
                session,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
            )
            try:
                if self._normal_drop_persistence_torn:
                    raise EventLogRecordingError
                event = self.record_event(
                    session,
                    "cooling_started",
                    payload={
                        "heat_level_percent": heat_level_percent,
                        "fan_level_percent": fan_level_percent,
                        "cooling_on": cooling_on,
                    },
                )
            except Exception:
                (
                    session.heat_level_percent,
                    session.fan_level_percent,
                    session.cooling_on,
                ) = previous_control_state
                raise
            finally:
                # The caller may still settle a known driver result without a
                # history row for the exact torn-P2 path.  It needs this
                # reservation to prove ownership; all ordinary failures keep
                # their established eager clear.
                if not self._normal_drop_persistence_torn:
                    self._clear_driver_command_reservation_locked(session, reservation)
            return event, _copy_session_for_read(session)

    def complete_reserved_driver_stop_cooling_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> tuple[RoastEvent, RoastSession]:
        """Complete a reserved cooling-stop command and record the event."""
        if session.canonical_writer is not None:
            if cooling_on:
                raise SessionLifecycleError(
                    "Driver still reports cooling active after stop_cooling."
                )
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            validated_fan = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )

            def apply_verified() -> None:
                session.heat_level_percent = validated_heat
                session.fan_level_percent = validated_fan
                session.cooling_on = False
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="complete",
                )

            return self._complete_reserved_canonical_driver_events(
                session,
                reservation=reservation,
                events=(
                    (
                        "cooling_stopped",
                        {
                            "heat_level_percent": validated_heat,
                            "fan_level_percent": validated_fan,
                            "cooling_on": False,
                        },
                    ),
                ),
                apply_verified=apply_verified,
            )
        with self._lock:
            self._assert_driver_command_reservation(session, reservation)
            if cooling_on:
                raise SessionLifecycleError(
                    "Driver still reports cooling active after stop_cooling."
                )
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            validated_fan = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )
            try:
                event = self.record_event(
                    session,
                    "cooling_stopped",
                    payload={
                        "heat_level_percent": validated_heat,
                        "fan_level_percent": validated_fan,
                        "cooling_on": False,
                    },
                )
                session.heat_level_percent = validated_heat
                session.fan_level_percent = validated_fan
                session.cooling_on = False
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="complete",
                )
            finally:
                self._clear_driver_command_reservation_locked(session, reservation)
            return event, _copy_session_for_read(session)

    def complete_reserved_driver_stop_cooling_recovery_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
    ) -> tuple[RoastEvent, RoastSession]:
        """Complete cooling-stop recovery after an emergency stop.

        Args:
            session: Latest faulted, stopped session being recovered.
            reservation: Active recovery stop-cooling reservation returned by
                `reserve_driver_stop_cooling_recovery`.
            heat_level_percent: Driver-reported heat level after the recovery
                stop-cooling command.
            fan_level_percent: Driver-reported fan level after the recovery
                stop-cooling command.
            cooling_on: Driver-reported cooling state after the recovery
                stop-cooling command.

        Returns:
            The recorded `cooling_stopped` recovery event plus an atomic session
            snapshot.

        Raises:
            SessionLifecycleError: If the reservation is stale or belongs to a
                different session, the session is active, the session is not a
                stopped fault session, the driver still reports cooling active,
                or returned heat/fan controls are invalid.

        Notes:
            Successful completion records `cooling_stopped` with
            `recovery_after_fault: true`, updates the session controls, and
            preserves `phase: fault` / `active: false`.
        """
        with self._lock:
            self._assert_latest_session(session)
            if (
                reservation.session_id != session.id
                or session.pending_driver_command_token != reservation.token
                or session.pending_driver_command_kind != reservation.kind
            ):
                raise SessionLifecycleError("Driver command reservation is no longer active.")
            if session.active or session.faulted_at_utc is None or session.phase != "fault":
                raise SessionLifecycleError(
                    "Recovery cooling stop is only allowed after an emergency stop."
                )
            if cooling_on:
                raise SessionLifecycleError(
                    "Driver still reports cooling active after stop_cooling."
                )
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            validated_fan = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )
            if validated_heat != 0:
                raise SessionLifecycleError("Heat must be off after post-fault cooling recovery.")
            try:
                event = self._record_stopped_session_event_locked(
                    session,
                    "cooling_stopped",
                    payload={
                        "heat_level_percent": validated_heat,
                        "fan_level_percent": validated_fan,
                        "cooling_on": False,
                        "recovery_after_fault": True,
                    },
                )
                session.heat_level_percent = validated_heat
                session.fan_level_percent = validated_fan
                session.cooling_on = False
                session.phase = "fault"
            finally:
                self._clear_driver_command_reservation_locked(session, reservation)
            return event, _copy_session_for_read(session)

    def clear_driver_command_reservation(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> None:
        """Clear a pending driver command reservation if it is still current."""
        with self._lock:
            if session.pending_driver_command_token == reservation.token:
                self._clear_driver_command_reservation_locked(session, reservation)

    def retain_fault_recovery_block(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        safety_payload: Mapping[str, EventPayloadValue] | None = None,
    ) -> None:
        """Pessimistically retain a stopped fault block after recovery uncertainty.

        This narrow fallback is used only when recording the emergency-stop
        result itself fails after a guarded driver command may have actuated.
        It keeps new-session admission closed without claiming a successful
        recovery action.
        """
        with self._lock:
            self._assert_latest_session(session)
            if session.active:
                raise SessionLifecycleError("Cannot retain a fault block for an active session.")
            _apply_emergency_safety_payload(
                session,
                default_emergency_safety_payload() if safety_payload is None else safety_payload,
            )
            session.phase = "fault"
            self._fault_recovery_admission_blocks.add(session.id)
            if session.faulted_at_utc is None:
                session.faulted_at_utc = self._utc_now()
                session.faulted_monotonic_seconds = session.elapsed_monotonic_seconds(
                    self._monotonic_now
                )
            self._clear_driver_command_reservation_locked(session, reservation)

    def retain_fault_block(
        self,
        session: RoastSession,
        *,
        safety_payload: Mapping[str, EventPayloadValue] | None = None,
    ) -> None:
        """Retain a pessimistic stopped-fault state without a command reservation.

        This is the final containment path when an emergency-stop result cannot
        be recorded. It intentionally keeps new-session admission closed by
        treating cooling as active or unknown.
        """
        with self._lock:
            self._assert_latest_session(session)
            if session.active:
                raise SessionLifecycleError("Cannot retain a fault block for an active session.")
            _apply_emergency_safety_payload(
                session,
                default_emergency_safety_payload() if safety_payload is None else safety_payload,
            )
            session.phase = "fault"
            self._fault_recovery_admission_blocks.add(session.id)
            if session.faulted_at_utc is None:
                session.faulted_at_utc = self._utc_now()
                session.faulted_monotonic_seconds = session.elapsed_monotonic_seconds(
                    self._monotonic_now
                )

    def retain_active_fault_block(
        self,
        session: RoastSession,
        *,
        safety_payload: Mapping[str, EventPayloadValue] | None = None,
    ) -> None:
        """Fail closed when an active emergency-stop fault cannot be recorded.

        This in-memory fallback preserves the driver safety payload and closes
        active control, recovery, and new-session admission without claiming
        that event persistence succeeded.
        """
        with self._lock:
            self._assert_latest_session(session)
            _apply_emergency_safety_payload(
                session,
                default_emergency_safety_payload() if safety_payload is None else safety_payload,
            )
            if session.active:
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="fault",
                )
            else:
                session.phase = "fault"
            self._fault_recovery_admission_blocks.add(session.id)
            if session.faulted_at_utc is None:
                session.faulted_at_utc = self._utc_now()
                session.faulted_monotonic_seconds = session.elapsed_monotonic_seconds(
                    self._monotonic_now
                )
            self._abort_finalisation_for_emergency_stop_locked(session)

    def retain_active_verified_emergency_containment(
        self,
        session: RoastSession,
        *,
        safety_payload: Mapping[str, EventPayloadValue],
    ) -> None:
        """Retain verified containment when its active fault row cannot be saved.

        This keeps the accepted driver result distinct from an unverified
        emergency stop: no fault row is added and the incomplete recording
        remains visible through the session's existing persistence evidence.
        Callers must restrict this exceptional admission to the composed
        manual/mock writer route.

        Args:
            session: Latest active session whose fault row was not saved.
            safety_payload: Independently verified driver containment evidence.
        """
        with self._lock:
            self._assert_latest_session(session)
            if not has_verified_emergency_containment(safety_payload=safety_payload):
                raise SessionLifecycleError(
                    "Verified emergency containment is required before recovery admission."
                )
            _apply_emergency_safety_payload(session, safety_payload)
            if session.active:
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="fault",
                )
            else:
                session.phase = "fault"
            if session.faulted_at_utc is None:
                session.faulted_at_utc = self._utc_now()
                session.faulted_monotonic_seconds = session.elapsed_monotonic_seconds(
                    self._monotonic_now
                )
            self._fault_recovery_admission_blocks.discard(session.id)
            self._abort_finalisation_for_emergency_stop_locked(session)

    def settle_known_incomplete_fault_recovery(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
        connected: bool,
    ) -> RoastSession | None:
        """Retain one verified guarded result when its canonical generation failed.

        The returned projection is driver evidence only.  This method never
        appends a recovery event or timestamps one, so it cannot turn an
        accepted command into a saved record.

        Args:
            session: Latest stopped fault session owning the recovery command.
            reservation: Exact guarded recovery reservation.
            heat_level_percent: Driver-reported heat after the command.
            fan_level_percent: Driver-reported fan after the command.
            cooling_on: Driver-reported cooling state after the command.
            connected: Whether the driver remained connected after the command.

        Returns:
            A projection of the accepted unsaved result, or ``None`` when the
            result cannot be proved safe enough to retain.
        """
        with self._lock:
            if not session.mock_log_torn:
                return None
            try:
                self._assert_stopped_fault_recovery_session_locked(session)
                if (
                    reservation.session_id != session.id
                    or session.pending_driver_command_token != reservation.token
                    or session.pending_driver_command_kind != reservation.kind
                    or reservation.kind not in {"drop", "start_cooling", "stop_cooling"}
                    or not connected
                ):
                    return None
                heat = validate_control_percent(heat_level_percent, label="heat_level_percent")
                fan = validate_control_percent(fan_level_percent, label="fan_level_percent")
            except SessionLifecycleError:
                return None
            if heat != 0:
                return None
            if reservation.kind in {"drop", "start_cooling"} and (fan != 100 or not cooling_on):
                return None
            if reservation.kind == "stop_cooling" and cooling_on:
                return None
            if reservation.kind == "drop":
                persisted_drop = self._get_existing_singleton_event(session, "beans_dropped")
                if (
                    session.beans_added_at_utc is None
                    or session.unrecorded_fault_recovery_drop_accepted
                ):
                    return None
                if persisted_drop is None:
                    session.unrecorded_fault_recovery_drop_accepted = True
                elif (
                    persisted_drop.payload.get("recovery_after_fault") is not True
                    or self._get_existing_singleton_event(session, "cooling_started") is not None
                ):
                    # Only the first, verified row of this exact composed
                    # recovery Drop may survive a failed derived cooling row.
                    # Any other existing endpoint is not evidence for this
                    # reservation and retains the established fail-closed path.
                    return None
            elif reservation.kind == "stop_cooling" and (
                session.beans_added_at_utc is not None
                and not _has_recorded_beans_dropped(session)
                and not session.unrecorded_fault_recovery_drop_accepted
                and not self._has_verified_known_zero_ordinary_discharge_locked(session)
            ):
                return None
            if (
                reservation.kind == "stop_cooling"
                and self._has_verified_known_zero_ordinary_discharge_locked(session)
            ):
                # The exact reservation and returned connected zero-heat,
                # fan-100, cooling-off state have both been checked above.
                session.ordinary_drop_persistence_zero_cooling_settled = True
            session.heat_level_percent = heat
            session.fan_level_percent = fan
            session.cooling_on = cooling_on
            session.phase = "fault"
            self._clear_driver_command_reservation_locked(session, reservation)
            self._clear_fault_recovery_in_flight_locked(session, reservation)
            return _copy_session_for_read(session)

    def clear_fault_recovery_in_flight(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> None:
        """Retire a guarded recovery I/O fence after safe containment completes."""
        with self._lock:
            self._clear_fault_recovery_in_flight_locked(session, reservation)

    def complete_reserved_driver_fault_recovery_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
        connected: bool = True,
    ) -> tuple[RoastEvent, RoastSession]:
        """Complete one guarded stopped-fault recovery command.

        The completion records only the command's actual lifecycle event while
        retaining the terminal fault state. It rejects a non-zero heat report,
        so a recovery command cannot report success after the driver reports
        heat enabled.

        Args:
            session: Latest stopped fault session that owns the reservation.
            reservation: Reservation returned by
                :meth:`reserve_driver_fault_recovery`.
            heat_level_percent: Driver-reported heat after the command.
            fan_level_percent: Driver-reported fan after the command.
            cooling_on: Driver-reported cooling state after the command.
            connected: Whether the driver remained connected after the command.

        Returns:
            The authoritative event and an atomic session snapshot.

        Raises:
            SessionLifecycleError: If the reservation/session is stale, heat is
            non-zero, or the command did not establish its required state.
        """
        if session.canonical_writer is not None:
            return self._complete_reserved_canonical_fault_recovery_snapshot(
                session,
                reservation=reservation,
                heat_level_percent=heat_level_percent,
                fan_level_percent=fan_level_percent,
                cooling_on=cooling_on,
                connected=connected,
            )
        completed = False
        with self._lock:
            try:
                self._assert_stopped_fault_recovery_session_locked(session)
                if (
                    reservation.session_id != session.id
                    or session.pending_driver_command_token != reservation.token
                    or session.pending_driver_command_kind != reservation.kind
                ):
                    raise SessionLifecycleError("Driver command reservation is no longer active.")
                validated_heat = validate_control_percent(
                    heat_level_percent,
                    label="heat_level_percent",
                )
                validated_fan = validate_control_percent(
                    fan_level_percent,
                    label="fan_level_percent",
                )
                if validated_heat != 0:
                    raise SessionLifecycleError("Heat must be off after fault recovery.")
                if not connected:
                    raise SessionLifecycleError("Driver disconnected during fault recovery.")
                if reservation.kind in ("drop", "start_cooling") and (
                    not cooling_on or validated_fan != 100
                ):
                    raise SessionLifecycleError(
                        "Fault recovery cooling requires fan 100 and cooling active."
                    )
                if (
                    self._normal_drop_persistence_torn or session.mock_log_torn
                ) and reservation.kind in {
                    "drop",
                    "start_cooling",
                    "stop_cooling",
                }:
                    raise EventLogRecordingError

                payload: dict[str, EventPayloadValue] = {
                    "heat_level_percent": validated_heat,
                    "fan_level_percent": validated_fan,
                    "cooling_on": cooling_on,
                    "recovery_after_fault": True,
                }
                if reservation.kind == "drop":
                    event = self._record_stopped_session_event_locked(
                        session,
                        "beans_dropped",
                        payload=payload,
                    )
                    self._record_stopped_session_event_locked(
                        session,
                        "cooling_started",
                        payload=payload,
                    )
                elif reservation.kind == "start_cooling":
                    if not cooling_on:
                        raise SessionLifecycleError(
                            "Driver still reports cooling inactive after start_cooling."
                        )
                    event = self._record_stopped_session_event_locked(
                        session,
                        "cooling_started",
                        payload=payload,
                    )
                elif reservation.kind == "stop_cooling":
                    if cooling_on:
                        raise SessionLifecycleError(
                            "Driver still reports cooling active after stop_cooling."
                        )
                    event = self._record_stopped_session_event_locked(
                        session,
                        "cooling_stopped",
                        payload=payload,
                    )
                    self._fault_drop_cooling_cycle_required.discard(session.id)
                else:
                    raise SessionLifecycleError("Unsupported stopped-fault recovery command.")
                session.heat_level_percent = validated_heat
                session.fan_level_percent = validated_fan
                session.cooling_on = cooling_on
                session.phase = "fault"
                completed = True
                return event, _copy_session_for_read(session)
            finally:
                # A torn exact Drop log can reject the recovery event after a
                # qualifying stop-cooling driver result is known. Keep only
                # that reservation for the caller's narrow known-state
                # settlement; every other failure retains eager clearing.
                if not (
                    (self._normal_drop_persistence_torn or session.mock_log_torn)
                    and reservation.kind in {"drop", "start_cooling", "stop_cooling"}
                ):
                    self._clear_driver_command_reservation_locked(session, reservation)
                if completed:
                    self._clear_fault_recovery_in_flight_locked(session, reservation)

    def _complete_reserved_canonical_fault_recovery_snapshot(
        self,
        session: RoastSession,
        *,
        reservation: DriverCommandReservation,
        heat_level_percent: int,
        fan_level_percent: int,
        cooling_on: bool,
        connected: bool,
    ) -> tuple[RoastEvent, RoastSession]:
        """Settle one composed recovery command without holding the store lock on I/O.

        The recovery reservation remains the command's authority while its
        exact row is owned by the generation writer.  In particular, a second
        emergency may cancel that reservation while the write is stalled; the
        later settlement then cannot project stale recovery state or admit the
        derived cooling row.
        """
        writer = session.canonical_writer
        assert writer is not None
        with self._lock:
            self._assert_stopped_fault_recovery_session_locked(session)
            if (
                reservation.session_id != session.id
                or session.pending_driver_command_token != reservation.token
                or session.pending_driver_command_kind != reservation.kind
            ):
                # No canonical turn owns this result yet.  The driver has
                # returned, but its completion is still ambiguous, so leave
                # the established guarded-recovery replacement path intact.
                raise SessionLifecycleError("Driver command reservation is no longer active.")
            validated_heat = validate_control_percent(
                heat_level_percent,
                label="heat_level_percent",
            )
            validated_fan = validate_control_percent(
                fan_level_percent,
                label="fan_level_percent",
            )
            if validated_heat != 0:
                raise SessionLifecycleError("Heat must be off after fault recovery.")
            if not connected:
                raise SessionLifecycleError("Driver disconnected during fault recovery.")
            if reservation.kind in ("drop", "start_cooling") and (
                not cooling_on or validated_fan != 100
            ):
                raise SessionLifecycleError(
                    "Fault recovery cooling requires fan 100 and cooling active."
                )
            if reservation.kind == "stop_cooling" and cooling_on:
                raise SessionLifecycleError(
                    "Fault recovery cooling stop requires cooling inactive."
                )
            if self._normal_drop_persistence_torn or session.mock_log_torn:
                raise EventLogRecordingError
            payload: dict[str, EventPayloadValue] = {
                "heat_level_percent": validated_heat,
                "fan_level_percent": validated_fan,
                "cooling_on": cooling_on,
                "recovery_after_fault": True,
            }
            if reservation.kind == "drop":
                kind: RoastEventKind = "beans_dropped"
            elif reservation.kind == "start_cooling":
                kind = "cooling_started"
            elif reservation.kind == "stop_cooling":
                kind = "cooling_stopped"
            else:  # pragma: no cover - DriverCommandReservation is closed.
                raise SessionLifecycleError("Unsupported stopped-fault recovery command.")
            event = self._new_stopped_recovery_event_locked(session, kind, payload)
            completion = writer.submit(_event_log_row(session, event), lane="event")

        # Never wait for the writer while owning store admission.  State and
        # emergency callers must be able to acquire it while this exact row is
        # stalled, and the reservation remains the only recovery authority.
        completion.result()

        derived: RoastEvent | None = None
        with self._lock:
            self._assert_stopped_fault_recovery_session_locked(session)
            if (
                reservation.session_id != session.id
                or session.pending_driver_command_token != reservation.token
                or session.pending_driver_command_kind != reservation.kind
            ):
                # The exact row is now read-back verified.  Retain that fact
                # without projecting its mutable phase/control consequences;
                # it fences a repeat Drop but cannot authorize a fresh Start.
                session.event_timeline.append(event)
                if reservation.kind == "drop":
                    self._historical_fault_drop_start_blocks.add(session.id)
                self._clear_fault_recovery_in_flight_locked(session, reservation)
                raise RecoveryPersistenceSupersededError(
                    "Driver command reservation was superseded during persistence."
                )
            session.event_timeline.append(event)
            if reservation.kind == "drop":
                derived = self._new_stopped_recovery_event_locked(
                    session,
                    "cooling_started",
                    payload,
                )
                derived_completion = writer.submit(_event_log_row(session, derived), lane="event")
            else:
                derived_completion = None

        if derived_completion is not None:
            # The paired cooling fact is admitted only after the exact Drop is
            # verified.  A superseding emergency before this admission leaves
            # no ineligible trailing cooling row behind.
            derived_completion.result()

        with self._lock:
            self._assert_stopped_fault_recovery_session_locked(session)
            if (
                reservation.session_id != session.id
                or session.pending_driver_command_token != reservation.token
                or session.pending_driver_command_kind != reservation.kind
            ):
                if derived_completion is not None:
                    assert derived is not None
                    session.event_timeline.append(derived)
                self._clear_fault_recovery_in_flight_locked(session, reservation)
                raise RecoveryPersistenceSupersededError(
                    "Driver command reservation was superseded during persistence."
                )
            if derived_completion is not None:
                assert derived is not None
                session.event_timeline.append(derived)
            session.heat_level_percent = validated_heat
            session.fan_level_percent = validated_fan
            session.cooling_on = cooling_on
            session.phase = "fault"
            if reservation.kind == "stop_cooling":
                self._fault_drop_cooling_cycle_required.discard(session.id)
            self._clear_driver_command_reservation_locked(session, reservation)
            self._clear_fault_recovery_in_flight_locked(session, reservation)
            return event, _copy_session_for_read(session)

    def _new_stopped_recovery_event_locked(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        payload: dict[str, EventPayloadValue],
    ) -> RoastEvent:
        """Build one ordered stopped-fault recovery fact under store admission."""
        existing_event = self._get_existing_singleton_event(session, kind)
        if existing_event is not None and kind not in {"cooling_started", "cooling_stopped"}:
            raise SessionLifecycleError("Stopped-fault recovery event already exists.")
        recorded_at_utc = _normalize_event_recorded_at_utc(session, recorded_at_utc=self._utc_now())
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=max(0.0, self._monotonic_now() - session.monotonic_start),
        )
        _validate_event_monotonic_order(session, kind=kind, monotonic_seconds=monotonic_seconds)
        return RoastEvent(
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload=dict(payload),
        )

    def retain_fault_drop_ambiguity(self, session: RoastSession) -> None:
        """Fence admission after a drop may have actuated without durable evidence."""
        with self._lock:
            self._assert_latest_session(session)
            self._fault_drop_ambiguity_required.add(session.id)

    def retain_fault_drop_ambiguity_if_unrecorded(self, session: RoastSession) -> None:
        """Fence a failed drop only when no durable drop event was recorded."""
        with self._lock:
            self._assert_latest_session(session)
            if not _has_recorded_beans_dropped(session):
                self._fault_drop_ambiguity_required.add(session.id)

    def retain_fault_recovery_admission_block(self, session: RoastSession) -> None:
        """Keep recovery admission closed until explicit verified containment."""
        with self._lock:
            self._assert_latest_session(session)
            self._fault_recovery_admission_blocks.add(session.id)

    def cancel_nonfinalisation_driver_command(self, session: RoastSession) -> None:
        """Cancel an ordinary token without resolving a normal-drop fence."""
        with self._lock:
            self._assert_latest_session(session)
            if session.pending_driver_command_kind != "finalisation":
                session.pending_driver_command_token = None
                session.pending_driver_command_kind = None

    def start_cooling(self, session: RoastSession) -> RoastEvent:
        """Start cooling for one active session."""
        with self._lock:
            self._assert_latest_active_session(session)
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling can only start after beans are dropped.")
            return self.record_event(session, "cooling_started")

    def start_cooling_snapshot(self, session: RoastSession) -> tuple[RoastEvent, RoastSession]:
        """Start cooling and return the event plus an atomic lightweight snapshot."""
        with self._lock:
            event = self.start_cooling(session)
            return event, _copy_session_for_read(session)

    def stop_cooling(self, session: RoastSession) -> RoastEvent:
        """Stop cooling for one active session."""
        with self._lock:
            self._assert_latest_active_session(session)
            if _has_unrecorded_charge_attempt(session):
                raise SessionLifecycleError(
                    "Cooling cannot stop while a charge attempt has no canonical record."
                )
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling cannot stop before beans are dropped.")
            if session.cooling_started_at_utc is None or not session.cooling_on:
                raise SessionLifecycleError("Cooling must be started before it can be stopped.")
            event = self.record_event(session, "cooling_stopped")
            session.stop(
                utc_now=self._utc_now,
                monotonic_now=self._monotonic_now,
                phase="complete",
            )
            return event

    def stop_cooling_snapshot(self, session: RoastSession) -> tuple[RoastEvent, RoastSession]:
        """Stop cooling and return the event plus an atomic lightweight snapshot."""
        with self._lock:
            event = self.stop_cooling(session)
            return event, _copy_session_for_read(session)

    def begin_manual_first_crack_write(
        self,
        session: RoastSession,
        *,
        observer_publication_token: object | None = None,
        on_live: Callable[[RoastSession, object], None] | None = None,
        started_monotonic_seconds: float | None = None,
    ) -> ManualFirstCrackAttempt:
        """Stage one manual first-crack row on the private mock writer.

        This deliberately reserves no driver capability.  The short store
        section captures only the exact session and clock identity; audit and
        canonical filesystem work run on the owned worker after that section.
        """
        with self._lock:
            if not self._manual_first_crack_writer_enabled:
                raise SessionLifecycleError("Manual first-crack writer is unavailable.")
            self._assert_latest_active_session(session)
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            if session.faulted_at_utc is not None:
                raise SessionLifecycleError("No non-fault events can be recorded after a fault.")
            existing = self._get_existing_singleton_event(session, "first_crack_detected")
            if existing is not None:
                completed: Future[ManualFirstCrackSettlement] = Future()
                completed.set_result(
                    ManualFirstCrackSettlement(
                        outcome="duplicate",
                        event=existing,
                        snapshot=_copy_session_for_read(session),
                    )
                )
                return ManualFirstCrackAttempt(
                    token="duplicate",
                    started_monotonic_seconds=self._monotonic_now(),
                    deadline_monotonic_seconds=self._monotonic_now(),
                    completion=completed,
                )
            if session.manual_first_crack_endpoint_admitted:
                raise SessionLifecycleError(
                    "Manual first-crack mark cannot be admitted after a lifecycle endpoint."
                )
            if (
                self._manual_first_crack_attempt is not None
                and self._manual_first_crack_attempt_session_id == session.id
                and not self._manual_first_crack_attempt.completion.done()
            ):
                return self._manual_first_crack_attempt
            if (
                session.manual_first_crack_admission_expired
                or session.manual_first_crack_canonical_evidence in {"zero", "uncertain"}
            ):
                raise SessionLifecycleError(
                    "Manual first-crack mark is pending or incomplete; do not resend it."
                )
            _validate_event_transition(session, "first_crack_detected")
            started_monotonic_seconds = (
                self._monotonic_now()
                if started_monotonic_seconds is None
                else started_monotonic_seconds
            )
            deadline_monotonic_seconds = (
                started_monotonic_seconds + self._manual_first_crack_budget_seconds
            )
            token = uuid4().hex
            event = self._new_event_locked(session, "first_crack_detected")
            # Bind audit evidence to the original mark, never to a later
            # session or writer selected after an endpoint races this attempt.
            self._manual_first_crack_audit_identities[token] = _ManualFirstCrackAuditIdentity(
                recorded_at_utc=event.recorded_at_utc,
                monotonic_seconds=event.monotonic_seconds,
                capture_ownership_sequence=session.capture_ownership_sequence,
                session_id=session.id,
                writer_generation=id(session.canonical_writer),
            )
            # Pending is visibly non-live and blocks only the exact recipe path
            # through its existing missing-canonical-FC check.
            session.manual_first_crack_pending = True
            writer = self._manual_first_crack_writers.get(session.id)
            if writer is None:
                if (
                    len(self._manual_first_crack_writers)
                    >= self._manual_first_crack_writer_capacity
                ):
                    session.manual_first_crack_pending = False
                    raise SessionLifecycleError(
                        "Manual first-crack writer capacity is unavailable."
                    )
                writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manual-fc-writer")
                self._manual_first_crack_writers[session.id] = writer
            completion: Future[ManualFirstCrackSettlement] = Future()
            work_completion = writer.submit(
                self._write_manual_first_crack,
                session,
                token,
                event,
                deadline_monotonic_seconds,
                on_live,
                observer_publication_token,
                completion,
            )
            attempt = ManualFirstCrackAttempt(
                token=token,
                started_monotonic_seconds=started_monotonic_seconds,
                deadline_monotonic_seconds=deadline_monotonic_seconds,
                completion=completion,
                observer_publication_token=observer_publication_token,
                on_live=on_live,
            )
            self._manual_first_crack_attempt = attempt
            self._manual_first_crack_attempt_session_id = session.id
            work_completion.add_done_callback(
                lambda _: self._release_manual_first_crack_writer(session.id, writer)
            )

            def settle_caller(
                finished: Future[ManualFirstCrackSettlement],
            ) -> None:
                """Settle non-live outcomes after their owned audit work ends."""
                if completion.done():
                    return
                try:
                    completion.set_result(finished.result())
                except BaseException as error:
                    completion.set_exception(error)

            work_completion.add_done_callback(settle_caller)
            return attempt

    def manual_first_crack_monotonic_now(self) -> float:
        """Return the injected monotonic clock used by manual-mark deadlines."""
        return self._monotonic_now()

    def manual_first_crack_remaining_seconds(self, attempt: ManualFirstCrackAttempt) -> float:
        """Return remaining producer budget in the attempt's clock domain."""
        return attempt.deadline_monotonic_seconds - self._monotonic_now()

    def revoke_manual_first_crack_live_publication(self, attempt: ManualFirstCrackAttempt) -> None:
        """Irrevocably revoke live projection after the producer wait expires."""
        self.settle_or_revoke_manual_first_crack_live_publication(attempt)

    def settle_or_revoke_manual_first_crack_live_publication(
        self, attempt: ManualFirstCrackAttempt
    ) -> ManualFirstCrackSettlement | None:
        """Atomically retain a live settlement or revoke this attempt's claim.

        The completion future is resolved under this same store lock when a
        live publication wins. Reading an already-complete future cannot
        block, and observer callbacks remain outside the lock.
        """
        if attempt.token == "duplicate":
            return
        with self._lock:
            if attempt.completion.done():
                settlement = attempt.completion.result()
                if settlement.outcome == "live":
                    return settlement
            self._manual_first_crack_live_revoked_tokens.add(attempt.token)
            session = self._sessions_by_id.get(self._manual_first_crack_attempt_session_id or "")
            if (
                session is not None
                and self._manual_first_crack_attempt is attempt
                and self._get_existing_singleton_event(session, "first_crack_detected") is None
            ):
                session.manual_first_crack_admission_expired = True
        return None

    def revoke_unconsumed_manual_first_crack_live_publication(self, session: RoastSession) -> None:
        """Revoke a pending mark's live right at a mutable endpoint admission."""
        with self._lock:
            self._revoke_unconsumed_manual_first_crack_live_locked(session)

    def admit_manual_first_crack_endpoint(self, session: RoastSession) -> None:
        """Close composed manual-mark admission when its session is still active.

        A concurrently completed captured session is intentionally untouched:
        its completed lifecycle cannot acquire a new live mark, and an
        emergency already eligible for that exact latest session must not be
        refused by an active-only assertion.
        """
        with self._lock:
            self._assert_latest_session(session)
            if session.active:
                self._admit_manual_first_crack_endpoint_locked(session)

    def _admit_manual_first_crack_endpoint_locked(self, session: RoastSession) -> None:
        """Record endpoint ownership before revoking an existing pending mark."""
        session.manual_first_crack_endpoint_admitted = True
        self._revoke_unconsumed_manual_first_crack_live_locked(session)

    def _revoke_unconsumed_manual_first_crack_live_locked(self, session: RoastSession) -> None:
        """Revoke only the current generation's not-yet-live mark under admission."""
        attempt = self._manual_first_crack_attempt
        if (
            attempt is None
            or self._manual_first_crack_attempt_session_id != session.id
            or attempt.completion.done()
            or self._get_existing_singleton_event(session, "first_crack_detected") is not None
        ):
            return
        self._manual_first_crack_live_revoked_tokens.add(attempt.token)
        session.manual_first_crack_admission_expired = True

    def _release_manual_first_crack_writer(
        self, session_id: str, writer: ThreadPoolExecutor
    ) -> None:
        """Release an idle generation lane without cancelling owned I/O."""
        with self._lock:
            if self._manual_first_crack_writers.get(session_id) is writer:
                del self._manual_first_crack_writers[session_id]
        writer.shutdown(wait=False, cancel_futures=False)

    def shutdown_manual_first_crack_writer(self) -> None:
        """Stop accepting private writer work without cancelling an owned write."""
        with self._lock:
            writers = tuple(self._manual_first_crack_writers.values())
        for writer in writers:
            writer.shutdown(wait=False, cancel_futures=False)
        with self._lock:
            canonical_writers = [
                session.canonical_writer
                for session in self._sessions_by_id.values()
                if session.canonical_writer is not None
            ]
        for canonical_writer in canonical_writers:
            canonical_writer.shutdown()

    def _write_manual_first_crack(
        self,
        session: RoastSession,
        token: str,
        event: RoastEvent,
        deadline_monotonic_seconds: float,
        on_live: Callable[[RoastSession, object], None] | None,
        observer_publication_token: object | None,
        caller_completion: Future[ManualFirstCrackSettlement],
    ) -> ManualFirstCrackSettlement:
        """Own audit, canonical append, verification and settlement for one mark."""
        try:
            self._append_manual_first_crack_audit(session, token=token, disposition="begin")
        except Exception:
            return self._settle_manual_first_crack_failure(
                session,
                token=token,
                canonical_evidence="zero",
                audit_unresolved=True,
            )

        completion: Future[None] | None = None
        canonical_evidence: Literal["zero", "uncertain"] | None = None
        with self._lock:
            if (
                self._latest_session is not session
                or not session.active
                or session.faulted_at_utc is not None
                or token in self._manual_first_crack_live_revoked_tokens
                or self._monotonic_now() >= deadline_monotonic_seconds
            ):
                historical = self._settle_manual_first_crack_historical_locked(
                    session, token=token, canonical_evidence="zero"
                )
            else:
                historical = None
                writer = session.canonical_writer
                try:
                    _validate_event_transition(session, "first_crack_detected")
                    _validate_event_monotonic_order(
                        session,
                        kind="first_crack_detected",
                        monotonic_seconds=event.monotonic_seconds,
                    )
                except SessionLifecycleError:
                    historical = self._settle_manual_first_crack_historical_locked(
                        session, token=token, canonical_evidence="zero"
                    )
                if historical is None and writer is None:
                    historical = self._settle_manual_first_crack_historical_locked(
                        session, token=token, canonical_evidence="zero"
                    )
                elif historical is None:
                    # This is the irreversible admission point.  Reserving the
                    # generation-owned event lane and enqueuing its exact frame
                    # occur while endpoint admission still has this store lock.
                    # The worker may perform I/O immediately, but no completion
                    # is awaited until after this critical section has ended.
                    try:
                        assert writer is not None
                        self._canonical_event_admissions.add((session.id, "first_crack_detected"))
                        completion = writer.submit(_event_log_row(session, event), lane="event")
                    except _CanonicalAdmissionRefusedError:
                        self._canonical_event_admissions.discard(
                            (session.id, "first_crack_detected")
                        )
                        canonical_evidence = "zero"
                    except Exception:
                        self._canonical_event_admissions.discard(
                            (session.id, "first_crack_detected")
                        )
                        canonical_evidence = "uncertain"
        if canonical_evidence is not None:
            return self._settle_manual_first_crack_failure(
                session, token=token, canonical_evidence=canonical_evidence
            )
        if historical is not None:
            self._append_manual_first_crack_terminal_audit(
                session, token=token, disposition="historical"
            )
            return historical

        try:
            if completion is None:
                raise EventLogRecordingError("Manual first-crack record was not admitted.")
            completion.result()
        except _CanonicalAdmissionRefusedError:
            return self._settle_manual_first_crack_failure(
                session, token=token, canonical_evidence="zero"
            )
        except Exception:
            return self._settle_manual_first_crack_failure(
                session, token=token, canonical_evidence="uncertain"
            )

        snapshot: RoastSession | None = None
        settlement: ManualFirstCrackSettlement | None = None
        with self._lock:
            # A command endpoint, expiry, replacement or fault may have won
            # while the exact accepted bytes were being written.  Such bytes
            # remain audit evidence only and never re-enter the live timeline.
            if (
                self._latest_session is not session
                or not session.active
                or session.faulted_at_utc is not None
                or token in self._manual_first_crack_live_revoked_tokens
                or self._monotonic_now() >= deadline_monotonic_seconds
            ):
                historical = self._settle_manual_first_crack_historical_locked(
                    session, token=token, canonical_evidence="verified"
                )
            else:
                try:
                    _validate_event_transition(session, "first_crack_detected")
                    _validate_event_monotonic_order(
                        session,
                        kind="first_crack_detected",
                        monotonic_seconds=event.monotonic_seconds,
                    )
                except SessionLifecycleError:
                    historical = self._settle_manual_first_crack_historical_locked(
                        session, token=token, canonical_evidence="verified"
                    )
                else:
                    historical = None
                    session.event_timeline.append(event)
                    _apply_event_timestamp(session, event)
                    session.first_crack_source = "manual_mark"
                    session.manual_first_crack_pending = False
                    snapshot = _copy_session_for_read(session)
                    self._canonical_event_admissions.discard((session.id, "first_crack_detected"))
                    # Publish the caller-visible terminal result in the same
                    # admission section that chooses live over historical.
                    # A simultaneous deadline revocation can therefore only
                    # observe an unresolved attempt or this exact settlement.
                    settlement = ManualFirstCrackSettlement(
                        outcome="live", event=event, snapshot=snapshot
                    )
                    caller_completion.set_result(settlement)
        if historical is not None:
            self._append_manual_first_crack_terminal_audit(
                session, token=token, disposition="historical"
            )
            return historical
        assert snapshot is not None and settlement is not None
        # The worker retains the captured claim until it consumes it or
        # another endpoint invalidates it.
        if on_live is not None and observer_publication_token is not None:
            with suppress(BaseException):
                on_live(session, observer_publication_token)
        # The verified immutable producer result is available before the
        # diagnostic terminal audit. The owned worker still performs that
        # audit after settling the caller-visible future.
        self._append_manual_first_crack_terminal_audit(session, token=token, disposition="live")
        return settlement

    def _settle_manual_first_crack_historical_locked(
        self,
        session: RoastSession,
        *,
        token: str,
        canonical_evidence: Literal["verified", "zero"],
    ) -> ManualFirstCrackSettlement:
        """Settle verified bytes without allowing them to acquire live authority."""
        if self._latest_session is session:
            session.manual_first_crack_pending = False
            session.manual_first_crack_export_disqualified = True
            session.manual_first_crack_admission_expired = True
            session.manual_first_crack_canonical_evidence = canonical_evidence
            snapshot = _copy_session_for_read(session)
        else:
            snapshot = None
        self._manual_first_crack_live_revoked_tokens.discard(token)
        self._canonical_event_admissions.discard((session.id, "first_crack_detected"))
        return ManualFirstCrackSettlement(outcome="historical", snapshot=snapshot)

    def _append_manual_first_crack_terminal_audit(
        self, session: RoastSession, *, token: str, disposition: str
    ) -> None:
        """Write terminal audit evidence without changing a settled result."""
        try:
            self._append_manual_first_crack_audit(session, token=token, disposition=disposition)
        except BaseException:
            # A terminal audit is diagnostic-only after the canonical result
            # has settled, but its failure remains truthful private evidence.
            with self._lock:
                if self._latest_session is session:
                    session.manual_first_crack_audit_unresolved = True
        finally:
            with self._lock:
                self._manual_first_crack_audit_identities.pop(token, None)

    def _settle_manual_first_crack_failure(
        self,
        session: RoastSession,
        *,
        token: str,
        canonical_evidence: Literal["zero", "uncertain"],
        audit_unresolved: bool = False,
    ) -> ManualFirstCrackSettlement:
        """Latch an incomplete manual writer generation without driver side effects."""
        with self._lock:
            if self._latest_session is session:
                session.manual_first_crack_pending = False
                session.manual_first_crack_export_disqualified = True
                if canonical_evidence == "uncertain":
                    session.mock_log_torn = True
                session.manual_first_crack_admission_expired = True
                session.manual_first_crack_canonical_evidence = canonical_evidence
                session.manual_first_crack_audit_unresolved |= audit_unresolved
                snapshot = _copy_session_for_read(session)
            else:
                snapshot = None
            self._manual_first_crack_live_revoked_tokens.discard(token)
            self._canonical_event_admissions.discard((session.id, "first_crack_detected"))
        self._append_manual_first_crack_terminal_audit(session, token=token, disposition="failed")
        return ManualFirstCrackSettlement(outcome="failed", snapshot=snapshot)

    def _append_manual_first_crack_audit(
        self, session: RoastSession, *, token: str, disposition: str
    ) -> None:
        """Append and verify one framed non-canonical mark audit row outside admission."""
        if session.log_writer is None:
            raise EventLogRecordingError("Manual first-crack audit has no log target.")
        path = session.log_writer.log_dir / "first-crack-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, object] = {
            "version": 1,
            "type": "manual_first_crack_audit",
            "session_id": session.id,
            "token": token,
            "disposition": disposition,
        }
        with self._lock:
            identity = self._manual_first_crack_audit_identities.get(token)
        if identity is not None:
            row.update(
                {
                    "session_id": identity.session_id,
                    "recorded_at_utc": identity.recorded_at_utc.isoformat(),
                    "monotonic_seconds": identity.monotonic_seconds,
                    "capture_ownership_sequence": identity.capture_ownership_sequence,
                    "writer_generation": identity.writer_generation,
                }
            )
        frame = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        try:
            with path.open("ab+") as output:
                before = os.fstat(output.fileno())
                offset = output.tell()
                output.write(frame)
                output.flush()
                os.fsync(output.fileno())
                after = os.fstat(output.fileno())
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise EventLogRecordingError("Manual first-crack audit identity changed.")
                output.seek(offset)
                if output.read() != frame:
                    raise EventLogRecordingError("Manual first-crack audit verification failed.")
        except OSError as exc:
            raise EventLogRecordingError("Manual first-crack audit was not verified.") from exc

    def _append_and_verify_manual_first_crack_row(
        self, session: RoastSession, event: RoastEvent
    ) -> None:
        """Append and read back exactly one canonical first-crack JSONL frame."""
        if session.canonical_writer is not None:
            _append_event_log_row(session, event)
            return
        if session.log_writer is None:
            raise EventLogRecordingError("Manual first-crack record has no log target.")
        path = session.log_writer.log_dir / "roast.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "session_id": session.id,
            "type": "event",
            "kind": event.kind,
            "recorded_at_utc": event.recorded_at_utc.isoformat(),
            "monotonic_seconds": event.monotonic_seconds,
            "payload": dict(event.payload),
        }
        frame = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        try:
            with path.open("ab+") as output:
                before = os.fstat(output.fileno())
                offset = output.tell()
                output.write(frame)
                output.flush()
                os.fsync(output.fileno())
                after = os.fstat(output.fileno())
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise EventLogRecordingError(
                        "Manual first-crack log identity changed during append."
                    )
                output.seek(offset)
                if output.read() != frame:
                    raise EventLogRecordingError(
                        "Manual first-crack log frame verification failed."
                    )
        except OSError as exc:
            raise EventLogRecordingError(
                "Manual first-crack canonical record was not verified."
            ) from exc

    def record_event(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
        provenance: Literal["manual_mark"] | None = None,
    ) -> RoastEvent:
        """Record one authoritative session event under store-owned locking.

        Args:
            session: Session to mutate.
            kind: Event kind to record.
            payload: Optional structured event details.

        Returns:
            The recorded event. For singleton event kinds, repeated calls return
            the already-recorded event instead of appending a duplicate row.

        Raises:
            SessionLifecycleError: If the session is not the latest active
                session in this store.
        """
        if provenance is not None and (
            type(provenance) is not str
            or provenance != "manual_mark"
            or kind not in {"beans_added", "first_crack_detected"}
        ):
            raise SessionLifecycleError(
                "Manual provenance is valid only for charge or first crack."
            )
        with self._lock:
            return self._record_event_locked(
                session,
                kind,
                payload=payload,
                provenance=provenance,
            )

    def _record_event_locked(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
        provenance: Literal["manual_mark"] | None = None,
        owned_normal_drop_reservation: NormalDropReservation | None = None,
    ) -> RoastEvent:
        """Record one event while holding the store lock.

        A P2 fence closes the ordinary ``beans_dropped`` writer.  The normal
        Drop settlement is the sole caller allowed to supply its exact owned
        reservation, preventing a foreign event writer from turning a pending
        or ambiguous operation into a claimed endpoint.
        """
        self._assert_latest_active_session(session)
        if (
            kind == "beans_dropped"
            and self._normal_drop_in_flight is not None
            and (
                owned_normal_drop_reservation is None
                or self._normal_drop_fence_state != "pending"
                or not self._normal_drop_reservation_matches_locked(
                    session, owned_normal_drop_reservation
                )
            )
        ):
            raise SessionLifecycleError(
                "Beans dropped can only be recorded by the owned normal-drop settlement."
            )
        self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
        if kind != "fault":
            self._assert_finalisation_allows_mutation(session)
        if session.faulted_at_utc is not None and kind != "fault":
            raise SessionLifecycleError("No non-fault events can be recorded after a fault.")

        existing_event = self._get_existing_singleton_event(session, kind)
        if existing_event is not None:
            return existing_event
        _validate_event_transition(session, kind)

        recorded_at_utc = self._utc_now()
        monotonic_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=monotonic_seconds,
        )
        recorded_at_utc = _normalize_event_recorded_at_utc(
            session,
            recorded_at_utc=recorded_at_utc,
        )
        _validate_event_monotonic_order(
            session,
            kind=kind,
            monotonic_seconds=monotonic_seconds,
        )
        event = RoastEvent(
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload={} if payload is None else dict(payload),
        )
        _append_event_log_row(session, event)
        session.event_timeline.append(event)
        _apply_event_timestamp(session, event)
        if provenance is not None:
            if kind == "beans_added":
                session.beans_added_source = provenance
            else:
                session.first_crack_source = provenance
        return event

    def _assert_normal_drop_persistence_allows_competing_writer_locked(
        self, session: RoastSession
    ) -> None:
        """Refuse ordinary log writers while an exact Drop owns this log window."""
        if (
            session.log_writer is not None
            and self._latest_session is session
            and (
                self._normal_drop_persistence_torn
                or not self._normal_drop_persistence_last_completion.is_set()
            )
        ):
            raise SessionLifecycleError("A normal bean-drop operation is unresolved.")

    def record_event_snapshot(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
        provenance: Literal["manual_mark"] | None = None,
    ) -> tuple[RoastEvent, RoastSession]:
        """Record one event and return the event plus an atomic lightweight snapshot."""
        writer = session.canonical_writer
        if writer is None:
            with self._lock:
                event = self.record_event(session, kind, payload=payload, provenance=provenance)
                return event, _copy_session_for_read(session)

        # This is the composed mock path.  It deliberately admits the exact
        # row under the store lock, then leaves that lock before waiting for
        # the generation-owned writer.  A held ordinary mark must therefore
        # not delay a state read or emergency driver's pre-dispatch section.
        admission_key = (session.id, kind)
        completion: Future[None] | None = None
        submission_error: Exception | None = None
        with self._lock:
            if provenance is not None and (
                type(provenance) is not str
                or provenance != "manual_mark"
                or kind not in {"beans_added", "first_crack_detected"}
            ):
                raise SessionLifecycleError(
                    "Manual provenance is valid only for charge or first crack."
                )
            is_manual_charge = kind == "beans_added" and provenance is not None
            self._assert_latest_active_session(session)
            if kind == "beans_dropped" and self._normal_drop_in_flight is not None:
                raise SessionLifecycleError(
                    "Beans dropped can only be recorded by the owned normal-drop settlement."
                )
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            if kind != "fault":
                self._assert_finalisation_allows_mutation(session)
            if session.faulted_at_utc is not None and kind != "fault":
                raise SessionLifecycleError("No non-fault events can be recorded after a fault.")

            existing_event = self._get_existing_singleton_event(session, kind)
            if existing_event is not None:
                return existing_event, _copy_session_for_read(session)
            if is_manual_charge and (
                session.manual_charge_persistence_uncertain
                or session.manual_charge_persistence_zero
            ):
                # A later press has no authority to recast the original
                # automatic charge's established persistence evidence.
                raise _CanonicalAdmissionRefusedError(
                    "Original automatic charge persistence evidence is already established."
                )
            if admission_key in self._canonical_event_admissions:
                raise SessionLifecycleError("Canonical event admission is still pending.")
            _validate_event_transition(session, kind)

            recorded_at_utc = _normalize_event_recorded_at_utc(
                session, recorded_at_utc=self._utc_now()
            )
            monotonic_seconds = _normalize_event_monotonic_seconds(
                session,
                monotonic_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
            )
            _validate_event_monotonic_order(
                session,
                kind=kind,
                monotonic_seconds=monotonic_seconds,
            )
            event = RoastEvent(
                kind=kind,
                recorded_at_utc=recorded_at_utc,
                monotonic_seconds=monotonic_seconds,
                payload={} if payload is None else dict(payload),
            )
            self._canonical_event_admissions.add(admission_key)
            if is_manual_charge:
                session.manual_charge_persistence_pending = True
            # A composed human charge owns both its store admission and its
            # queued writer turn.  `submit_group` never runs inline, so the
            # subsequent settlement remains outside the store lock.
            try:
                completion = writer.submit_group((_event_log_row(session, event),), lane="event")[0]
            except Exception as error:
                submission_error = error

        try:
            if submission_error is not None:
                raise submission_error
            if completion is None:
                raise EventLogRecordingError("Canonical event record was not admitted.")
            completion.result()
        except _CanonicalAdmissionRefusedError:
            with self._lock:
                self._canonical_event_admissions.discard(admission_key)
                if is_manual_charge:
                    session.manual_charge_persistence_pending = False
                    session.manual_charge_persistence_zero = True
            raise
        except Exception:
            with self._lock:
                self._canonical_event_admissions.discard(admission_key)
                if is_manual_charge:
                    session.manual_charge_persistence_pending = False
                    session.manual_charge_persistence_uncertain = True
            raise

        with self._lock:
            try:
                # A verified human charge remains an exact historical fact
                # after an emergency, but must not revive roast phase or live
                # authority.  The fact preserves the existing guarded Drop
                # and cooling recovery path for this exact stopped session.
                if (
                    is_manual_charge
                    and self._latest_session is session
                    and not session.active
                    and session.faulted_at_utc is not None
                ):
                    _record_historical_manual_charge_locked(session, event, source=provenance)
                    return event, _copy_session_for_read(session)
                # Persistence is not authority to revive a session that a
                # fault, replacement, or endpoint changed while its row was
                # saving.
                self._assert_latest_active_session(session)
                self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
                if session.faulted_at_utc is not None:
                    raise SessionLifecycleError(
                        "No non-fault events can be recorded after a fault."
                    )
                if self._get_existing_singleton_event(session, kind) is not None:
                    raise SessionLifecycleError(
                        "Canonical event admission was displaced while saving."
                    )
                _validate_event_transition(session, kind)
                session.event_timeline.append(event)
                _apply_event_timestamp(session, event)
                if provenance is not None:
                    if kind == "beans_added":
                        session.beans_added_source = provenance
                        session.manual_charge_persistence_pending = False
                    else:
                        session.first_crack_source = provenance
                return event, _copy_session_for_read(session)
            finally:
                self._canonical_event_admissions.discard(admission_key)

    def process_auto_t0_reading_snapshot(
        self,
        session: RoastSession,
        *,
        bean_temp_c: float,
        drop_threshold_c: float,
        defer_canonical_settlement: bool = False,
        on_live_settlement: Callable[[], None] | None = None,
    ) -> tuple[RoastEvent | None, RoastSession]:
        """Process one bean-temperature reading for automatic T0 detection.

        Args:
            session: Session to mutate.
            bean_temp_c: Current bean temperature from the configured driver.
            drop_threshold_c: Required drop from max preheat temperature.

        Args:
            defer_canonical_settlement: Keep the exact accepted writer turn
                owned by its generation and settle it from its completion
                callback. This is used only by the autonomous manual/mock
                sampler so a held recorder cannot suppress later driver reads.
            on_live_settlement: Called after a deferred turn becomes the
                current session's verified live charge. It is never called for
                historical or failed settlement.

        Returns:
            The recorded beans-added event when this reading crosses the
            threshold, otherwise `None`, plus an atomic session snapshot. On
            confirmation the event is backdated to the candidate turning
            point — the most recent local-max bean-temperature sample before
            the sustained decline — not the confirmation tick (#167). The raw
            confirmation timestamp stays available in the event payload.

        Raises:
            SessionLifecycleError: If called outside the pre-T0 active phase or
                with non-finite inputs.
        """
        writer: _CanonicalGenerationWriter | None = None
        event: RoastEvent | None = None
        completion: Future[None] | None = None
        submission_error: Exception | None = None
        admission_key = (session.id, "beans_added")
        with self._lock:
            self._assert_latest_active_session(session)
            if self._finalisation_blocks_mutation(session):
                return None, _copy_session_for_read(session)
            if session.beans_added_at_utc is not None:
                return self._get_existing_singleton_event(session, "beans_added"), (
                    _copy_session_for_read(session)
                )
            # One automatic charge is one exact observation.  Once its
            # admitted writer turn has settled uncertainly, a later
            # temperature read must remain useful without submitting a new
            # charge or recasting that prior uncertainty as known-zero.
            if (
                session.manual_charge_persistence_uncertain
                or session.manual_charge_persistence_zero
            ):
                return None, _copy_session_for_read(session)
            if admission_key in self._canonical_event_admissions:
                return None, _copy_session_for_read(session)
            _validate_event_transition(session, "beans_added")
            current_temp = float(bean_temp_c)
            threshold = float(drop_threshold_c)
            if not math.isfinite(current_temp):
                raise SessionLifecycleError("Automatic T0 bean temperature must be finite.")
            if not math.isfinite(threshold) or threshold <= 0:
                raise SessionLifecycleError("Automatic T0 threshold must be greater than 0.")

            confirmation_elapsed_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
            confirmation_at_utc = self._utc_now()

            previous_max = session.auto_t0_charge_temperature_c
            if previous_max is None or current_temp >= previous_max:
                # A new local max — OR a repeat of the running max (a plateau of
                # quantized/equal sensor readings). Either way this sample is the
                # current candidate turning point: #167 wants the MOST RECENT
                # local max before the decline, so advance the timestamp to the
                # latest sample at the running max, not the first plateau sample.
                session.auto_t0_charge_temperature_c = current_temp
                session.auto_t0_charge_temperature_monotonic_seconds = confirmation_elapsed_seconds
                previous_max = current_temp

            session.auto_t0_preheat_sample_count += 1
            drop_c = previous_max - current_temp
            session.auto_t0_current_drop_c = max(0.0, drop_c)
            if session.auto_t0_preheat_sample_count < 2 or drop_c < threshold:
                return None, _copy_session_for_read(session)

            turning_point_elapsed_seconds = session.auto_t0_charge_temperature_monotonic_seconds
            if turning_point_elapsed_seconds is None:  # pragma: no cover - defensive
                # The candidate is always set with the first local max above,
                # so reaching confirmation without one is unreachable; fall
                # back to the confirmation tick rather than crash.
                turning_point_elapsed_seconds = confirmation_elapsed_seconds

            payload: dict[str, EventPayloadValue] = {
                "source": "auto_t0",
                "charge_temperature_c": round(previous_max, 3),
                "detected_bean_temperature_c": round(current_temp, 3),
                "drop_c": round(drop_c, 3),
                "drop_threshold_c": round(threshold, 3),
                "turning_point_monotonic_seconds": round(turning_point_elapsed_seconds, 3),
                "confirmed_at_monotonic_seconds": round(confirmation_elapsed_seconds, 3),
                "confirmed_at_utc": confirmation_at_utc.isoformat(),
            }
            writer = session.canonical_writer
            if writer is None:
                event = self._record_backdated_beans_added(
                    session,
                    turning_point_elapsed_seconds=turning_point_elapsed_seconds,
                    confirmation_elapsed_seconds=confirmation_elapsed_seconds,
                    confirmation_at_utc=confirmation_at_utc,
                    payload=payload,
                )
                return event, _copy_session_for_read(session)
            event = self._new_backdated_beans_added(
                session,
                turning_point_elapsed_seconds=turning_point_elapsed_seconds,
                confirmation_elapsed_seconds=confirmation_elapsed_seconds,
                payload=payload,
            )
            self._canonical_event_admissions.add(admission_key)
            session.manual_charge_persistence_pending = True
            # `submit_group` always queues to the owned executor; unlike the
            # convenience `submit` method it has no writer-thread inline-I/O
            # branch.  Thus the exact auto-T0 frame is enqueued with its
            # store admission, while completion still settles outside this
            # lock.
            try:
                completion = writer.submit_group((_event_log_row(session, event),), lane="event")[0]
            except Exception as error:
                submission_error = error

        assert writer is not None and event is not None
        if defer_canonical_settlement:
            if submission_error is not None:
                self._settle_deferred_auto_t0(
                    session=session,
                    event=event,
                    admission_key=admission_key,
                    completion=None,
                    submission_error=submission_error,
                    on_live_settlement=on_live_settlement,
                )
            elif completion is None:
                self._settle_deferred_auto_t0(
                    session=session,
                    event=event,
                    admission_key=admission_key,
                    completion=None,
                    submission_error=EventLogRecordingError(
                        "Automatic charge record was not admitted."
                    ),
                    on_live_settlement=on_live_settlement,
                )
            else:
                completion.add_done_callback(
                    lambda result: self._settle_deferred_auto_t0(
                        session=session,
                        event=event,
                        admission_key=admission_key,
                        completion=result,
                        submission_error=None,
                        on_live_settlement=on_live_settlement,
                    )
                )
            # An accepted deferred row is not live until its exact completion
            # settles. In particular, do not discard pre-T0 observations or
            # project roast phase from this producer turn.
            return None, _copy_session_for_read(session)

        try:
            if submission_error is not None:
                raise submission_error
            if completion is None:
                raise EventLogRecordingError("Automatic charge record was not admitted.")
            completion.result()
        except _CanonicalAdmissionRefusedError:
            with self._lock:
                self._canonical_event_admissions.discard(admission_key)
                session.manual_charge_persistence_pending = False
                session.manual_charge_persistence_zero = True
            raise
        except Exception:
            with self._lock:
                self._canonical_event_admissions.discard(admission_key)
                session.manual_charge_persistence_pending = False
                session.manual_charge_persistence_uncertain = True
            raise

        with self._lock:
            try:
                if (
                    self._latest_session is session
                    and not session.active
                    and session.faulted_at_utc is not None
                ):
                    _record_historical_manual_charge_locked(session, event)
                    return event, _copy_session_for_read(session)
                self._assert_latest_active_session(session)
                if self._get_existing_singleton_event(session, "beans_added") is not None:
                    raise SessionLifecycleError(
                        "Canonical event admission was displaced while saving."
                    )
                _validate_event_transition(session, "beans_added")
                session.event_timeline.append(event)
                _apply_event_timestamp(session, event)
                session.beans_added_source = "auto_t0"
                session.manual_charge_persistence_pending = False
                session.manual_charge_persistence_zero = False
                return event, _copy_session_for_read(session)
            finally:
                self._canonical_event_admissions.discard(admission_key)

    def _settle_deferred_auto_t0(
        self,
        *,
        session: RoastSession,
        event: RoastEvent,
        admission_key: tuple[str, RoastEventKind],
        completion: Future[None] | None,
        submission_error: Exception | None,
        on_live_settlement: Callable[[], None] | None,
    ) -> None:
        """Settle one exact deferred automatic-charge writer turn.

        This callback never dispatches a driver command. It records only the
        exact turn it was given, retaining a verified row as historical when
        lifecycle ownership changed before completion.
        """
        error = submission_error
        if error is None:
            try:
                if completion is None:
                    raise EventLogRecordingError("Automatic charge record was not admitted.")
                completion.result()
            except Exception as exc:  # noqa: BLE001 - writer owns exact failure evidence.
                error = exc
        if error is not None:
            with self._lock:
                self._canonical_event_admissions.discard(admission_key)
                session.manual_charge_persistence_pending = False
                if isinstance(error, _CanonicalAdmissionRefusedError) and error.known_zero:
                    session.manual_charge_persistence_zero = True
                else:
                    session.manual_charge_persistence_uncertain = True
            return

        live_settled = False
        with self._lock:
            try:
                current = self._latest_session is session and session.active
                if (
                    current
                    and session.faulted_at_utc is None
                    and self._get_existing_singleton_event(session, "beans_added") is None
                ):
                    _validate_event_transition(session, "beans_added")
                    session.event_timeline.append(event)
                    _apply_event_timestamp(session, event)
                    session.beans_added_source = "auto_t0"
                    session.manual_charge_persistence_pending = False
                    session.manual_charge_persistence_zero = False
                    live_settled = True
                else:
                    # A completed exact turn may still be retained for its own
                    # generation after fault, shutdown, or replacement, but it
                    # must never project phase or authority into that state.
                    if event not in session.event_timeline:
                        _record_historical_manual_charge_locked(session, event)
            finally:
                self._canonical_event_admissions.discard(admission_key)
        if live_settled and on_live_settlement is not None:
            try:
                on_live_settlement()
            except Exception:
                # Observer notification cannot recast a verified canonical
                # charge as a sampler failure or alter its settled evidence.
                return

    def _record_backdated_beans_added(
        self,
        session: RoastSession,
        *,
        turning_point_elapsed_seconds: float,
        confirmation_elapsed_seconds: float,
        confirmation_at_utc: datetime,
        payload: dict[str, EventPayloadValue],
    ) -> RoastEvent:
        """Record automatic T0 backdated to the candidate turning point.

        The reported `beans_added` timestamp is the local-max bean-temperature
        sample (#167), never the confirmation tick. The raw confirmation
        timestamp is preserved in ``payload`` so existing consumers keep the
        confirmation moment available.

        Args:
            session: Session to mutate. Caller holds the store lock.
            turning_point_elapsed_seconds: Backdated T0 elapsed seconds — the
                monotonic elapsed time of the most recent local max.
            confirmation_elapsed_seconds: Elapsed seconds at the confirmation
                tick, used to clamp the backdated value into the valid range.
            confirmation_at_utc: Wall-clock confirmation timestamp.
            payload: Structured event details including the raw confirmation
                timestamp.

        Returns:
            The recorded, backdated ``beans_added`` event.
        """
        event = self._new_backdated_beans_added(
            session,
            turning_point_elapsed_seconds=turning_point_elapsed_seconds,
            confirmation_elapsed_seconds=confirmation_elapsed_seconds,
            payload=payload,
        )
        _append_event_log_row(session, event)
        session.event_timeline.append(event)
        _apply_event_timestamp(session, event)
        session.beans_added_source = "auto_t0"
        return event

    def _new_backdated_beans_added(
        self,
        session: RoastSession,
        *,
        turning_point_elapsed_seconds: float,
        confirmation_elapsed_seconds: float,
        payload: dict[str, EventPayloadValue],
    ) -> RoastEvent:
        """Build one automatic T0 row without performing persistence."""
        backdated_elapsed_seconds = round(turning_point_elapsed_seconds, 6)
        # The turning point can never be after the confirmation tick or before
        # the session start; clamp defensively so the timeline stays ordered.
        backdated_elapsed_seconds = max(
            0.0,
            min(backdated_elapsed_seconds, confirmation_elapsed_seconds),
        )
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=backdated_elapsed_seconds,
        )
        recorded_at_utc = session.created_at_utc + timedelta(seconds=monotonic_seconds)
        recorded_at_utc = _normalize_event_recorded_at_utc(
            session,
            recorded_at_utc=recorded_at_utc,
        )
        _validate_event_monotonic_order(
            session,
            kind="beans_added",
            monotonic_seconds=monotonic_seconds,
        )
        return RoastEvent(
            kind="beans_added",
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload=dict(payload),
        )

    def record_first_crack_window_observation(
        self,
        session: RoastSession,
        *,
        window_sequence_number: int,
        confidence: float | None,
        positive_window_count: int,
        confirmed: bool,
        fc_status: Literal["listening", "candidate", "confirmed"],
    ) -> None:
        """Record one per-window first-crack inference observation (#175).

        This is observability-only: it appends an `fc_window` row to the durable
        roast log and updates the session's first-crack inference aggregates. It
        never records a timeline event or changes control behavior. The
        confirmed first-crack timeline event is still recorded separately by
        `record_first_crack_detection_snapshot`.

        Args:
            session: Latest active session that owns the observation.
            window_sequence_number: Source audio window sequence number.
            confidence: Per-window model confidence, or `None` when the backend
                reported no confidence.
            positive_window_count: Rolling positive-window count inside the
                confirmation span after this window was processed.
            confirmed: Whether this window confirmed first crack.
            fc_status: Per-window status (`listening`, `candidate`, `confirmed`).

        Raises:
            SessionLifecycleError: If the session is not the latest active session.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            self._assert_finalisation_allows_mutation(session)
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            recorded_at_utc = self._utc_now()
            monotonic_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
            session.fc_window_count += 1
            if confidence is not None and (
                session.fc_max_confidence is None or confidence > session.fc_max_confidence
            ):
                session.fc_max_confidence = confidence
            if positive_window_count > session.fc_max_positive_window_count:
                session.fc_max_positive_window_count = positive_window_count
            if confirmed:
                session.fc_model_confirmed = True
            _append_jsonl_log_row(
                session,
                {
                    "session_id": session.id,
                    "type": "fc_window",
                    "recorded_at_utc": recorded_at_utc.isoformat(),
                    "monotonic_seconds": monotonic_seconds,
                    "window_sequence_number": window_sequence_number,
                    "confidence": confidence,
                    "positive_window_count": positive_window_count,
                    "confirmed": confirmed,
                    "fc_status": fc_status,
                },
            )

    def record_first_crack_detection_snapshot(
        self,
        session: RoastSession,
        *,
        detected_at_monotonic_seconds: float,
        max_future_seconds: float | None = 0.0,
        confirmed_at_monotonic_seconds: float | None = None,
        payload: dict[str, EventPayloadValue] | None = None,
    ) -> tuple[RoastEvent, RoastSession]:
        """Record automatic first crack at the detector-provided monotonic time.

        Args:
            session: Session to mutate.
            detected_at_monotonic_seconds: Absolute monotonic timestamp reported
                by the detector for the first-crack event. With #168 this is the
                backdated crack onset (the confirming-window onset), not the
                audio-confirmation tick.
            max_future_seconds: Allowed future timestamp tolerance. This lets
                adapter-inferred window-end defaults record when the capture
                window has just been emitted but its inferred end timestamp is
                slightly ahead of the integration clock. Use `None` for
                detector-paced replay where source-audio time can intentionally
                advance faster than wall-clock time.
            confirmed_at_monotonic_seconds: Raw audio-confirmation timestamp when
                the recorded timestamp is backdated (#168). The future-timeline
                bound is validated against this confirmation timestamp — the
                actual detector signal that could be ahead of the integration
                clock — so an implausible-future detector signal is still
                rejected even though the recorded event is backdated to an
                earlier onset. Defaults to ``detected_at_monotonic_seconds``.
            payload: Optional structured event details.

        Returns:
            The event plus an atomic lightweight snapshot. Repeated calls return
            the already-recorded first-crack singleton event.

        Raises:
            SessionLifecycleError: If the session transition is invalid or the
                detector timestamp is outside the active roast interval.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            self._assert_finalisation_allows_mutation(session)
            self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
            if session.faulted_at_utc is not None:
                raise SessionLifecycleError("No non-fault events can be recorded after a fault.")

            existing_event = self._get_existing_singleton_event(
                session,
                "first_crack_detected",
            )
            if existing_event is not None:
                return existing_event, _copy_session_for_read(session)
            _validate_event_transition(session, "first_crack_detected")

            current_elapsed_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
            # Validate the future-timeline bound against the raw confirmation
            # timestamp (the actual detector signal), not the backdated onset:
            # a wildly-future detector signal must still be rejected (#168).
            confirmation_seconds = (
                detected_at_monotonic_seconds
                if confirmed_at_monotonic_seconds is None
                else confirmed_at_monotonic_seconds
            )
            _detected_elapsed_seconds(
                session,
                detected_at_monotonic_seconds=confirmation_seconds,
                current_elapsed_seconds=current_elapsed_seconds,
                max_future_seconds=max_future_seconds,
            )
            detected_elapsed_seconds = _detected_elapsed_seconds(
                session,
                detected_at_monotonic_seconds=detected_at_monotonic_seconds,
                current_elapsed_seconds=current_elapsed_seconds,
                max_future_seconds=max_future_seconds,
            )
            event = RoastEvent(
                kind="first_crack_detected",
                recorded_at_utc=session.created_at_utc
                + timedelta(seconds=detected_elapsed_seconds),
                monotonic_seconds=detected_elapsed_seconds,
                payload={} if payload is None else dict(payload),
            )
            _append_event_log_row(session, event)
            session.event_timeline.append(event)
            _apply_event_timestamp(session, event)
            session.first_crack_source = "model_detection"
            return event, _copy_session_for_read(session)

    def emergency_stop(
        self,
        session: RoastSession,
        *,
        reason: str,
        safety_payload: Mapping[str, EventPayloadValue] | None = None,
        allow_stopped_latest: bool = False,
    ) -> RoastEvent:
        """Apply driver-owned emergency-stop behavior and finalize the session."""
        with self._lock:
            if allow_stopped_latest:
                self._assert_latest_session(session)
            else:
                self._assert_latest_active_session(session)
            normalized_safety_payload = (
                default_emergency_safety_payload()
                if safety_payload is None
                else dict(safety_payload)
            )
            if self._manual_first_crack_writer_enabled and (
                session.manual_first_crack_pending or session.first_crack_source == "manual_mark"
            ):
                # A fault cannot make an accepted or still-writing configured
                # manual-writer mark eligible for standard replay. This is
                # evidence-only state; it deliberately has no driver consequence.
                session.manual_first_crack_export_disqualified = True
            _apply_emergency_safety_payload(session, normalized_safety_payload)
            event_payload = _build_emergency_fault_payload(
                reason=reason,
                safety_payload=normalized_safety_payload,
            )
            event = self._record_emergency_fault_locked(
                session,
                payload=event_payload,
                allow_stopped_latest=allow_stopped_latest,
            )
            if session.active:
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="fault",
                )
            else:
                session.phase = "fault"
            if has_verified_emergency_containment(safety_payload=normalized_safety_payload):
                self._fault_recovery_admission_blocks.discard(session.id)
            else:
                self._fault_recovery_admission_blocks.add(session.id)
            self._abort_finalisation_for_emergency_stop_locked(session)
            return event

    def _abort_finalisation_for_emergency_stop_locked(self, session: RoastSession) -> None:
        """Retain an emergency-aborted finalisation and release its reservation."""
        if session.id not in self._nonterminal_finalisations:
            return
        record = session.finalisation
        if record is not None:
            # Result models are server-owned. Preserve their append-only data
            # and only apply the lifecycle fields common to all finalisation records.
            object.__setattr__(record, "status", "aborted")
            object.__setattr__(record, "abort_reason", "emergency_stop")
            attempted = getattr(getattr(record, "disconnect", None), "attempt_count", 0)
            ordering = (
                "emergency_stop_after_disconnect_attempt"
                if attempted
                else "emergency_stop_before_disconnect_commit"
            )
            object.__setattr__(record, "emergency_stop_ordering", ordering)
            object.__setattr__(record, "retained", True)
            object.__setattr__(record, "session_active_after", session.active)
            object.__setattr__(record, "session_phase_after", session.phase)
        self._clear_finalisation_locked(session)

    def emergency_stop_snapshot(
        self,
        session: RoastSession,
        *,
        reason: str,
        safety_payload: Mapping[str, EventPayloadValue] | None = None,
        allow_stopped_latest: bool = False,
    ) -> tuple[RoastEvent, RoastSession]:
        """Apply emergency stop and return the event plus an atomic lightweight snapshot."""
        with self._lock:
            event = self.emergency_stop(
                session,
                reason=reason,
                safety_payload=safety_payload,
                allow_stopped_latest=allow_stopped_latest,
            )
            return event, _copy_session_for_read(session)

    def _append_mock_drop_turn(
        self,
        ticket: MockDropPersistenceTicket,
        append: Callable[[], None],
        *,
        settle_success: Callable[[], _AppendTurnResult] | None = None,
        settle_failure: Callable[[Exception], _AppendTurnResult] | None = None,
    ) -> _AppendTurnResult | None:
        """Perform one exact mock persistence turn outside lifecycle/store locks."""
        ticket.previous_completion.wait()
        # This caller owns its ticket and writes its own row(s). Its predecessor
        # only grants the turn; neither lifecycle mutation nor another session's
        # work is held while its filesystem writer is stalled.
        try:
            with self._lock:
                if self._normal_drop_persistence_failed:
                    raise SessionLifecycleError("The exact normal bean-drop save is incomplete.")
            append()
        except Exception as error:
            with self._lock:
                self._normal_drop_persistence_failed = True
            if settle_failure is not None:
                return settle_failure(error)
            raise
        else:
            if settle_success is not None:
                return settle_success()
        finally:
            ticket.completion.set()

    def _reserve_mock_drop_ticket_locked(self) -> MockDropPersistenceTicket:
        """Transfer the next private mock-log append turn under store ownership."""
        ticket = MockDropPersistenceTicket(
            sequence=self._normal_drop_persistence_next,
            previous_completion=self._normal_drop_persistence_last_completion,
            completion=Event(),
        )
        self._normal_drop_persistence_next += 1
        self._normal_drop_persistence_last_completion = ticket.completion
        return ticket

    def _discharge_unused_mock_drop_turn_locked(
        self,
        session: RoastSession,
        reservation: NormalDropReservation,
    ) -> bool:
        """Complete only the exact unstarted Drop turn owned by this settlement."""
        ticket = self._normal_drop_persistence_drop_ticket
        if (
            ticket is None
            or self._normal_drop_persistence_drop_started
            or not self._normal_drop_reservation_matches_locked(session, reservation)
        ):
            return False
        ticket.completion.set()
        self._normal_drop_persistence_drop_ticket = None
        return True

    def prepare_mock_drop_emergency_stop(
        self,
        session: RoastSession,
        *,
        reason: str,
        safety_payload: Mapping[str, EventPayloadValue],
        allow_stopped_latest: bool,
    ) -> tuple[RoastEvent, MockDropPersistenceTicket]:
        """Publish contained fault state before its private mock-log append.

        This is deliberately available only while the exact normal-drop fence
        is retained.  It lets an emergency leave the server lifecycle barrier
        before its own append blocks, without treating the event as recorded.
        """
        with self._lock:
            if not self.has_pending_normal_drop_persistence(session):
                raise SessionLifecycleError("No exact normal bean-drop save is pending.")
            if allow_stopped_latest:
                self._assert_latest_session(session)
            else:
                self._assert_latest_active_session(session)
            normalized_safety_payload = dict(safety_payload)
            _apply_emergency_safety_payload(session, normalized_safety_payload)
            event = self._new_emergency_fault_event_locked(
                session,
                payload=_build_emergency_fault_payload(
                    reason=reason, safety_payload=normalized_safety_payload
                ),
                allow_stopped_latest=allow_stopped_latest,
            )
            _apply_event_timestamp(session, event)
            self._normal_drop_unpersisted_fault_count += 1
            session.unpersisted_mock_drop_fault_count += 1
            if session.active:
                session.stop(
                    utc_now=self._utc_now,
                    monotonic_now=self._monotonic_now,
                    phase="fault",
                )
            else:
                session.phase = "fault"
            if has_verified_emergency_containment(safety_payload=normalized_safety_payload):
                self._fault_recovery_admission_blocks.discard(session.id)
            else:
                self._fault_recovery_admission_blocks.add(session.id)
            self._abort_finalisation_for_emergency_stop_locked(session)
            ticket = self._reserve_mock_drop_ticket_locked()
            return event, ticket

    def commit_prepared_mock_drop_emergency_stop(
        self, session: RoastSession, *, event: RoastEvent, ticket: MockDropPersistenceTicket
    ) -> RoastSession:
        """Append one previously published mock-drop emergency fault event."""

        def settle_success() -> RoastSession:
            with self._lock:
                self._assert_latest_session(session)
                session.event_timeline.append(event)
                _apply_event_timestamp(session, event)
                self._normal_drop_unpersisted_fault_count -= 1
                session.unpersisted_mock_drop_fault_count -= 1
                return _copy_session_for_read(session)

        def settle_failure(error: Exception) -> RoastSession:
            if isinstance(error, _MockDropAppendMayBeTornError):
                with self._lock:
                    if self._latest_session is session:
                        self._normal_drop_persistence_torn = True
                        session.mock_log_torn = True
            raise error

        result = self._append_mock_drop_turn(
            ticket,
            lambda: _append_event_log_row_with_failure_evidence(session, event),
            settle_success=settle_success,
            settle_failure=settle_failure,
        )
        assert result is not None
        return result

    def get_active_session(self) -> RoastSession | None:
        """Return the current active session when present."""
        with self._lock:
            if self._latest_session is None or not self._latest_session.active:
                return None
            return self._latest_session

    def get_latest_session(self) -> RoastSession | None:
        """Return the latest session whether active or stopped."""
        with self._lock:
            return self._latest_session

    def get_session_presence(self) -> SessionPresence:
        """Return the closed session-presence snapshot without session details.

        A pending or in-flight start remains visible as ``starting`` so callers
        never mistake an incomplete start for an idle server.
        """
        with self._lock:
            if (
                self._pending_session_start_token is not None
                or self._session_start_in_flight_token is not None
            ):
                return "starting"
            if self._latest_session is None:
                return "none"
            if self._latest_session.active:
                return "active"
            return "stopped"

    def try_get_capture_subject(self) -> CaptureSubject | None:
        """Take one nonblocking capture-subject sample without lifecycle effects.

        The returned value deliberately contains no mutable session object. The
        internal sequence distinguishes replacement sessions even if an injected
        test identity reuses the same session id.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if (
                self._pending_session_start_token is not None
                or self._session_start_in_flight_token is not None
            ):
                return CaptureSubject("starting", None, None)
            session = self._latest_session
            if session is None:
                return CaptureSubject("none", None, None)
            return CaptureSubject(
                "active" if session.active else "stopped",
                session.id,
                session.capture_ownership_sequence,
            )
        finally:
            self._lock.release()

    def get_session_snapshot(
        self,
        *,
        session_id: str | None = None,
        active_only: bool = False,
    ) -> RoastSession:
        """Return a deep-copied session snapshot under store-owned locking."""
        with self._lock:
            if self._latest_session is None:
                raise SessionLifecycleError("No roast session exists.")
            session = self._latest_session
            if session_id is not None:
                session = self._sessions_by_id.get(session_id)
                if session is None:
                    raise SessionLifecycleError(f"Unknown session_id: {session_id}")
            if active_only and not session.active:
                raise SessionLifecycleError("No active roast session exists.")
            return _copy_session_for_read(session)

    def begin_finalisation(
        self, session_id: str
    ) -> tuple[RoastSession | None, str | None, int | None]:
        """Reserve an eligible latest session for finalisation.

        Returns the live session, a rejection code, and a monotonically
        increasing generation. The caller performs driver evidence reads after
        reservation, then either attaches a retained record or abandons it.
        """
        with self._lock:
            session = self._sessions_by_id.get(session_id)
            if session is None:
                return None, "unknown_session", None
            if session.finalisation is not None:
                if getattr(session.finalisation, "status", None) in (
                    "clean",
                    "completed_not_clean",
                    "aborted",
                ):
                    return (
                        session,
                        None,
                        getattr(session.finalisation, "reservation_generation", None),
                    )
                if session_id in self._finalisation_in_progress:
                    return session, "finalisation_in_progress", None
                rejection = self._finalisation_admission_rejection_locked(
                    session, require_reservation=True
                )
                if rejection is not None:
                    self._abort_finalisation_locked(session)
                    return session, rejection, None
                self._finalisation_in_progress.add(session_id)
                return session, None, getattr(session.finalisation, "reservation_generation", None)
            rejection = self._finalisation_admission_rejection_locked(session)
            if rejection is not None:
                return session, rejection, None
            if session_id in self._finalisation_in_progress:
                return session, "finalisation_in_progress", None
            if session.pending_driver_command_token is not None:
                return session, "command_in_progress", None
            self._finalisation_generation += 1
            self._finalisation_in_progress.add(session_id)
            self._nonterminal_finalisations.add(session_id)
            self._reserve_driver_command_locked(session, kind="finalisation")
            token = session.pending_driver_command_token
            assert token is not None
            self._finalisation_tokens[session_id] = token
            return session, None, self._finalisation_generation

    def attach_finalisation(self, session: RoastSession, record: object) -> object:
        """Attach a retained finalisation record after successful admission."""
        with self._lock:
            if self._finalisation_admission_rejection_locked(session, require_reservation=True):
                object.__setattr__(record, "status", "aborted")
                object.__setattr__(
                    record,
                    "abort_reason",
                    "emergency_stop"
                    if session.faulted_at_utc is not None
                    else "session_or_reservation_changed",
                )
                object.__setattr__(record, "retained", True)
                object.__setattr__(
                    record,
                    "emergency_stop_ordering",
                    "emergency_stop_before_disconnect_commit",
                )
                object.__setattr__(record, "session_active_after", session.active)
                object.__setattr__(record, "session_phase_after", session.phase)
                session.finalisation = record
                self._clear_finalisation_locked(session)
                return record
            session.finalisation = record
            return record

    def abandon_finalisation_admission(self, session: RoastSession) -> None:
        """Release a failed first-admission reservation."""
        with self._lock:
            self._clear_finalisation_locked(session)

    def finish_finalisation_invocation(self, session: RoastSession) -> None:
        """Clear the in-progress marker after one finalisation call returns."""
        with self._lock:
            self._finalisation_in_progress.discard(session.id)

    def finish_finalisation_terminal(self, session: RoastSession, record: object) -> object:
        """Release reservation and stop a successfully disconnected session."""
        with self._lock:
            if getattr(session.finalisation, "status", None) == "aborted":
                return deepcopy(session.finalisation)
            self._assert_latest_active_session(session)
            session.finalisation = record
            self._clear_finalisation_locked(session)
            session.stop(
                utc_now=self._utc_now,
                monotonic_now=self._monotonic_now,
                phase=session.phase,
            )
            return deepcopy(record)

    def persist_finalisation(self, session: RoastSession, record: object) -> object:
        """Persist nonterminal finalisation progress without overwriting an abort."""
        with self._lock:
            if getattr(session.finalisation, "status", None) == "aborted":
                return deepcopy(session.finalisation)
            session.finalisation = record
            return deepcopy(record)

    def finalisation_blocks_session(self, session_id: str) -> bool:
        """Return whether finalisation fences background mutation for this session."""
        with self._lock:
            session = self._sessions_by_id.get(session_id)
            return session is not None and self._finalisation_blocks_mutation(session)

    def abort_finalisation_if_invalid(
        self, session: RoastSession, generation: int | None
    ) -> object | None:
        """Atomically abort a retained finalisation when its reservation changed."""
        with self._lock:
            record = session.finalisation
            if (
                record is not None
                and self._finalisation_admission_rejection_locked(session, require_reservation=True)
                is None
                and getattr(record, "reservation_generation", None) == generation
                and self._finalisation_tokens.get(session.id)
                == session.pending_driver_command_token
            ):
                return None
            if record is None:
                raise SessionLifecycleError("Finalisation reservation is invalid without a record.")
            aborted = self._abort_finalisation_locked(session)
            return aborted

    def copy_session(self, session: RoastSession) -> RoastSession:
        """Return a deep-copied snapshot of one known session object under the store lock."""
        with self._lock:
            return _copy_session_for_read(session)

    def copy_finalisation(self, session: RoastSession) -> object | None:
        """Return a detached finalisation record under store locking."""
        with self._lock:
            return deepcopy(session.finalisation)

    @property
    def telemetry_buffer_limit(self) -> int:
        """Return the per-session telemetry retention limit."""
        return self._telemetry_buffer_limit

    def _assert_latest_active_session(self, session: RoastSession) -> None:
        """Validate that one session is the current mutable active session."""
        self._assert_latest_session(session)
        if not session.active:
            raise SessionLifecycleError("Stopped sessions cannot be mutated.")

    def _assert_staged_start_claimable_locked(self, session: RoastSession) -> None:
        """Validate staged first-write eligibility while the store lock is held."""
        self._assert_latest_active_session(session)
        if session.faulted_at_utc is not None or session.phase == "fault":
            raise SessionLifecycleError("Staged first write is blocked by an emergency fault.")
        if session.pending_driver_command_token is not None:
            raise SessionLifecycleError(
                "Staged first write is blocked by a pending driver command."
            )

    def _fixed_zero_identity_rejection_locked(
        self, expected_session_id: str
    ) -> FixedZeroIdentityRejection | None:
        """Return the closed containment identity rejection under the store lock."""
        latest = self._latest_session
        if latest is None or expected_session_id not in self._sessions_by_id:
            return "unknown_session"
        if latest.id != expected_session_id:
            return "not_latest_session"
        if latest.faulted_at_utc is not None or latest.phase == "fault":
            return "session_faulted"
        if not latest.active:
            return "session_not_active"
        return None

    def _timing_evidence_identity_rejection_locked(
        self, expected_session_id: str
    ) -> TimingEvidenceIdentityRejection | None:
        """Return the closed timing-evidence identity rejection under the lock."""
        rejection = self._fixed_zero_identity_rejection_locked(expected_session_id)
        if rejection is not None:
            return rejection
        latest = self._latest_session
        assert latest is not None
        if latest.purpose != "roast":
            return "session_purpose_not_eligible"
        return None

    def _finalisation_blocks_mutation(self, session: RoastSession) -> bool:
        """Return whether a finalisation reservation excludes ordinary mutation."""
        return (
            session.id in self._nonterminal_finalisations
            or session.pending_driver_command_kind == "finalisation"
        )

    def _assert_finalisation_allows_mutation(self, session: RoastSession) -> None:
        """Reject an ordinary mutation while a finalisation reservation is active."""
        if self._finalisation_blocks_mutation(session):
            raise SessionLifecycleError("Session finalisation is in progress.")

    def _finalisation_admission_rejection_locked(
        self, session: RoastSession, *, require_reservation: bool = False
    ) -> str | None:
        """Return the current finalisation-identity rejection under store locking."""
        if self._latest_session is not session:
            return "not_latest_session"
        if not session.active:
            return "session_not_active"
        if session.purpose != "cold_characterisation":
            return "session_purpose_not_eligible"
        if session.faulted_at_utc is not None:
            return "session_faulted"
        if require_reservation and (
            session.pending_driver_command_kind != "finalisation"
            or session.pending_driver_command_token is None
        ):
            return "command_in_progress"
        return None

    def _abort_finalisation_locked(self, session: RoastSession) -> object | None:
        """Mark a retained finalisation aborted without changing driver lifecycle."""
        record = session.finalisation
        if record is not None:
            object.__setattr__(record, "status", "aborted")
            if getattr(record, "abort_reason", None) != "emergency_stop":
                object.__setattr__(record, "abort_reason", "session_or_reservation_changed")
            object.__setattr__(record, "retained", True)
            object.__setattr__(record, "session_active_after", session.active)
            object.__setattr__(record, "session_phase_after", session.phase)
        self._clear_finalisation_locked(session)
        return deepcopy(record)

    def _clear_finalisation_locked(self, session: RoastSession) -> None:
        """Release private finalisation fencing and reservation bookkeeping."""
        token = self._finalisation_tokens.pop(session.id, None)
        if token is not None and session.pending_driver_command_token == token:
            session.pending_driver_command_token = None
            session.pending_driver_command_kind = None
        self._nonterminal_finalisations.discard(session.id)
        self._finalisation_in_progress.discard(session.id)

    def _assert_latest_session(self, session: RoastSession) -> None:
        """Validate that one session is the latest known session."""
        if self._latest_session is not session:
            raise SessionLifecycleError("Only the latest session can be mutated.")

    def _assert_stopped_fault_recovery_session_locked(self, session: RoastSession) -> None:
        """Validate the closed stopped-fault recovery admission state."""
        self._assert_latest_session(session)
        if session.active:
            raise SessionLifecycleError("Recovery command requires a stopped session.")
        if session.faulted_at_utc is None or session.phase != "fault":
            raise SessionLifecycleError("Recovery command is only allowed after an emergency stop.")
        if session.heat_level_percent != 0:
            raise SessionLifecycleError("Recovery command requires heat to be off.")

    def _assert_guarded_fault_recovery_admission_locked(self, session: RoastSession) -> None:
        """Reject guarded recovery while start or earlier recovery I/O is pending."""
        self._assert_stopped_fault_recovery_session_locked(session)
        if session.id in self._fault_recovery_admission_blocks:
            raise SessionLifecycleError(
                "Recovery command requires a verified emergency-stop containment result."
            )
        if (
            self._pending_session_start_token is not None
            or self._session_start_in_flight_token is not None
        ):
            raise SessionLifecycleError(
                "Recovery command cannot run while a roast session start is in progress."
            )
        if session.id in self._fault_recovery_in_flight_tokens:
            raise SessionLifecycleError(
                "Recovery command cannot run while an earlier fault recovery command is in flight."
            )

    def _record_stopped_session_event_locked(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue],
    ) -> RoastEvent:
        """Record a narrowly allowed event for a stopped latest session."""
        self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
        existing_event = self._get_existing_singleton_event(session, kind)
        is_post_fault_recovery = (
            kind in {"cooling_started", "cooling_stopped"}
            and payload.get("recovery_after_fault") is True
        )
        if existing_event is not None and not is_post_fault_recovery:
            return existing_event
        recorded_at_utc = self._utc_now()
        monotonic_seconds = (
            max(0.0, self._monotonic_now() - session.monotonic_start)
            if payload.get("recovery_after_fault") is True
            else session.elapsed_monotonic_seconds(self._monotonic_now)
        )
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=monotonic_seconds,
        )
        recorded_at_utc = _normalize_event_recorded_at_utc(
            session,
            recorded_at_utc=recorded_at_utc,
        )
        _validate_event_monotonic_order(
            session,
            kind=kind,
            monotonic_seconds=monotonic_seconds,
        )
        event = RoastEvent(
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload=dict(payload),
        )
        _append_event_log_row(session, event)
        session.event_timeline.append(event)
        if payload.get("recovery_after_fault") is not True:
            _apply_event_timestamp(session, event)
        return event

    def _start_session_locked(self, *, purpose: SessionPurpose = "roast") -> RoastSession:
        """Start one new active session while the store lock is already held."""
        self._assert_no_pending_fault_cooling_recovery_locked()
        if self._latest_session is not None and self._latest_session.active:
            raise SessionLifecycleError("An active roast session already exists.")

        session_id = self._session_id_factory()
        self._capture_ownership_sequence += 1
        session = RoastSession(
            id=session_id,
            created_at_utc=self._utc_now(),
            monotonic_start=self._monotonic_now(),
            capture_ownership_sequence=self._capture_ownership_sequence,
            purpose=purpose,
            log_writer=LogWriterReference(
                session_id=session_id,
                log_dir=self._default_log_dir / session_id,
            ),
        )
        if self._manual_first_crack_writer_enabled:
            session.canonical_writer = _CanonicalGenerationWriter(session=session)
        self._latest_session = session
        self._sessions_by_id[session_id] = session
        self._session_id_order.append(session_id)
        self._prune_session_history_locked()
        return session

    def _assert_no_pending_fault_cooling_recovery_locked(self) -> None:
        """Reject new sessions while stopped fault cooling still needs recovery."""
        if self._normal_drop_in_flight is not None:
            raise SessionLifecycleError(
                "Cannot start a roast session after an ambiguous bean drop."
            )
        session = self._latest_session
        if session is not None and session.id in self._fault_recovery_admission_blocks:
            raise SessionLifecycleError(
                "Cannot start a roast session until emergency-stop containment is verified."
            )
        if session is not None and session.id in self._fault_drop_cooling_cycle_required:
            raise SessionLifecycleError(
                "Cannot start a roast session until post-drop cooling is started and stopped."
            )
        if session is not None and session.id in self._historical_fault_drop_start_blocks:
            raise SessionLifecycleError(
                "Cannot start a roast session after a superseded recovery bean drop."
            )
        if session is not None and session.id in self._fault_drop_ambiguity_required:
            raise SessionLifecycleError(
                "Cannot start a roast session after an ambiguous bean drop."
            )
        if session is not None and _has_unrecorded_charge_attempt(session):
            raise SessionLifecycleError(
                "Cannot start a roast session while a charge attempt has no canonical record."
            )
        if (
            session is not None
            and not session.active
            and session.faulted_at_utc is not None
            and session.beans_added_at_utc is not None
            and not _has_recorded_beans_dropped(session)
            and not session.unrecorded_fault_recovery_drop_accepted
            and not self._has_settled_known_zero_ordinary_discharge_locked(session)
        ):
            raise SessionLifecycleError(
                "Cannot start a roast session until post-fault bean discharge is verified."
            )
        if (
            session is not None
            and not session.active
            and session.faulted_at_utc is not None
            and session.phase == "fault"
            and session.cooling_on
        ):
            raise SessionLifecycleError(
                "Cannot start a new roast session while post-fault cooling recovery is pending."
            )
        if (
            session is not None
            and not session.active
            and session.faulted_at_utc is not None
            and session.phase == "fault"
            and session.pending_driver_command_token is not None
        ):
            raise SessionLifecycleError("Cannot start a roast session during fault recovery.")
        if session is not None and session.id in self._fault_recovery_in_flight_tokens:
            raise SessionLifecycleError("Cannot start a roast session during fault recovery I/O.")

    def _has_verified_known_zero_ordinary_discharge_locked(self, session: RoastSession) -> bool:
        """Return the exact qualified ordinary-Drop discharge evidence.

        Only a pre-dispatch coupled-group refusal can create the zero fact.
        The current stopped-fault controls must still be the verified emergency
        containment projection; a raw returned flag or incomplete writer alone
        is never sufficient.
        """
        return (
            session.verified_ordinary_driver_drop
            and session.ordinary_drop_persistence_zero
            and not session.unrecorded_driver_drop
            and session.mock_log_torn
            and session.heat_level_percent == 0
            and session.fan_level_percent == 100
            and session.cooling_on
        )

    def _has_settled_known_zero_ordinary_discharge_locked(self, session: RoastSession) -> bool:
        """Return qualified discharge evidence after its exact cooling settlement."""
        return (
            session.verified_ordinary_driver_drop
            and session.ordinary_drop_persistence_zero
            and session.ordinary_drop_persistence_zero_cooling_settled
            and not session.unrecorded_driver_drop
            and session.mock_log_torn
            and session.heat_level_percent == 0
            and not session.cooling_on
            and session.pending_driver_command_token is None
            and session.id not in self._fault_recovery_in_flight_tokens
        )

    def _assert_session_start_reservation(
        self,
        reservation: SessionStartReservation,
    ) -> None:
        """Validate a session-start reservation before creating the session."""
        if self._pending_session_start_token != reservation.token:
            raise SessionLifecycleError("Session start reservation is no longer active.")

    def _get_existing_singleton_event(
        self,
        session: RoastSession,
        kind: RoastEventKind,
    ) -> RoastEvent | None:
        """Return an existing singleton event when this kind is idempotent."""
        if kind not in _SINGLETON_EVENT_KINDS:
            return None
        for event in session.event_timeline:
            if event.kind == kind:
                return event
        return None

    def _reserve_driver_command_locked(
        self,
        session: RoastSession,
        *,
        kind: DriverCommandKind,
    ) -> DriverCommandReservation:
        """Reserve a driver command while the store lock is already held."""
        allow_torn_stopped_fault_cooling_stop = (
            kind == "stop_cooling"
            and not session.active
            and session.faulted_at_utc is not None
            and session.phase == "fault"
        )
        if (
            self._normal_drop_persistence_torn
            and kind
            not in {
                "heat_containment",
                "start_cooling",
            }
            and not allow_torn_stopped_fault_cooling_stop
        ):
            raise SessionLifecycleError("The exact normal bean-drop log has an uncertain tail.")
        if kind == "drop" and self._normal_drop_in_flight is not None:
            raise SessionLifecycleError("A normal bean-drop operation is unresolved.")
        if kind == "control" and session.pending_driver_command_token is not None:
            raise SessionLifecycleError("Another driver command is already in progress.")
        if kind == "control" and self._normal_drop_in_flight is not None:
            raise SessionLifecycleError("A normal bean-drop operation is unresolved.")
        if kind == "control" and self._ordinary_drop_is_unresolved_locked(session):
            # An owned, returned-unsettled, or unknown ordinary Drop may
            # already have moved beans. Controls must not enter the driver on
            # that unresolved batch; cooling and emergency routes retain their
            # separately established admission rules.
            raise SessionLifecycleError("A prior bean-drop outcome is unresolved.")
        if session.pending_driver_command_token is not None:
            raise SessionLifecycleError("Another driver command is already in progress.")
        reservation = DriverCommandReservation(
            session_id=session.id,
            token=_generate_session_id(),
            kind=kind,
        )
        session.pending_driver_command_token = reservation.token
        session.pending_driver_command_kind = reservation.kind
        return reservation

    def _ordinary_drop_is_unresolved_locked(self, session: RoastSession) -> bool:
        """Return whether an ordinary Drop may have moved beans without settlement."""
        operation = self._normal_drop_in_flight
        return (
            session.ordinary_driver_drop_dispatch_owned
            or session.verified_ordinary_driver_drop
            or session.unrecorded_driver_drop
            or (
                operation is not None
                and self._normal_drop_reservation_matches_locked(session, operation)
            )
        )

    def _normal_drop_exact_identity_rejection_locked(
        self, identity: NormalDropIdentity
    ) -> NormalDropRejection | None:
        """Validate store ownership before phase, evidence, or fence inspection."""
        if (
            type(identity.session_id) is not str
            or type(identity.clock_epoch_id) is not str
            or type(identity.ownership_sequence) is not int
            or type(identity.timing_evidence_sequence) is not int
        ):
            return "invalid_identity"
        latest = self._latest_session
        if latest is None or identity.session_id not in self._sessions_by_id:
            return "unknown_session"
        if latest.id != identity.session_id:
            return "not_latest_session"
        if identity.clock_epoch_id != self._clock_epoch_id:
            return "clock_epoch_mismatch"
        if identity.ownership_sequence != latest.capture_ownership_sequence:
            return "ownership_sequence_mismatch"
        return None

    def _normal_drop_lifecycle_rejection_locked(
        self, session: RoastSession
    ) -> NormalDropRejection | None:
        """Return phase-independent exact-owner admission refusal, when any."""
        if session.purpose != "roast":
            return "session_purpose_not_eligible"
        if session.faulted_at_utc is not None or session.phase == "fault":
            return "session_faulted"
        if not session.active:
            return "session_not_active"
        return None

    def _normal_drop_evidence_rejection_locked(
        self,
        session: RoastSession,
        identity: NormalDropIdentity,
    ) -> NormalDropRejection | None:
        """Return the closed timing-evidence freshness result for normal drop."""
        if identity.timing_evidence_sequence <= 0 or session.timing_evidence_sequence <= 0:
            return "timing_evidence_missing"
        if identity.timing_evidence_sequence != session.timing_evidence_sequence:
            return "timing_evidence_superseded"
        return None

    def _normal_drop_reservation_matches_locked(
        self,
        session: RoastSession,
        reservation: NormalDropReservation,
    ) -> bool:
        """Return whether one settlement owns the unresolved normal-drop fence."""
        operation = self._normal_drop_in_flight
        return (
            operation == reservation
            and self._latest_session is session
            and reservation.identity.session_id == session.id
            and reservation.identity.clock_epoch_id == self._clock_epoch_id
            and reservation.identity.ownership_sequence == session.capture_ownership_sequence
        )

    def _normal_drop_admission_outcome_for_in_flight_locked(
        self, session: RoastSession
    ) -> Literal["in_progress", "ambiguous"]:
        """Distinguish an owned pending dispatch from retained uncertainty."""
        operation = self._normal_drop_in_flight
        assert operation is not None
        if self._normal_drop_fence_state == "pending" and (
            session.pending_driver_command_token == operation.token
            and session.pending_driver_command_kind == "drop"
        ):
            return "in_progress"
        return "ambiguous"

    def _retain_normal_drop_unknown_locked(
        self,
        session: RoastSession,
        reservation: NormalDropReservation,
    ) -> None:
        """Make one owned normal-drop fence terminally UNKNOWN.

        This is deliberately one-way.  Only an exact, still-pending operation
        can settle as proved cancellation; any driver or persistence ambiguity
        retains the reservation after its ordinary token is cleared.
        """
        self._clear_normal_drop_token_locked(session, reservation)
        self._normal_drop_fence_state = "unknown"

    def _clear_normal_drop_token_locked(
        self,
        session: RoastSession,
        reservation: NormalDropReservation,
    ) -> None:
        """Clear only this operation's ordinary command token, when still present."""
        if (
            session.pending_driver_command_token == reservation.token
            and session.pending_driver_command_kind == "drop"
        ):
            session.pending_driver_command_token = None
            session.pending_driver_command_kind = None

    def _assert_driver_command_reservation(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> None:
        """Validate a reservation before applying one driver command result."""
        self._assert_latest_active_session(session)
        if reservation.session_id != session.id:
            raise SessionLifecycleError("Driver command reservation belongs to another session.")
        if (
            session.pending_driver_command_token != reservation.token
            or session.pending_driver_command_kind != reservation.kind
        ):
            raise SessionLifecycleError("Driver command reservation is no longer active.")

    def _clear_driver_command_reservation_locked(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> None:
        """Clear a driver command reservation while the store lock is held."""
        if session.pending_driver_command_token != reservation.token:
            return
        session.pending_driver_command_token = None
        session.pending_driver_command_kind = None

    def _clear_fault_recovery_in_flight_locked(
        self,
        session: RoastSession,
        reservation: DriverCommandReservation,
    ) -> None:
        """Retire one exact stopped-fault I/O fence while the store lock is held."""
        if self._fault_recovery_in_flight_tokens.get(session.id) == reservation.token:
            del self._fault_recovery_in_flight_tokens[session.id]

    def _prune_session_history_locked(self) -> None:
        """Evict oldest completed sessions once retained history exceeds the limit."""
        while len(self._session_id_order) > self._session_history_limit:
            oldest_session_id = self._session_id_order[0]
            oldest_session = self._sessions_by_id.get(oldest_session_id)
            if oldest_session is not None and oldest_session.active:
                break
            self._session_id_order.popleft()
            if oldest_session is not None:
                del self._sessions_by_id[oldest_session_id]

    def _record_emergency_fault_locked(
        self,
        session: RoastSession,
        *,
        payload: dict[str, EventPayloadValue],
        allow_stopped_latest: bool,
    ) -> RoastEvent:
        """Record an emergency fault, including a stopped latest-session race."""
        if self._normal_drop_persistence_torn:
            raise SessionLifecycleError(
                "Fault history must be recorded by the owned normal-drop persistence turn."
            )
        self._assert_normal_drop_persistence_allows_competing_writer_locked(session)
        event = self._new_emergency_fault_event_locked(
            session, payload=payload, allow_stopped_latest=allow_stopped_latest
        )
        if session.canonical_writer is not None:
            # Canonical storage never runs while emergency admission owns the
            # store lock. This covers both an ordinary fault-only turn and a
            # fault queued behind an accepted human FC.
            completion = session.canonical_writer.submit(
                _event_log_row(session, event), lane="event"
            )
            self._emergency_fault_completions[id(event)] = (event, completion)

            def settle_fault_write(result: Future[None]) -> None:
                """Publish the deferred fault history only after its exact turn verifies."""
                try:
                    result.result()
                except Exception:
                    # The writer latches incomplete evidence itself.  Keep the
                    # fault's already-recorded containment state, but never
                    # manufacture a durable timeline row for a failed append.
                    return
                with self._lock:
                    if event not in session.event_timeline:
                        session.event_timeline.append(event)

            completion.add_done_callback(settle_fault_write)

            def retire_unclaimed_fault_write(result: Future[None]) -> None:
                """Retire a settled turn that no handler took ownership of."""
                del result
                with self._lock:
                    current = self._emergency_fault_completions.get(id(event))
                    if current is not None and current[0] is event and current[1] is completion:
                        del self._emergency_fault_completions[id(event)]

            completion.add_done_callback(retire_unclaimed_fault_write)
        else:
            _append_event_log_row(session, event)
            session.event_timeline.append(event)
        _apply_event_timestamp(session, event)
        return event

    def emergency_fault_completion(self, event: RoastEvent) -> Future[None] | None:
        """Return the deferred append owned by this exact emergency fault."""
        with self._lock:
            current = self._emergency_fault_completions.get(id(event))
            if current is not None and current[0] is event:
                del self._emergency_fault_completions[id(event)]
                return current[1]
            if any(
                recorded is event
                for session in self._sessions_by_id.values()
                for recorded in session.event_timeline
            ):
                return None
        failed: Future[None] = Future()
        failed.set_exception(EventLogRecordingError("Emergency fault save failed or is unknown."))
        return failed

    def _new_emergency_fault_event_locked(
        self,
        session: RoastSession,
        *,
        payload: dict[str, EventPayloadValue],
        allow_stopped_latest: bool,
    ) -> RoastEvent:
        """Build one emergency fault event without appending it."""
        if session.active:
            _validate_event_transition(session, "fault")
        elif not allow_stopped_latest:
            raise SessionLifecycleError("Stopped sessions cannot be mutated.")
        elif session.faulted_at_utc is None and self._normal_drop_unpersisted_fault_count == 0:
            _validate_event_transition(session, "fault")
        recorded_at_utc = self._utc_now()
        monotonic_seconds = max(0.0, self._monotonic_now() - session.monotonic_start)
        monotonic_seconds = _normalize_event_monotonic_seconds(
            session,
            monotonic_seconds=monotonic_seconds,
        )
        recorded_at_utc = _normalize_event_recorded_at_utc(
            session,
            recorded_at_utc=recorded_at_utc,
        )
        _validate_event_monotonic_order(
            session,
            kind="fault",
            monotonic_seconds=monotonic_seconds,
        )
        event = RoastEvent(
            kind="fault",
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            payload=dict(payload),
        )
        return event


def _apply_event_timestamp(session: RoastSession, event: RoastEvent) -> None:
    """Update authoritative event timestamp fields from one timeline event."""
    if event.kind == "beans_added":
        session.phase = "roasting"
        session.beans_added_at_utc = event.recorded_at_utc
        session.beans_added_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "first_crack_detected":
        session.phase = "development"
        session.first_crack_at_utc = event.recorded_at_utc
        session.first_crack_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "beans_dropped":
        session.phase = "dropped"
        session.heat_level_percent = 0
        session.beans_dropped_at_utc = event.recorded_at_utc
        session.beans_dropped_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "cooling_started":
        session.phase = "cooling"
        session.cooling_on = True
        session.cooling_started_at_utc = event.recorded_at_utc
        session.cooling_started_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "cooling_stopped":
        session.cooling_on = False
        session.phase = "complete" if session.beans_dropped_at_utc is not None else "pre_roast"
        session.cooling_stopped_at_utc = event.recorded_at_utc
        session.cooling_stopped_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "fault" and session.faulted_at_utc is None:
        session.phase = "fault"
        session.faulted_at_utc = event.recorded_at_utc
        session.faulted_monotonic_seconds = event.monotonic_seconds


def _record_historical_manual_charge_locked(
    session: RoastSession,
    event: RoastEvent,
    *,
    source: Literal["manual_mark", "auto_t0"] | None = None,
) -> None:
    """Retain one verified pre-fault charge without reopening the roast.

    The caller holds the store lock and has proved that this exact accepted
    canonical row completed after an emergency displaced its live settlement.
    """
    fault_index = next(
        (
            index
            for index, existing in enumerate(session.event_timeline)
            if existing.kind == "fault"
        ),
        len(session.event_timeline),
    )
    session.event_timeline.insert(fault_index, event)
    session.beans_added_at_utc = event.recorded_at_utc
    session.beans_added_monotonic_seconds = event.monotonic_seconds
    event_source = source if source is not None else event.payload.get("source")
    if event_source == "manual_mark":
        session.beans_added_source = "manual_mark"
    elif event_source == "auto_t0":
        session.beans_added_source = "auto_t0"
    else:
        session.beans_added_source = None
    session.manual_charge_persistence_pending = False


def _has_unresolved_manual_charge(session: RoastSession) -> bool:
    """Return whether a manual charge may exist without verified discharge."""
    return session.manual_charge_persistence_pending or session.manual_charge_persistence_uncertain


def _has_unrecorded_charge_attempt(session: RoastSession) -> bool:
    """Return whether an original charge attempt lacks a canonical event.

    A known-zero result establishes only that its canonical row was never
    written; it does not establish that the operator did not charge the drum.
    This is exact-session recovery evidence and intentionally does not consult
    the generation-wide torn flag.
    """
    return _has_unresolved_manual_charge(session) or session.manual_charge_persistence_zero


def _detected_elapsed_seconds(
    session: RoastSession,
    *,
    detected_at_monotonic_seconds: float,
    current_elapsed_seconds: float,
    max_future_seconds: float | None,
) -> float:
    if max_future_seconds is not None and max_future_seconds < 0:
        raise SessionLifecycleError("Detected first-crack future tolerance must be >= 0.")
    detected_elapsed_seconds = round(
        float(detected_at_monotonic_seconds) - session.monotonic_start,
        6,
    )
    if not math.isfinite(detected_elapsed_seconds):
        raise SessionLifecycleError("Detected first-crack timestamp must be finite.")
    if detected_elapsed_seconds < 0:
        raise SessionLifecycleError(
            "Detected first-crack timestamp cannot be before session start."
        )
    if detected_elapsed_seconds > current_elapsed_seconds:
        future_delta_seconds = detected_elapsed_seconds - current_elapsed_seconds
        if max_future_seconds is not None and future_delta_seconds > max_future_seconds:
            raise SessionLifecycleError("Detected first-crack timestamp cannot be in the future.")
        if max_future_seconds is not None:
            detected_elapsed_seconds = current_elapsed_seconds
    if (
        session.beans_added_monotonic_seconds is not None
        and detected_elapsed_seconds < session.beans_added_monotonic_seconds
    ):
        raise SessionLifecycleError("Detected first crack cannot be before beans are added.")
    return detected_elapsed_seconds


def _validate_event_transition(session: RoastSession, kind: RoastEventKind) -> None:
    """Validate that one new event is allowed from the current session phase."""
    allowed_phases = _ALLOWED_PHASES_BY_EVENT.get(kind)
    if allowed_phases is None:
        raise SessionLifecycleError(f"Unknown roast event kind: {kind}.")
    if session.phase not in allowed_phases:
        allowed_phase_list = ", ".join(
            phase for phase in _PHASE_PROGRESSION_ORDER if phase in allowed_phases
        )
        raise SessionLifecycleError(
            f"{kind} cannot be recorded while phase is {session.phase}; "
            f"allowed phases: {allowed_phase_list}."
        )


def _is_valid_mock_drop_projection(
    *,
    heat_level_percent: object,
    fan_level_percent: object,
    cooling_on: object,
) -> bool:
    """Return whether a mock Drop report is the complete expected endpoint.

    P2 stores only the mock driver's internally consistent command projection:
    zero heat, full fan, and cooling enabled.  This validates a reported
    software state; it does not make a physical-actuation or durability claim.
    """
    try:
        heat = validate_control_percent(heat_level_percent, label="heat_level_percent")
        fan = validate_control_percent(fan_level_percent, label="fan_level_percent")
    except (TypeError, ValueError):
        return False
    return heat == 0 and fan == 100 and cooling_on is True


def _validate_event_monotonic_order(
    session: RoastSession,
    *,
    kind: RoastEventKind,
    monotonic_seconds: float,
) -> None:
    if kind == "fault" or not session.event_timeline:
        return
    latest_event = session.event_timeline[-1]
    if monotonic_seconds < latest_event.monotonic_seconds:
        raise SessionLifecycleError("Roast events must be recorded in monotonic timestamp order.")


def _normalize_event_monotonic_seconds(
    session: RoastSession,
    *,
    monotonic_seconds: float,
) -> float:
    if not session.event_timeline:
        return monotonic_seconds
    latest_event = session.event_timeline[-1]
    if _has_recorded_first_crack(session) and monotonic_seconds < latest_event.monotonic_seconds:
        return latest_event.monotonic_seconds
    return monotonic_seconds


def _normalize_event_recorded_at_utc(
    session: RoastSession,
    *,
    recorded_at_utc: datetime,
) -> datetime:
    if not session.event_timeline:
        return recorded_at_utc
    latest_event = session.event_timeline[-1]
    if _has_recorded_first_crack(session) and recorded_at_utc < latest_event.recorded_at_utc:
        return latest_event.recorded_at_utc
    return recorded_at_utc


def _has_recorded_first_crack(session: RoastSession) -> bool:
    return session.first_crack_monotonic_seconds is not None or any(
        event.kind == "first_crack_detected" for event in session.event_timeline
    )


def _has_recorded_beans_dropped(session: RoastSession) -> bool:
    """Return whether the timeline contains authoritative bean-drop evidence."""
    return any(event.kind == "beans_dropped" for event in session.event_timeline)


def _elapsed_since(
    session: RoastSession,
    *,
    start_seconds: float | None,
    monotonic_now: Callable[[], float],
) -> float | None:
    """Return elapsed seconds from one event to drop, stop, or now."""
    if start_seconds is None:
        return None
    if session.beans_dropped_monotonic_seconds is not None:
        end_seconds = session.beans_dropped_monotonic_seconds
    elif session.monotonic_stop is not None:
        end_seconds = max(0.0, session.monotonic_stop - session.monotonic_start)
    else:
        end_seconds = session.elapsed_monotonic_seconds(monotonic_now)
    return round(max(0.0, end_seconds - start_seconds), 3)


def _generate_session_id() -> str:
    """Return a stable opaque session identifier."""
    return uuid4().hex


def default_emergency_safety_payload(
    *,
    driver: str = "store-default",
    driver_error: str | None = None,
) -> dict[str, EventPayloadValue]:
    """Return fail-closed safety state when driver safety behavior is unavailable."""
    payload: dict[str, EventPayloadValue] = {
        "driver": driver,
        "driver_safety_method": "emergency_stop",
        "driver_safety_method_called": False,
        "heat_level_percent": 0,
        "fan_level_percent": 100,
        "cooling_on": True,
    }
    if driver_error is not None:
        payload["driver_error"] = driver_error
    return payload


def has_verified_emergency_containment(*, safety_payload: Mapping[str, EventPayloadValue]) -> bool:
    """Return whether a driver confirmed the complete emergency-safe state."""
    heat_level_percent = safety_payload.get("heat_level_percent")
    fan_level_percent = safety_payload.get("fan_level_percent")
    cooling_on = safety_payload.get("cooling_on")
    return (
        safety_payload.get("driver_safety_method_called") is True
        and isinstance(heat_level_percent, int)
        and not isinstance(heat_level_percent, bool)
        and heat_level_percent == 0
        and isinstance(fan_level_percent, int)
        and not isinstance(fan_level_percent, bool)
        and fan_level_percent == 100
        and cooling_on is True
    )


def _apply_emergency_safety_payload(
    session: RoastSession,
    safety_payload: Mapping[str, EventPayloadValue],
) -> None:
    """Apply fail-closed emergency safety state from a driver payload."""
    session.heat_level_percent = _payload_control_percent(
        safety_payload,
        key="heat_level_percent",
        default=0,
    )
    session.fan_level_percent = _payload_control_percent(
        safety_payload,
        key="fan_level_percent",
        default=100,
    )
    cooling_on = safety_payload.get("cooling_on", True)
    session.cooling_on = cooling_on if isinstance(cooling_on, bool) else True


def _build_emergency_fault_payload(
    *,
    reason: str,
    safety_payload: Mapping[str, EventPayloadValue],
) -> dict[str, EventPayloadValue]:
    """Build a fault payload without allowing drivers to override core keys."""
    event_payload = dict(safety_payload)
    event_payload["reason"] = reason
    event_payload["heat_level_percent"] = _payload_control_percent(
        safety_payload,
        key="heat_level_percent",
        default=0,
    )
    event_payload["fan_level_percent"] = _payload_control_percent(
        safety_payload,
        key="fan_level_percent",
        default=100,
    )
    cooling_on = safety_payload.get("cooling_on", True)
    event_payload["cooling_on"] = cooling_on if isinstance(cooling_on, bool) else True
    return event_payload


def _payload_control_percent(
    payload: Mapping[str, EventPayloadValue],
    *,
    key: str,
    default: int,
) -> int:
    """Return a valid control percentage from driver payload or a safe default."""
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if not 0 <= value <= 100:
        return default
    return value


def _copy_session_for_read(session: RoastSession) -> RoastSession:
    """Return a lightweight read snapshot with retained telemetry samples."""
    return RoastSession(
        id=session.id,
        created_at_utc=session.created_at_utc,
        monotonic_start=session.monotonic_start,
        capture_ownership_sequence=session.capture_ownership_sequence,
        beans_added_source=session.beans_added_source,
        first_crack_source=session.first_crack_source,
        timing_evidence_sequence=session.timing_evidence_sequence,
        timing_evidence_last_lo=session.timing_evidence_last_lo,
        timing_evidence_last_hi=session.timing_evidence_last_hi,
        hottop_reception_evidence_sequence=session.hottop_reception_evidence_sequence,
        hottop_reception_evidence_last_lo=session.hottop_reception_evidence_last_lo,
        hottop_reception_evidence_last_hi=session.hottop_reception_evidence_last_hi,
        purpose=session.purpose,
        phase=session.phase,
        beans_added_at_utc=session.beans_added_at_utc,
        beans_added_monotonic_seconds=session.beans_added_monotonic_seconds,
        first_crack_at_utc=session.first_crack_at_utc,
        first_crack_monotonic_seconds=session.first_crack_monotonic_seconds,
        beans_dropped_at_utc=session.beans_dropped_at_utc,
        beans_dropped_monotonic_seconds=session.beans_dropped_monotonic_seconds,
        cooling_started_at_utc=session.cooling_started_at_utc,
        cooling_started_monotonic_seconds=session.cooling_started_monotonic_seconds,
        cooling_stopped_at_utc=session.cooling_stopped_at_utc,
        cooling_stopped_monotonic_seconds=session.cooling_stopped_monotonic_seconds,
        faulted_at_utc=session.faulted_at_utc,
        faulted_monotonic_seconds=session.faulted_monotonic_seconds,
        heat_level_percent=session.heat_level_percent,
        fan_level_percent=session.fan_level_percent,
        cooling_on=session.cooling_on,
        auto_t0_charge_temperature_c=session.auto_t0_charge_temperature_c,
        auto_t0_charge_temperature_monotonic_seconds=(
            session.auto_t0_charge_temperature_monotonic_seconds
        ),
        auto_t0_current_drop_c=session.auto_t0_current_drop_c,
        auto_t0_preheat_sample_count=session.auto_t0_preheat_sample_count,
        event_timeline=deepcopy(session.event_timeline),
        telemetry_buffer=deque(session.telemetry_buffer),
        log_writer=session.log_writer,
        last_logged_telemetry_monotonic_seconds=(session.last_logged_telemetry_monotonic_seconds),
        fc_window_count=session.fc_window_count,
        fc_max_confidence=session.fc_max_confidence,
        fc_max_positive_window_count=session.fc_max_positive_window_count,
        fc_model_confirmed=session.fc_model_confirmed,
        stopped_at_utc=session.stopped_at_utc,
        monotonic_stop=session.monotonic_stop,
        pending_driver_command_token=session.pending_driver_command_token,
        pending_driver_command_kind=session.pending_driver_command_kind,
        finalisation=deepcopy(session.finalisation),
        mock_log_torn=session.mock_log_torn,
        unpersisted_mock_drop_fault_count=session.unpersisted_mock_drop_fault_count,
        torn_start_cooling_attempted=session.torn_start_cooling_attempted,
        unconfirmed_torn_start_cooling_attempted=session.unconfirmed_torn_start_cooling_attempted,
        unrecorded_fault_recovery_drop_accepted=(session.unrecorded_fault_recovery_drop_accepted),
        manual_first_crack_pending=session.manual_first_crack_pending,
        manual_first_crack_export_disqualified=session.manual_first_crack_export_disqualified,
        manual_first_crack_admission_expired=session.manual_first_crack_admission_expired,
        manual_first_crack_canonical_evidence=session.manual_first_crack_canonical_evidence,
        manual_first_crack_audit_unresolved=session.manual_first_crack_audit_unresolved,
        manual_first_crack_endpoint_admitted=session.manual_first_crack_endpoint_admitted,
        manual_charge_persistence_pending=session.manual_charge_persistence_pending,
        manual_charge_persistence_uncertain=session.manual_charge_persistence_uncertain,
        manual_charge_persistence_zero=session.manual_charge_persistence_zero,
    )
