"""Roast session lifecycle and event-timeline models for RoastPilot."""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Literal
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
DriverCommandKind = Literal["control", "drop", "start_cooling", "stop_cooling"]

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
    path = session.log_writer.log_dir / "roast.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, sort_keys=True))
        output.write("\n")


def _append_event_log_row(session: RoastSession, event: RoastEvent) -> None:
    """Append one event row to the session's durable roast log."""
    _append_jsonl_log_row(
        session,
        {
            "session_id": session.id,
            "type": "event",
            "kind": event.kind,
            "recorded_at_utc": event.recorded_at_utc.isoformat(),
            "monotonic_seconds": event.monotonic_seconds,
            "payload": dict(event.payload),
        },
    )


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
        auto_t0_current_drop_c: Current drop from the tracked charge temperature.
        auto_t0_preheat_sample_count: Number of valid pre-T0 bean-temperature samples.
        event_timeline: Shared ordered event timeline for future runtime stories.
        telemetry_buffer: Rolling telemetry sample buffer.
        log_writer: Append-only log writer reference when available.
        last_logged_telemetry_monotonic_seconds: Latest telemetry timestamp
            written to the append-only JSONL log.
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
    heat_level_percent: int = 0
    fan_level_percent: int = 0
    cooling_on: bool = False
    auto_t0_charge_temperature_c: float | None = None
    auto_t0_current_drop_c: float | None = None
    auto_t0_preheat_sample_count: int = 0
    event_timeline: list[RoastEvent] = field(default_factory=_event_timeline_default)
    telemetry_buffer: deque[TelemetrySample] = field(default_factory=_telemetry_buffer_default)
    log_writer: LogWriterReference | None = None
    last_logged_telemetry_monotonic_seconds: float | None = None
    stopped_at_utc: datetime | None = None
    monotonic_stop: float | None = None
    pending_driver_command_token: str | None = None
    pending_driver_command_kind: DriverCommandKind | None = None

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
        """
        import time

        if telemetry_buffer_limit < 0:
            raise ValueError("telemetry_buffer_limit must be >= 0.")
        if session_history_limit < 1:
            raise ValueError("session_history_limit must be >= 1.")
        if not math.isfinite(telemetry_log_interval_seconds) or telemetry_log_interval_seconds <= 0:
            raise ValueError("telemetry_log_interval_seconds must be greater than 0.")

        self._telemetry_buffer_limit = telemetry_buffer_limit
        self._telemetry_log_interval_seconds = telemetry_log_interval_seconds
        self._session_history_limit = session_history_limit
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._monotonic_now = monotonic_now or time.monotonic
        self._session_id_factory = session_id_factory or _generate_session_id
        self._default_log_dir = default_log_dir
        self._lock = RLock()
        self._latest_session: RoastSession | None = None
        self._sessions_by_id: dict[str, RoastSession] = {}
        self._session_id_order: deque[str] = deque()
        self._pending_session_start_token: str | None = None

    def start_session(self) -> RoastSession:
        """Start one new active session.

        Returns:
            The created active roast session.

        Raises:
            SessionLifecycleError: If an active session already exists.
        """
        with self._lock:
            if self._pending_session_start_token is not None:
                raise SessionLifecycleError("A roast session start is already in progress.")
            return self._start_session_locked()

    def start_session_snapshot(self) -> RoastSession:
        """Start one new session and return an atomic lightweight snapshot."""
        with self._lock:
            session = self.start_session()
            return _copy_session_for_read(session)

    def reserve_session_start(self) -> SessionStartReservation:
        """Reserve session startup before preparing the configured driver."""
        with self._lock:
            if self._latest_session is not None and self._latest_session.active:
                raise SessionLifecycleError("An active roast session already exists.")
            if self._pending_session_start_token is not None:
                raise SessionLifecycleError("A roast session start is already in progress.")
            reservation = SessionStartReservation(token=_generate_session_id())
            self._pending_session_start_token = reservation.token
            return reservation

    def complete_session_start_snapshot(
        self,
        reservation: SessionStartReservation,
    ) -> RoastSession:
        """Create the reserved session and return an atomic lightweight snapshot."""
        with self._lock:
            self._assert_session_start_reservation(reservation)
            try:
                session = self._start_session_locked()
            finally:
                self._pending_session_start_token = None
            return _copy_session_for_read(session)

    def clear_session_start_reservation(
        self,
        reservation: SessionStartReservation,
    ) -> None:
        """Clear a pending session-start reservation if it is still current."""
        with self._lock:
            if self._pending_session_start_token == reservation.token:
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
            return self._reserve_driver_command_locked(session, kind=kind)

    def reserve_driver_drop(self, session: RoastSession) -> DriverDropReservation:
        """Reserve a driver drop command or return the existing drop event."""
        with self._lock:
            self._assert_latest_active_session(session)
            existing_event = self._get_existing_singleton_event(session, "beans_dropped")
            if existing_event is not None:
                return DriverDropReservation(
                    reservation=None,
                    existing_event=existing_event,
                    snapshot=_copy_session_for_read(session),
                )
            _validate_event_transition(session, "beans_dropped")
            return DriverDropReservation(
                reservation=self._reserve_driver_command_locked(session, kind="drop")
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
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling can only start after beans are dropped.")
            _validate_event_transition(session, "cooling_started")
            return DriverEventReservation(
                reservation=self._reserve_driver_command_locked(session, kind="start_cooling")
            )

    def reserve_driver_stop_cooling(self, session: RoastSession) -> DriverCommandReservation:
        """Reserve a cooling-stop command for an active cooling session."""
        with self._lock:
            self._assert_latest_active_session(session)
            if session.beans_dropped_at_utc is None:
                raise SessionLifecycleError("Cooling cannot stop before beans are dropped.")
            if session.cooling_started_at_utc is None or not session.cooling_on:
                raise SessionLifecycleError("Cooling must be started before it can be stopped.")
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
            self._assert_latest_session(session)
            if session.active:
                raise SessionLifecycleError("Recovery cooling stop requires a stopped session.")
            if session.faulted_at_utc is None or session.phase != "fault":
                raise SessionLifecycleError(
                    "Recovery cooling stop is only allowed after an emergency stop."
                )
            if not session.cooling_on:
                raise SessionLifecycleError("Cooling must be active before it can be stopped.")
            return self._reserve_driver_command_locked(session, kind="stop_cooling")

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

    def cancel_pending_driver_command(self, session: RoastSession) -> None:
        """Cancel any pending non-emergency driver command for one session."""
        with self._lock:
            self._assert_latest_session(session)
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
            if session.faulted_at_utc is not None and kind != "fault":
                raise SessionLifecycleError("No non-fault events can be recorded after a fault.")

            existing_event = self._get_existing_singleton_event(session, kind)
            if existing_event is not None:
                return existing_event
            _validate_event_transition(session, kind)

            recorded_at_utc = self._utc_now()
            monotonic_seconds = session.elapsed_monotonic_seconds(self._monotonic_now)
            event = RoastEvent(
                kind=kind,
                recorded_at_utc=recorded_at_utc,
                monotonic_seconds=monotonic_seconds,
                payload={} if payload is None else dict(payload),
            )
            _append_event_log_row(session, event)
            session.event_timeline.append(event)
            _apply_event_timestamp(session, event)
            return event

    def record_event_snapshot(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue] | None = None,
    ) -> tuple[RoastEvent, RoastSession]:
        """Record one event and return the event plus an atomic lightweight snapshot."""
        with self._lock:
            event = self.record_event(session, kind, payload=payload)
            return event, _copy_session_for_read(session)

    def process_auto_t0_reading_snapshot(
        self,
        session: RoastSession,
        *,
        bean_temp_c: float,
        drop_threshold_c: float,
    ) -> tuple[RoastEvent | None, RoastSession]:
        """Process one bean-temperature reading for automatic T0 detection.

        Args:
            session: Session to mutate.
            bean_temp_c: Current bean temperature from the configured driver.
            drop_threshold_c: Required drop from max preheat temperature.

        Returns:
            The recorded beans-added event when this reading crosses the
            threshold, otherwise `None`, plus an atomic session snapshot.

        Raises:
            SessionLifecycleError: If called outside the pre-T0 active phase or
                with non-finite inputs.
        """
        with self._lock:
            self._assert_latest_active_session(session)
            if session.beans_added_at_utc is not None:
                return self._get_existing_singleton_event(session, "beans_added"), (
                    _copy_session_for_read(session)
                )
            _validate_event_transition(session, "beans_added")
            current_temp = float(bean_temp_c)
            threshold = float(drop_threshold_c)
            if not math.isfinite(current_temp):
                raise SessionLifecycleError("Automatic T0 bean temperature must be finite.")
            if not math.isfinite(threshold) or threshold <= 0:
                raise SessionLifecycleError("Automatic T0 threshold must be greater than 0.")

            previous_max = session.auto_t0_charge_temperature_c
            if previous_max is None or current_temp > previous_max:
                session.auto_t0_charge_temperature_c = current_temp
                previous_max = current_temp

            session.auto_t0_preheat_sample_count += 1
            drop_c = previous_max - current_temp
            session.auto_t0_current_drop_c = max(0.0, drop_c)
            if session.auto_t0_preheat_sample_count < 2 or drop_c < threshold:
                return None, _copy_session_for_read(session)

            event = self.record_event(
                session,
                "beans_added",
                payload={
                    "source": "auto_t0",
                    "charge_temperature_c": round(previous_max, 3),
                    "detected_bean_temperature_c": round(current_temp, 3),
                    "drop_c": round(drop_c, 3),
                    "drop_threshold_c": round(threshold, 3),
                },
            )
            return event, _copy_session_for_read(session)

    def record_first_crack_detection_snapshot(
        self,
        session: RoastSession,
        *,
        detected_at_monotonic_seconds: float,
        max_future_seconds: float = 0.0,
        payload: dict[str, EventPayloadValue] | None = None,
    ) -> tuple[RoastEvent, RoastSession]:
        """Record automatic first crack at the detector-provided monotonic time.

        Args:
            session: Session to mutate.
            detected_at_monotonic_seconds: Absolute monotonic timestamp reported
                by the detector for the first-crack event.
            max_future_seconds: Allowed future timestamp tolerance. This lets
                adapter-inferred window-end defaults record when the capture
                window has just been emitted but its inferred end timestamp is
                slightly ahead of the integration clock.
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
            if session.faulted_at_utc is not None:
                raise SessionLifecycleError("No non-fault events can be recorded after a fault.")

            existing_event = self._get_existing_singleton_event(
                session,
                "first_crack_detected",
            )
            if existing_event is not None:
                return existing_event, _copy_session_for_read(session)
            _validate_event_transition(session, "first_crack_detected")

            detected_elapsed_seconds = _detected_elapsed_seconds(
                session,
                detected_at_monotonic_seconds=detected_at_monotonic_seconds,
                current_elapsed_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
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
            return event

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

    def copy_session(self, session: RoastSession) -> RoastSession:
        """Return a deep-copied snapshot of one known session object under the store lock."""
        with self._lock:
            return _copy_session_for_read(session)

    @property
    def telemetry_buffer_limit(self) -> int:
        """Return the per-session telemetry retention limit."""
        return self._telemetry_buffer_limit

    def _assert_latest_active_session(self, session: RoastSession) -> None:
        """Validate that one session is the current mutable active session."""
        self._assert_latest_session(session)
        if not session.active:
            raise SessionLifecycleError("Stopped sessions cannot be mutated.")

    def _assert_latest_session(self, session: RoastSession) -> None:
        """Validate that one session is the latest known session."""
        if self._latest_session is not session:
            raise SessionLifecycleError("Only the latest session can be mutated.")

    def _record_stopped_session_event_locked(
        self,
        session: RoastSession,
        kind: RoastEventKind,
        *,
        payload: dict[str, EventPayloadValue],
    ) -> RoastEvent:
        """Record a narrowly allowed event for a stopped latest session."""
        existing_event = self._get_existing_singleton_event(session, kind)
        if existing_event is not None:
            return existing_event
        event = RoastEvent(
            kind=kind,
            recorded_at_utc=self._utc_now(),
            monotonic_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
            payload=dict(payload),
        )
        _append_event_log_row(session, event)
        session.event_timeline.append(event)
        _apply_event_timestamp(session, event)
        return event

    def _start_session_locked(self) -> RoastSession:
        """Start one new active session while the store lock is already held."""
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
        self._sessions_by_id[session_id] = session
        self._session_id_order.append(session_id)
        self._prune_session_history_locked()
        return session

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
        if session.active:
            return self.record_event(session, "fault", payload=payload)
        if not allow_stopped_latest:
            raise SessionLifecycleError("Stopped sessions cannot be mutated.")
        if session.faulted_at_utc is None:
            _validate_event_transition(session, "fault")
        event = RoastEvent(
            kind="fault",
            recorded_at_utc=self._utc_now(),
            monotonic_seconds=session.elapsed_monotonic_seconds(self._monotonic_now),
            payload=dict(payload),
        )
        _append_event_log_row(session, event)
        session.event_timeline.append(event)
        _apply_event_timestamp(session, event)
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


def _detected_elapsed_seconds(
    session: RoastSession,
    *,
    detected_at_monotonic_seconds: float,
    current_elapsed_seconds: float,
    max_future_seconds: float,
) -> float:
    if max_future_seconds < 0:
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
        if future_delta_seconds > max_future_seconds:
            raise SessionLifecycleError("Detected first-crack timestamp cannot be in the future.")
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
        auto_t0_current_drop_c=session.auto_t0_current_drop_c,
        auto_t0_preheat_sample_count=session.auto_t0_preheat_sample_count,
        event_timeline=deepcopy(session.event_timeline),
        telemetry_buffer=deque(session.telemetry_buffer),
        log_writer=session.log_writer,
        last_logged_telemetry_monotonic_seconds=(session.last_logged_telemetry_monotonic_seconds),
        stopped_at_utc=session.stopped_at_utc,
        monotonic_stop=session.monotonic_stop,
        pending_driver_command_token=session.pending_driver_command_token,
        pending_driver_command_kind=session.pending_driver_command_kind,
    )
