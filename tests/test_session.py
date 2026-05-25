from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.session import (
    RoastEventKind,
    RoastSessionStore,
    SessionLifecycleError,
    TelemetrySample,
    compute_bean_ror_c_per_min,
    compute_bean_temp_delta_60s_c,
    compute_development_percent,
    compute_development_time_seconds,
    compute_env_ror_c_per_min,
    compute_env_temp_delta_60s_c,
    compute_roast_elapsed_seconds,
    compute_roast_metrics,
)

EXPECTED_JSONL_EVENT_KEYS = {
    "session_id",
    "type",
    "kind",
    "recorded_at_utc",
    "monotonic_seconds",
    "payload",
}
EXPECTED_JSONL_TELEMETRY_KEYS = {
    "session_id",
    "type",
    "recorded_at_utc",
    "monotonic_seconds",
    "bean_temp_c",
    "env_temp_c",
    "heat_level_percent",
    "fan_level_percent",
    "cooling_on",
}


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


def test_session_history_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="session_history_limit"):
        RoastSessionStore(session_history_limit=0)


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


def test_record_telemetry_sample_uses_session_clock_and_snapshot_retains_buffer() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 0, 5, tzinfo=UTC)
    clock.monotonic_value = 105.5
    snapshot = store.record_telemetry_sample(
        session,
        bean_temp_c=151.25,
        env_temp_c=204.5,
        heat_level_percent=55,
        fan_level_percent=35,
        cooling_on=False,
    )

    samples = list(snapshot.telemetry_buffer)
    assert len(samples) == 1
    assert samples[0].recorded_at_utc == datetime(2026, 5, 4, 12, 0, 5, tzinfo=UTC)
    assert samples[0].monotonic_seconds == 5.5
    assert samples[0].bean_temp_c == 151.25
    assert samples[0].env_temp_c == 204.5
    assert samples[0].heat_level_percent == 55
    assert samples[0].fan_level_percent == 35
    assert samples[0].cooling_on is False
    assert list(store.get_session_snapshot().telemetry_buffer) == samples


def test_append_only_jsonl_log_writes_events_immediately_and_telemetry_at_1hz(
    tmp_path: Path,
) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
        session_id_factory=lambda: "session-001",
        telemetry_log_interval_seconds=1.0,
    )
    session = store.start_session()

    clock.monotonic_value = 100.0
    store.record_telemetry_sample(
        session,
        bean_temp_c=151.25,
        env_temp_c=204.5,
        heat_level_percent=55,
        fan_level_percent=35,
        cooling_on=False,
    )
    clock.monotonic_value = 100.5
    store.record_telemetry_sample(
        session,
        bean_temp_c=151.5,
        env_temp_c=205.0,
        heat_level_percent=55,
        fan_level_percent=35,
        cooling_on=False,
    )
    clock.monotonic_value = 100.7
    event = store.record_event(session, "beans_added")
    clock.monotonic_value = 101.2
    store.record_telemetry_sample(
        session,
        bean_temp_c=152.0,
        env_temp_c=206.0,
        heat_level_percent=60,
        fan_level_percent=40,
        cooling_on=False,
    )

    log_path = tmp_path / "roasts" / "session-001" / "roast.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [row["type"] for row in rows] == ["telemetry", "event", "telemetry"]
    assert rows[0]["session_id"] == "session-001"
    assert rows[0]["bean_temp_c"] == 151.25
    assert rows[0]["monotonic_seconds"] == 0.0
    assert rows[1]["kind"] == "beans_added"
    assert rows[1]["recorded_at_utc"] == event.recorded_at_utc.isoformat()
    assert round(rows[1]["monotonic_seconds"], 3) == 0.7
    assert rows[2]["bean_temp_c"] == 152.0
    assert rows[2]["env_temp_c"] == 206.0
    assert rows[2]["heat_level_percent"] == 60
    assert rows[2]["fan_level_percent"] == 40
    latest_logged = store.get_session_snapshot().last_logged_telemetry_monotonic_seconds
    assert latest_logged is not None
    assert round(latest_logged, 3) == 1.2


def test_append_only_jsonl_log_schema_completeness(tmp_path: Path) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
        session_id_factory=lambda: "session-001",
        telemetry_log_interval_seconds=1.0,
    )
    session = store.start_session()

    store.record_telemetry_sample(
        session,
        bean_temp_c=151.25,
        env_temp_c=204.5,
        heat_level_percent=55,
        fan_level_percent=35,
        cooling_on=False,
    )
    clock.monotonic_value = 101.0
    store.record_event(
        session,
        "beans_added",
        payload={
            "charge_temp_c": 151.25,
            "detected": False,
        },
    )

    log_path = tmp_path / "roasts" / "session-001" / "roast.jsonl"
    telemetry_row, event_row = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert set(telemetry_row) == EXPECTED_JSONL_TELEMETRY_KEYS
    assert telemetry_row == {
        "session_id": "session-001",
        "type": "telemetry",
        "recorded_at_utc": "2026-05-04T12:00:00+00:00",
        "monotonic_seconds": 0.0,
        "bean_temp_c": 151.25,
        "env_temp_c": 204.5,
        "heat_level_percent": 55,
        "fan_level_percent": 35,
        "cooling_on": False,
    }
    assert set(event_row) == EXPECTED_JSONL_EVENT_KEYS
    assert event_row == {
        "session_id": "session-001",
        "type": "event",
        "kind": "beans_added",
        "recorded_at_utc": "2026-05-04T12:00:00+00:00",
        "monotonic_seconds": 1.0,
        "payload": {
            "charge_temp_c": 151.25,
            "detected": False,
        },
    }


def test_append_only_jsonl_log_includes_automatic_first_crack_events(tmp_path: Path) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
        session_id_factory=lambda: "session-001",
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 110.0
    store.record_first_crack_detection_snapshot(
        session,
        detected_at_monotonic_seconds=109.25,
        payload={"source": "first_crack_detector"},
    )

    log_path = tmp_path / "roasts" / "session-001" / "roast.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["beans_added", "first_crack_detected"]
    assert rows[1]["payload"] == {"source": "first_crack_detector"}
    assert rows[1]["monotonic_seconds"] == 9.25


def test_event_log_write_failure_does_not_commit_event(tmp_path: Path) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
        session_id_factory=lambda: "session-001",
    )
    session = store.start_session()
    blocked_log_dir = tmp_path / "roasts" / "session-001"
    blocked_log_dir.parent.mkdir(parents=True)
    blocked_log_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        store.record_event(session, "beans_added")

    assert session.event_timeline == []
    assert session.phase == "pre_roast"
    assert session.beans_added_at_utc is None


def test_telemetry_log_write_failure_does_not_advance_buffer(tmp_path: Path) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
        session_id_factory=lambda: "session-001",
    )
    session = store.start_session()
    blocked_log_dir = tmp_path / "roasts" / "session-001"
    blocked_log_dir.parent.mkdir(parents=True)
    blocked_log_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        store.record_telemetry_sample(
            session,
            bean_temp_c=151.25,
            env_temp_c=204.5,
            heat_level_percent=55,
            fan_level_percent=35,
            cooling_on=False,
        )

    assert list(session.telemetry_buffer) == []
    assert session.last_logged_telemetry_monotonic_seconds is None


def test_reserved_driver_drop_log_failure_clears_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    store.record_event(session, "beans_added")
    store.apply_driver_control_state(
        session,
        heat_level_percent=35,
        fan_level_percent=25,
        cooling_on=False,
    )
    drop_reservation = store.reserve_driver_drop(session)
    assert drop_reservation.reservation is not None

    def fail_event_log_write(
        _session: session_module.RoastSession,
        _event: session_module.RoastEvent,
    ) -> None:
        raise OSError("log unavailable")

    monkeypatch.setattr(session_module, "_append_event_log_row", fail_event_log_write)

    with pytest.raises(OSError, match="log unavailable"):
        store.complete_reserved_driver_drop_snapshot(
            session,
            reservation=drop_reservation.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        )

    assert session.pending_driver_command_token is None
    assert session.pending_driver_command_kind is None
    assert session.heat_level_percent == 35
    assert session.fan_level_percent == 25
    assert session.cooling_on is False
    assert session.beans_dropped_at_utc is None
    assert session.phase == "roasting"


def test_reserved_driver_start_cooling_log_failure_rolls_back_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    store.record_event(session, "beans_added")
    store.record_event(session, "beans_dropped")
    store.apply_driver_control_state(
        session,
        heat_level_percent=30,
        fan_level_percent=40,
        cooling_on=False,
    )
    cooling_reservation = store.reserve_driver_start_cooling(session)
    assert cooling_reservation.reservation is not None

    def fail_event_log_write(
        _session: session_module.RoastSession,
        _event: session_module.RoastEvent,
    ) -> None:
        raise OSError("log unavailable")

    monkeypatch.setattr(session_module, "_append_event_log_row", fail_event_log_write)

    with pytest.raises(OSError, match="log unavailable"):
        store.complete_reserved_driver_start_cooling_snapshot(
            session,
            reservation=cooling_reservation.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=True,
        )

    assert session.pending_driver_command_token is None
    assert session.pending_driver_command_kind is None
    assert session.heat_level_percent == 30
    assert session.fan_level_percent == 40
    assert session.cooling_on is False
    assert session.cooling_started_at_utc is None
    assert session.phase == "dropped"


def test_reserved_driver_start_cooling_rejects_inactive_driver_result() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    store.record_event(session, "beans_added")
    store.record_event(session, "beans_dropped")
    store.apply_driver_control_state(
        session,
        heat_level_percent=30,
        fan_level_percent=40,
        cooling_on=False,
    )
    cooling_reservation = store.reserve_driver_start_cooling(session)
    assert cooling_reservation.reservation is not None

    with pytest.raises(SessionLifecycleError, match="cooling inactive after start_cooling"):
        store.complete_reserved_driver_start_cooling_snapshot(
            session,
            reservation=cooling_reservation.reservation,
            heat_level_percent=0,
            fan_level_percent=100,
            cooling_on=False,
        )

    assert session.pending_driver_command_token is None
    assert session.pending_driver_command_kind is None
    assert session.heat_level_percent == 30
    assert session.fan_level_percent == 40
    assert session.cooling_on is False
    assert session.cooling_started_at_utc is None
    assert session.event_timeline[-1].kind == "beans_dropped"
    assert session.phase == "dropped"


def test_record_active_telemetry_sample_returns_none_when_session_is_stale() -> None:
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

    assert (
        store.record_active_telemetry_sample(
            session_id=first_session.id,
            bean_temp_c=151.25,
            env_temp_c=204.5,
            heat_level_percent=55,
            fan_level_percent=35,
            cooling_on=False,
        )
        is None
    )
    assert list(second_session.telemetry_buffer) == []


def test_append_telemetry_rejects_out_of_order_samples() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    store.append_telemetry(
        session,
        TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=2.0),
    )
    with pytest.raises(SessionLifecycleError, match="timestamp order"):
        store.append_telemetry(
            session,
            TelemetrySample(recorded_at_utc=clock.utc_now(), monotonic_seconds=1.0),
        )


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


def test_get_session_snapshot_supports_completed_session_after_rollover() -> None:
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

    first_snapshot = store.get_session_snapshot(session_id=first_session.id)
    second_snapshot = store.get_session_snapshot(session_id=second_session.id)

    assert first_snapshot.id == first_session.id
    assert first_snapshot.active is False
    assert second_snapshot.id == second_session.id
    assert second_snapshot.active is True


def test_oldest_completed_session_is_evicted_when_history_limit_is_exceeded() -> None:
    clock = ClockHarness()
    issued_ids = iter(["session-001", "session-002", "session-003"])
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        session_id_factory=lambda: next(issued_ids),
        session_history_limit=2,
    )

    first_session = store.start_session()
    store.stop_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 101.0
    second_session = store.start_session()
    store.stop_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 102.0
    third_session = store.start_session()

    with pytest.raises(SessionLifecycleError, match=first_session.id):
        store.get_session_snapshot(session_id=first_session.id)

    assert store.get_session_snapshot(session_id=second_session.id).id == second_session.id
    assert store.get_session_snapshot(session_id=third_session.id).id == third_session.id


def test_start_cooling_rejects_session_before_bean_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    with pytest.raises(SessionLifecycleError, match="after beans are dropped"):
        store.start_cooling(session)


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("first_crack_detected", "allowed phases: roasting"),
        ("beans_dropped", "allowed phases: roasting, development"),
    ],
)
def test_record_event_rejects_invalid_pre_roast_transitions(
    kind: RoastEventKind,
    match: str,
) -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    with pytest.raises(SessionLifecycleError, match=match):
        store.record_event(session, kind)


def test_record_event_allows_drop_directly_from_roasting_phase() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 120.0
    event = store.record_event(session, "beans_dropped")

    assert event.kind == "beans_dropped"
    assert session.phase == "dropped"
    assert session.first_crack_at_utc is None


def test_auto_t0_records_beans_added_from_drop_against_preheat_max() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    event, first_snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=170.0,
        drop_threshold_c=25.0,
    )
    assert event is None
    assert first_snapshot.phase == "pre_roast"
    assert first_snapshot.auto_t0_charge_temperature_c == 170.0

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    event, snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=143.5,
        drop_threshold_c=25.0,
    )

    assert event is not None
    assert event.kind == "beans_added"
    assert event.recorded_at_utc == clock.utc_value
    assert event.monotonic_seconds == 5.0
    assert event.payload == {
        "source": "auto_t0",
        "charge_temperature_c": 170.0,
        "detected_bean_temperature_c": 143.5,
        "drop_c": 26.5,
        "drop_threshold_c": 25.0,
    }
    assert snapshot.phase == "roasting"
    assert snapshot.beans_added_at_utc == clock.utc_value
    assert snapshot.auto_t0_charge_temperature_c == 170.0
    assert snapshot.auto_t0_current_drop_c == 26.5


def test_auto_t0_uses_max_preheat_for_gradual_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for bean_temp_c in (160.0, 170.0, 162.0, 151.0):
        event, _ = store.process_auto_t0_reading_snapshot(
            session,
            bean_temp_c=bean_temp_c,
            drop_threshold_c=25.0,
        )
        assert event is None

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 112.0
    event, snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=144.9,
        drop_threshold_c=25.0,
    )

    assert event is not None
    assert event.kind == "beans_added"
    assert event.payload["charge_temperature_c"] == 170.0
    assert event.payload["drop_c"] == 25.1
    assert snapshot.phase == "roasting"


def test_auto_t0_pending_status_does_not_round_drop_to_threshold() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=170.0,
        drop_threshold_c=25.0,
    )
    event, snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=145.0004,
        drop_threshold_c=25.0,
    )

    assert event is None
    assert snapshot.phase == "pre_roast"
    assert snapshot.auto_t0_current_drop_c is not None
    assert abs(snapshot.auto_t0_current_drop_c - 24.9996) < 0.000001
    assert snapshot.auto_t0_current_drop_c < 25.0


def test_auto_t0_does_not_guess_without_preheat_baseline() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    event, snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=120.0,
        drop_threshold_c=25.0,
    )

    assert event is None
    assert snapshot.phase == "pre_roast"
    assert snapshot.beans_added_at_utc is None
    assert snapshot.auto_t0_charge_temperature_c == 120.0
    assert snapshot.auto_t0_current_drop_c == 0.0


def test_auto_t0_rejects_invalid_phase_after_manual_override() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    manual_event = store.record_event(session, "beans_added")

    event, snapshot = store.process_auto_t0_reading_snapshot(
        session,
        bean_temp_c=140.0,
        drop_threshold_c=25.0,
    )

    assert event == manual_event
    assert snapshot.phase == "roasting"
    assert len(snapshot.event_timeline) == 1


def test_record_event_keeps_later_phase_when_singleton_event_is_repeated() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    store.record_event(session, "first_crack_detected")

    clock.utc_value = datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    clock.monotonic_value = 125.0
    repeated_event = store.record_event(session, "beans_added")

    assert repeated_event.kind == "beans_added"
    assert session.phase == "development"
    assert len(session.event_timeline) == 2


def test_stop_cooling_marks_session_complete_and_stopped() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 1, 30, tzinfo=UTC)
    clock.monotonic_value = 108.0
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


def test_emergency_stop_faults_and_stops_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 4, tzinfo=UTC)
    clock.monotonic_value = 130.0
    event = store.emergency_stop(session, reason="test-fault")

    assert event.kind == "fault"
    assert session.phase == "fault"
    assert session.active is False
    assert session.heat_level_percent == 0
    assert session.fan_level_percent == 100
    assert session.cooling_on is True
    assert event.payload["driver_safety_method_called"] is False
    assert session.stopped_at_utc == datetime(2026, 5, 4, 12, 4, tzinfo=UTC)
    assert session.monotonic_stop == 130.0


def test_emergency_stop_calls_supplied_driver_safety_action() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    event = store.emergency_stop(
        session,
        reason="driver-owned-safety",
        safety_payload={
            "driver": "test-driver",
            "driver_safety_method": "emergency_stop",
            "driver_safety_method_called": True,
            "heat_level_percent": 0,
            "fan_level_percent": 100,
            "cooling_on": True,
        },
    )

    assert event.kind == "fault"
    assert event.payload["reason"] == "driver-owned-safety"
    assert event.payload["driver"] == "test-driver"
    assert event.payload["driver_safety_method_called"] is True
    assert session.heat_level_percent == 0
    assert session.fan_level_percent == 100
    assert session.cooling_on is True
    assert session.phase == "fault"
    assert session.active is False


def test_emergency_stop_preserves_core_reason_when_driver_payload_collides() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    event = store.emergency_stop(
        session,
        reason="core-reason",
        safety_payload={
            "reason": "driver-reason",
            "driver": "test-driver",
            "heat_level_percent": 25,
            "fan_level_percent": 75,
            "cooling_on": False,
        },
    )

    assert event.payload["reason"] == "core-reason"
    assert event.payload["driver"] == "test-driver"
    assert session.heat_level_percent == 25
    assert session.fan_level_percent == 75
    assert session.cooling_on is False


def test_emergency_stop_can_fault_latest_session_stopped_after_driver_call() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.stop_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    event, snapshot = store.emergency_stop_snapshot(
        session,
        reason="driver-already-ran",
        safety_payload={
            "driver": "test-driver",
            "driver_safety_method": "emergency_stop",
            "driver_safety_method_called": True,
            "heat_level_percent": 0,
            "fan_level_percent": 100,
            "cooling_on": True,
        },
        allow_stopped_latest=True,
    )

    assert event.kind == "fault"
    assert event.payload["reason"] == "driver-already-ran"
    assert event.payload["driver_safety_method_called"] is True
    assert session.active is False
    assert session.phase == "fault"
    assert session.stopped_at_utc == datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    assert session.monotonic_stop == 105.0
    assert snapshot.phase == "fault"
    assert snapshot.event_timeline[-1].kind == "fault"


def test_stop_cooling_recovery_records_new_event_after_completed_session_fault() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.record_event(session, "beans_dropped")
    store.record_event(session, "cooling_started")
    store.stop_cooling(session)

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    store.emergency_stop(
        session,
        reason="post-complete-fault",
        safety_payload={
            "driver": "test-driver",
            "driver_safety_method": "emergency_stop",
            "heat_level_percent": 0,
            "fan_level_percent": 100,
            "cooling_on": True,
        },
        allow_stopped_latest=True,
    )

    clock.utc_value = datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    clock.monotonic_value = 125.0
    reservation = store.reserve_driver_stop_cooling_recovery(session)
    event, snapshot = store.complete_reserved_driver_stop_cooling_recovery_snapshot(
        session,
        reservation=reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=False,
    )

    cooling_events = [
        timeline_event
        for timeline_event in snapshot.event_timeline
        if timeline_event.kind == "cooling_stopped"
    ]
    assert event.kind == cooling_events[-1].kind
    assert event.payload == cooling_events[-1].payload
    assert len(cooling_events) == 2
    assert cooling_events[0].payload == {}
    assert cooling_events[1].payload["recovery_after_fault"] is True
    assert cooling_events[1].recorded_at_utc == datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    assert snapshot.phase == "fault"
    assert snapshot.cooling_on is False


def test_emergency_stop_faults_active_complete_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    store.record_event(session, "beans_dropped")

    clock.utc_value = datetime(2026, 5, 4, 12, 3, tzinfo=UTC)
    clock.monotonic_value = 125.0
    store.record_event(session, "cooling_started")

    clock.utc_value = datetime(2026, 5, 4, 12, 4, tzinfo=UTC)
    clock.monotonic_value = 135.0
    store.record_event(session, "cooling_stopped")

    assert session.active is True
    assert session.phase == "complete"

    clock.utc_value = datetime(2026, 5, 4, 12, 5, tzinfo=UTC)
    clock.monotonic_value = 145.0
    event = store.emergency_stop(session, reason="post-complete-fault")

    assert event.kind == "fault"
    assert event.payload["reason"] == "post-complete-fault"
    assert event.payload["driver_safety_method_called"] is False
    assert session.phase == "fault"
    assert session.active is False
    assert session.heat_level_percent == 0
    assert session.fan_level_percent == 100
    assert session.cooling_on is True
    assert session.stopped_at_utc == datetime(2026, 5, 4, 12, 5, tzinfo=UTC)
    assert session.monotonic_stop == 145.0


def test_set_heat_rejects_faulted_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    store.emergency_stop(session, reason="test-fault")

    with pytest.raises(SessionLifecycleError, match="Stopped sessions"):
        store.set_heat(session, heat_level_percent=50)


def test_record_event_rejects_non_fault_events_after_fault() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()
    store.emergency_stop(session, reason="test-fault")

    with pytest.raises(SessionLifecycleError, match="Stopped sessions"):
        store.record_event(session, "beans_dropped")


def test_start_cooling_rejects_session_that_has_faulted() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 1, 30, tzinfo=UTC)
    clock.monotonic_value = 108.0
    store.record_event(session, "beans_dropped")
    store.record_event(session, "fault", payload={"reason": "test"})

    with pytest.raises(SessionLifecycleError, match="No non-fault events"):
        store.start_cooling(session)


def test_stop_cooling_rejects_session_when_cooling_not_started() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 1, 30, tzinfo=UTC)
    clock.monotonic_value = 108.0
    store.record_event(session, "beans_dropped")

    with pytest.raises(SessionLifecycleError, match="must be started"):
        store.stop_cooling(session)


def test_record_event_rejects_first_crack_after_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.utc_value = datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.utc_value = datetime(2026, 5, 4, 12, 2, tzinfo=UTC)
    clock.monotonic_value = 115.0
    store.record_event(session, "beans_dropped")

    with pytest.raises(SessionLifecycleError, match="allowed phases: roasting"):
        store.record_event(session, "first_crack_detected")


def test_set_heat_rejects_non_integer_values() -> None:
    store = RoastSessionStore()
    session = store.start_session()

    with pytest.raises(TypeError, match="integer"):
        store.set_heat(session, heat_level_percent=True)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="integer"):
        store.set_heat(session, heat_level_percent=12.5)  # type: ignore[arg-type]


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
    ]
    assert [event.monotonic_seconds for event in session.event_timeline] == [
        5.0,
        40.0,
        50.0,
        60.0,
        90.0,
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
    assert [event.payload["source"] for event in session.event_timeline] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]


def test_compute_roast_metrics_from_event_timestamps() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.monotonic_value = 140.0
    store.record_event(session, "first_crack_detected")

    clock.monotonic_value = 155.0
    store.record_event(session, "beans_dropped")

    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)

    assert metrics.roast_elapsed_seconds == 50.0
    assert metrics.development_time_seconds == 15.0
    assert metrics.development_percent == 30.0
    assert metrics.bean_temp_delta_60s_c is None
    assert metrics.env_temp_delta_60s_c is None
    assert metrics.bean_ror_c_per_min is None
    assert metrics.env_ror_c_per_min is None


def test_compute_roast_metrics_returns_none_before_beans_added() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)

    assert metrics.roast_elapsed_seconds is None
    assert metrics.development_time_seconds is None
    assert metrics.development_percent is None
    assert metrics.bean_temp_delta_60s_c is None
    assert metrics.env_temp_delta_60s_c is None
    assert metrics.bean_ror_c_per_min is None
    assert metrics.env_ror_c_per_min is None


def test_compute_roast_metrics_uses_clock_for_active_session() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")

    clock.monotonic_value = 120.0
    store.record_event(session, "first_crack_detected")

    clock.monotonic_value = 150.0
    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)

    assert session.active is True
    assert metrics.roast_elapsed_seconds == 45.0
    assert metrics.development_time_seconds == 30.0
    assert metrics.development_percent == 66.667
    assert metrics.bean_temp_delta_60s_c is None
    assert metrics.env_temp_delta_60s_c is None
    assert metrics.bean_ror_c_per_min is None
    assert metrics.env_ror_c_per_min is None


def test_compute_temperature_deltas_60s_uses_regular_sample_window() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (0.0, 150.0, 180.0),
        (30.0, 160.0, 195.0),
        (60.0, 171.25, 211.5),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)

    assert compute_bean_temp_delta_60s_c(session) == 21.25
    assert compute_env_temp_delta_60s_c(session) == 31.5
    assert metrics.bean_temp_delta_60s_c == 21.25
    assert metrics.env_temp_delta_60s_c == 31.5
    assert metrics.bean_ror_c_per_min == 21.25
    assert metrics.env_ror_c_per_min == 31.5


def test_compute_temperature_deltas_60s_uses_irregular_latest_window() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (3.0, 140.0, 180.0),
        (12.0, 145.0, 188.0),
        (64.0, 171.0, 218.0),
        (71.25, 178.0, 225.5),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    assert compute_bean_temp_delta_60s_c(session) == 33.0
    assert compute_env_temp_delta_60s_c(session) == 37.5


def test_compute_temperature_deltas_60s_skip_missing_sensor_values() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (1.0, 150.0, None),
        (4.0, None, 200.0),
        (20.0, 158.0, None),
        (40.0, None, 215.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    assert compute_bean_temp_delta_60s_c(session) == 8.0
    assert compute_env_temp_delta_60s_c(session) == 15.0


def test_compute_temperature_deltas_60s_return_none_for_single_valid_sample() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=clock.utc_now(),
            monotonic_seconds=1.0,
            bean_temp_c=150.0,
            env_temp_c=None,
        ),
    )
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=clock.utc_now(),
            monotonic_seconds=10.0,
            bean_temp_c=None,
            env_temp_c=200.0,
        ),
    )

    assert compute_bean_temp_delta_60s_c(session) is None
    assert compute_env_temp_delta_60s_c(session) is None


def test_compute_temperature_ror_uses_regular_sample_window() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (0.0, 150.0, 180.0),
        (30.0, 160.0, 195.0),
        (60.0, 171.25, 211.5),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    metrics = compute_roast_metrics(session, monotonic_now=clock.monotonic_now)

    assert compute_bean_ror_c_per_min(session) == 21.25
    assert compute_env_ror_c_per_min(session) == 31.5
    assert metrics.bean_ror_c_per_min == 21.25
    assert metrics.env_ror_c_per_min == 31.5


def test_compute_temperature_ror_uses_irregular_sample_span() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (3.0, 140.0, 180.0),
        (20.0, 150.0, 190.0),
        (64.0, 171.0, 218.0),
        (72.0, 178.0, 226.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    assert compute_bean_ror_c_per_min(session) == 32.308
    assert compute_env_ror_c_per_min(session) == 41.538


def test_compute_temperature_ror_skips_missing_sensor_values() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (1.0, 150.0, None),
        (4.0, None, 200.0),
        (20.0, 158.0, None),
        (40.0, None, 215.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    assert compute_bean_ror_c_per_min(session) == 25.263
    assert compute_env_ror_c_per_min(session) == 25.0


def test_compute_temperature_ror_returns_none_before_min_sample_span() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (1.0, 150.0, 200.0),
        (9.0, 158.0, 212.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    assert compute_bean_ror_c_per_min(session) is None
    assert compute_env_ror_c_per_min(session) is None


def test_compute_temperature_ror_uses_configurable_window_and_min_span() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (0.0, 100.0, 150.0),
        (40.0, 130.0, 170.0),
        (70.0, 145.0, 182.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=clock.utc_now(),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    metrics = compute_roast_metrics(
        session,
        monotonic_now=clock.monotonic_now,
        ror_window_seconds=45,
        ror_min_sample_seconds=20,
    )

    assert (
        compute_bean_ror_c_per_min(
            session,
            window_seconds=45,
            min_sample_seconds=20,
        )
        == 30.0
    )
    assert (
        compute_env_ror_c_per_min(
            session,
            window_seconds=45,
            min_sample_seconds=20,
        )
        == 24.0
    )
    assert metrics.bean_ror_c_per_min == 30.0
    assert metrics.env_ror_c_per_min == 24.0


def test_compute_development_time_seconds_returns_none_before_first_crack() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 130.0

    assert compute_development_time_seconds(session, monotonic_now=clock.monotonic_now) is None


def test_compute_development_time_seconds_uses_current_clock_before_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 128.5
    store.record_event(session, "first_crack_detected")
    clock.monotonic_value = 151.75

    assert compute_development_time_seconds(session, monotonic_now=clock.monotonic_now) == 23.25


def test_compute_development_time_seconds_freezes_at_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 130.0
    store.record_event(session, "first_crack_detected")
    clock.monotonic_value = 165.0
    store.record_event(session, "beans_dropped")
    clock.monotonic_value = 210.0

    assert compute_development_time_seconds(session, monotonic_now=clock.monotonic_now) == 35.0


def test_compute_development_percent_uses_roast_elapsed_denominator() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 145.0
    store.record_event(session, "first_crack_detected")
    clock.monotonic_value = 180.0

    assert compute_development_percent(session, monotonic_now=clock.monotonic_now) == 46.667


def test_compute_roast_elapsed_seconds_returns_none_before_beans_added() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    assert compute_roast_elapsed_seconds(session, monotonic_now=clock.monotonic_now) is None


def test_compute_roast_elapsed_seconds_uses_current_clock_before_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 142.25

    assert compute_roast_elapsed_seconds(session, monotonic_now=clock.monotonic_now) == 37.25


def test_compute_roast_elapsed_seconds_freezes_at_drop() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 155.0
    store.record_event(session, "beans_dropped")
    clock.monotonic_value = 210.0

    assert compute_roast_elapsed_seconds(session, monotonic_now=clock.monotonic_now) == 50.0


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


def test_record_event_rejects_unknown_event_kind_with_lifecycle_error() -> None:
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
    )
    session = store.start_session()

    with pytest.raises(SessionLifecycleError, match="Unknown roast event kind: drum_started"):
        store.record_event(session, cast(RoastEventKind, "drum_started"))


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
