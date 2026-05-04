"""Roast session lifecycle and event-timeline models for RoastPilot."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

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

_SINGLETON_EVENT_KINDS: frozenset[RoastEventKind] = frozenset(
    {
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    }
)


def _event_payload_default() -> dict[str, EventPayloadValue]:
    """Return an empty typed event payload mapping."""
    return {}


def _event_timeline_default() -> list[RoastEvent]:
    """Return an empty typed event timeline."""
    return []


def _telemetry_buffer_default() -> deque[TelemetrySample]:
    """Return an empty typed telemetry buffer."""
    return deque()


def _append_telemetry_with_limit(
    session: RoastSession,
    sample: TelemetrySample,
    *,
    max_samples: int,
) -> None:
    """Append telemetry to one session with an explicit retention limit."""
    if max_samples < 0:
        raise ValueError("max_samples must be >= 0.")
    session.telemetry_buffer.append(sample)
    while len(session.telemetry_buffer) > max_samples:
        session.telemetry_buffer.popleft()


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
        event_timeline: Shared ordered event timeline for future runtime stories.
        telemetry_buffer: Rolling telemetry sample buffer.
        log_writer: Append-only log writer reference when available.
        stopped_at_utc: Wall-clock UTC stop time once the session is stopped.
        monotonic_stop: Monotonic clock value captured when the session stops.
    """

    id: str
    created_at_utc: datetime
    monotonic_start: float
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
    event_timeline: list[RoastEvent] = field(default_factory=_event_timeline_default)
    telemetry_buffer: deque[TelemetrySample] = field(default_factory=_telemetry_buffer_default)
    log_writer: LogWriterReference | None = None
    stopped_at_utc: datetime | None = None
    monotonic_stop: float | None = None

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


class SessionLifecycleError(RuntimeError):
    """Raised when a roast session lifecycle transition is invalid."""


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
        utc_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        default_log_dir: Path = Path("./logs/roasts"),
    ) -> None:
        """Initialize the single-session store.

        Args:
            telemetry_buffer_limit: Maximum retained telemetry samples per session.
            utc_now: Optional UTC timestamp supplier.
            monotonic_now: Optional monotonic clock supplier.
            session_id_factory: Optional session id supplier.
            default_log_dir: Base directory for future log writer references.
        """
        import time

        if telemetry_buffer_limit < 0:
            raise ValueError("telemetry_buffer_limit must be >= 0.")

        self._telemetry_buffer_limit = telemetry_buffer_limit
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._monotonic_now = monotonic_now or time.monotonic
        self._session_id_factory = session_id_factory or _generate_session_id
        self._default_log_dir = default_log_dir
        self._lock = RLock()
        self._latest_session: RoastSession | None = None

    def start_session(self) -> RoastSession:
        """Start one new active session.

        Returns:
            The created active roast session.

        Raises:
            SessionLifecycleError: If an active session already exists.
        """
        with self._lock:
            if self._latest_session is not None and self._latest_session.active:
                raise SessionLifecycleError("An active roast session already exists.")

            session_id = self._session_id_factory()
            session = RoastSession(
                id=session_id,
                created_at_utc=self._utc_now(),
                monotonic_start=self._monotonic_now(),
                log_writer=LogWriterReference(
                    session_id=session_id,
                    log_dir=self._default_log_dir / session_id,
                ),
            )
            self._latest_session = session
            return session

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
            _append_telemetry_with_limit(
                session,
                sample,
                max_samples=self._telemetry_buffer_limit,
            )

    def record_event(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
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
        with self._lock:
            self._assert_latest_active_session(session)

            existing_event = self._get_existing_singleton_event(session, kind)
            if existing_event is not None:
                return existing_event

            recorded_at_utc = self._utc_now()
            monotonic_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
            event = RoastEvent(
                kind=kind,
                recorded_at_utc=recorded_at_utc,
                monotonic_seconds=monotonic_seconds,
                payload={} if payload is None else dict(payload),
            )
            session.event_timeline.append(event)
            _apply_event_timestamp(session, event)
            return event

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

    @property
    def telemetry_buffer_limit(self) -> int:
        """Return the per-session telemetry retention limit."""
        return self._telemetry_buffer_limit

    def _assert_latest_active_session(self, session: RoastSession) -> None:
        """Validate that one session is the current mutable active session."""
        if self._latest_session is not session:
            raise SessionLifecycleError("Only the latest session can be mutated.")
        if not session.active:
            raise SessionLifecycleError("Stopped sessions cannot be mutated.")

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


def _apply_event_timestamp(session: RoastSession, event: RoastEvent) -> None:
    """Update authoritative event timestamp fields from one timeline event."""
    if event.kind == "beans_added":
        session.beans_added_at_utc = event.recorded_at_utc
        session.beans_added_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "first_crack_detected":
        session.first_crack_at_utc = event.recorded_at_utc
        session.first_crack_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "beans_dropped":
        session.beans_dropped_at_utc = event.recorded_at_utc
        session.beans_dropped_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "cooling_started":
        session.cooling_started_at_utc = event.recorded_at_utc
        session.cooling_started_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "cooling_stopped":
        session.cooling_stopped_at_utc = event.recorded_at_utc
        session.cooling_stopped_monotonic_seconds = event.monotonic_seconds
        return
    if event.kind == "fault" and session.faulted_at_utc is None:
        session.faulted_at_utc = event.recorded_at_utc
        session.faulted_monotonic_seconds = event.monotonic_seconds


def _generate_session_id() -> str:
    """Return a stable opaque session identifier."""
    return uuid4().hex
