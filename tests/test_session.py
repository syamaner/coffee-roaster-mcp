from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coffee_roaster_mcp.session import (
    RoastEventKind,
    RoastSessionStore,
    SessionLifecycleError,
    TelemetrySample,
)


class ClockHarness:
    """Deterministic wall-clock and monotonic clock supplier for session tests."""

    def __init__(self) -> None:
        self.utc_value = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        self.monotonic_value = 100.0

    def utc_now(self) -> datetime:
        return self.utc_value

    def monotonic_now(self) -> float:
        return self.monotonic_value


def test_start_session_creates_active_roast_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        session_id_factory=lambda: "session-001",
        default_log_dir=Path("/tmp/roasts"),
    )

    session = store.start_session()

    assert session.id == "session-001"
    assert session.created_at_utc == clock.utc_value
    assert session.monotonic_start == 100.0
    assert session.phase == "pre_roast"
    assert session.active is True
    assert session.beans_added_at_utc is None
    assert session.first_crack_at_utc is None
    assert session.beans_dropped_at_utc is None
    assert session.event_timeline == []
    assert list(session.telemetry_buffer) == []
    assert session.log_writer is not None
    assert session.log_writer.session_id == "session-001"
    assert session.log_writer.log_dir == Path("/tmp/roasts/session-001")
    assert store.get_active_session() is session
    assert store.get_latest_session() is session


def test_start_session_rejects_second_active_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    store.start_session()

    with pytest.raises(SessionLifecycleError, match="already exists"):
        store.start_session()


def test_stop_session_marks_session_complete_and_clears_active_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 7, tzinfo=UTC)
    clock.monotonic_value = 142.5
    stopped = store.stop_session()

    assert stopped is session
    assert session.active is False
    assert session.phase == "complete"
    assert session.stopped_at_utc == clock.utc_value
    assert session.monotonic_stop == 142.5
    assert session.elapsed_monotonic_seconds(clock.monotonic_now) == 42.5
    assert store.get_active_session() is None
    assert store.get_latest_session() is session


def test_stop_session_is_clean_when_no_session_exists() -> None:
    store = RoastSessionStore()

    assert store.stop_session() is None


def test_negative_telemetry_buffer_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="telemetry_buffer_limit"):
        RoastSessionStore(telemetry_buffer_limit=-1)


def test_stop_session_returns_none_after_session_already_stopped() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    store.start_session()
    store.stop_session()

    assert store.stop_session() is None


def test_session_telemetry_buffer_retains_only_recent_samples() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        telemetry_buffer_limit=2,
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    store.append_telemetry(
        session,
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=1.0, bean_temp_c=100.0),
    )
    store.append_telemetry(
        session,
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=2.0, bean_temp_c=101.0),
    )
    store.append_telemetry(
        session,
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=3.0, bean_temp_c=102.0),
    )

    samples = list(session.telemetry_buffer)
    assert len(samples) == 2
    assert [sample.monotonic_seconds for sample in samples] == [2.0, 3.0]


def test_start_session_allows_new_session_after_previous_stop() -> None:
    clock = ClockHarness()
    issued_ids = iter(["session-001", "session-002"])
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        session_id_factory=lambda: next(issued_ids),
    )

    first_session = store.start_session()
    clock.utc_value = datetime(2026, 5, 4, 12, 5, tzinfo=UTC)
    clock.monotonic_value = 120.0
    store.stop_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 6, tzinfo=UTC)
    clock.monotonic_value = 121.0
    second_session = store.start_session()

    assert first_session.id == "session-001"
    assert first_session.active is False
    assert second_session.id == "session-002"
    assert second_session.active is True
    assert store.get_active_session() is second_session
    assert store.get_latest_session() is second_session


def test_start_cooling_rejects_session_before_bean_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    with pytest.raises(SessionLifecycleError, match="after beans are dropped"):
        store.start_cooling(session)


def test_stop_cooling_marks_session_complete_and_stopped() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_dropped")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 110.0
    store.start_cooling(session)

    clock.utc_value = datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    clock.monotonic_value = 120.0
    event = store.stop_cooling(session)

    assert event.kind == "cooling_stopped"
    assert session.phase == "complete"
    assert session.active is False
    assert session.cooling_on is False
    assert session.stopped_at_utc == datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    assert session.monotonic_stop == 120.0
    assert store.get_active_session() is None


def test_stop_cooling_rejects_session_when_cooling_not_started() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_dropped")

    with pytest.raises(SessionLifecycleError, match="must be started"):
        store.stop_cooling(session)


def test_store_append_telemetry_rejects_non_latest_session() -> None:
    clock = ClockHarness()
    issued_ids = iter(["session-001", "session-002"])
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        session_id_factory=lambda: next(issued_ids),
    )

    first_session = store.start_session()
    store.stop_session()
    second_session = store.start_session()

    with pytest.raises(SessionLifecycleError, match="latest session"):
        store.append_telemetry(
            first_session,
            TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=1.0),
        )

    store.append_telemetry(
        second_session,
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=2.0),
    )
    assert [sample.monotonic_seconds for sample in second_session.telemetry_buffer] == [2.0]


def test_store_append_telemetry_rejects_stopped_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    store.stop_session()

    with pytest.raises(SessionLifecycleError, match="Stopped sessions"):
        store.append_telemetry(
            session,
            TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=1.0),
        )


def test_record_event_updates_timeline_order_and_authoritative_timestamps() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    event_steps: list[tuple[RoastEventKind, datetime, float]] = [
        ("beans_added", datetime(2026, 5, 4, 12, 1, tzinfo=UTC), 105.0),
        ("first_crack_detected", datetime(2026, 5, 4, 12, 7, tzinfo=UTC), 140.0),
        ("beans_dropped", datetime(2026, 5, 4, 12, 8, tzinfo=UTC), 150.0),
        ("cooling_started", datetime(2026, 5, 4, 12, 8, 30, tzinfo=UTC), 160.0),
        ("cooling_stopped", datetime(2026, 5, 4, 12, 11, tzinfo=UTC), 190.0),
        ("fault", datetime(2026, 5, 4, 12, 12, tzinfo=UTC), 200.0),
    ]

    for kind, utc_value, monotonic_value in event_steps:
        clock.utc_value = utc_value
        clock.monotonic_value = monotonic_value
        store.record_event(session, kind, payload={"source": kind})

    assert [event.kind for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
        "fault",
    ]
    assert [event.monotonic_seconds for event in session.event_timeline] == [
        5.0,
        40.0,
        50.0,
        60.0,
        90.0,
        100.0,
    ]
    assert session.beans_added_at_utc == datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    assert session.beans_added_monotonic_seconds == 5.0
    assert session.first_crack_at_utc == datetime(2026, 5, 4, 12, 7, tzinfo=UTC)
    assert session.first_crack_monotonic_seconds == 40.0
    assert session.beans_dropped_at_utc == datetime(2026, 5, 4, 12, 8, tzinfo=UTC)
    assert session.beans_dropped_monotonic_seconds == 50.0
    assert session.cooling_started_at_utc == datetime(2026, 5, 4, 12, 8, 30, tzinfo=UTC)
    assert session.cooling_started_monotonic_seconds == 60.0
    assert session.cooling_stopped_at_utc == datetime(2026, 5, 4, 12, 11, tzinfo=UTC)
    assert session.cooling_stopped_monotonic_seconds == 90.0
    assert session.faulted_at_utc == datetime(2026, 5, 4, 12, 12, tzinfo=UTC)
    assert session.faulted_monotonic_seconds == 100.0
    assert [event.payload["source"] for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
        "fault",
    ]


def test_record_event_is_idempotent_for_singleton_event_kinds() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    first_event = store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    second_event = store.record_event(session, "beans_added")

    assert second_event is first_event
    assert len(session.event_timeline) == 1
    assert session.beans_added_at_utc == datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    assert session.beans_added_monotonic_seconds == 5.0


def test_record_event_rejects_stopped_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    store.stop_session()

    with pytest.raises(SessionLifecycleError, match="Stopped sessions"):
        store.record_event(session, "beans_added")


def test_record_event_preserves_first_fault_timestamp_across_multiple_faults() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    first_fault = store.record_event(session, "fault", payload={"code": "sensor-timeout"})

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    second_fault = store.record_event(session, "fault", payload={"code": "driver-disconnect"})

    assert [event.kind for event in session.event_timeline] == ["fault", "fault"]
    assert first_fault is not second_fault
    assert session.faulted_at_utc == datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    assert session.faulted_monotonic_seconds == 5.0
    assert session.event_timeline[0].payload["code"] == "sensor-timeout"
    assert session.event_timeline[1].payload["code"] == "driver-disconnect"
