from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coffee_roaster_mcp.session import (
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


def test_stop_session_marks_session_complete_and_clears_active_reference() -> None:
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


def test_session_telemetry_buffer_retains_only_recent_samples() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        telemetry_buffer_limit=2,
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    session.append_telemetry(
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=1.0, bean_temp_c=100.0),
        max_samples=store.telemetry_buffer_limit,
    )
    session.append_telemetry(
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=2.0, bean_temp_c=101.0),
        max_samples=store.telemetry_buffer_limit,
    )
    session.append_telemetry(
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=3.0, bean_temp_c=102.0),
        max_samples=store.telemetry_buffer_limit,
    )

    samples = list(session.telemetry_buffer)
    assert len(samples) == 2
    assert [sample.monotonic_seconds for sample in samples] == [2.0, 3.0]
