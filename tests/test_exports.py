"""Snapshot export coverage for first-crack metadata."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coffee_roaster_mcp.exports import export_roast_snapshot
from coffee_roaster_mcp.session import EventPayloadValue, RoastSessionStore, TelemetrySample

EXPECTED_CSV_FIELDNAMES = [
    "timestamp_utc",
    "elapsed_seconds",
    "phase",
    "bean_temp_c",
    "env_temp_c",
    "heat_level_percent",
    "fan_level_percent",
    "cooling_on",
    "event",
    "beans_added",
    "first_crack_detected",
    "beans_dropped",
    "development_time_percent",
    "bean_ror_c_per_min",
    "env_ror_c_per_min",
    "bean_delta_60s_c",
    "env_delta_60s_c",
    "fc_model_repo",
    "fc_model_revision",
    "fc_model_precision",
]
EXPECTED_SUMMARY_KEYS = {
    "session_id",
    "active",
    "phase",
    "started_at_utc",
    "created_at_utc",
    "stopped_at_utc",
    "beans_added_at_utc",
    "first_crack_at_utc",
    "beans_dropped_at_utc",
    "cooling_started_at_utc",
    "cooling_stopped_at_utc",
    "faulted_at_utc",
    "heat_level_percent",
    "fan_level_percent",
    "cooling_on",
    "event_count",
    "total_roast_seconds",
    "development_time_seconds",
    "development_time_percent",
    "roaster_driver",
    "first_crack_model",
    "metrics",
}
EXPECTED_SUMMARY_METRICS_KEYS = {
    "roast_elapsed_seconds",
    "development_time_seconds",
    "development_percent",
    "development_time_percent",
    "bean_temp_delta_60s_c",
    "env_temp_delta_60s_c",
    "bean_ror_c_per_min",
    "env_ror_c_per_min",
}
EXPECTED_FIRST_CRACK_MODEL_KEYS = {
    "repo_id",
    "revision",
    "precision",
}


class ClockHarness:
    """Deterministic clock supplier for export tests."""

    def __init__(self) -> None:
        self.utc_value = datetime(2026, 5, 17, 14, 0, tzinfo=UTC)
        self.monotonic_value = 100.0

    def utc_now(self) -> datetime:
        """Return the current test wall-clock timestamp."""
        return self.utc_value

    def monotonic_now(self) -> float:
        """Return the current test monotonic timestamp."""
        return self.monotonic_value


def test_snapshot_export_preserves_first_crack_detector_metadata(tmp_path: Path) -> None:
    """Export first-crack detector metadata in JSONL, CSV, and summary files."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    metadata: dict[str, EventPayloadValue] = {
        "source": "first_crack_detector",
        "detected_at_monotonic_seconds": 137.25,
        "precision": "int8",
        "revision": "v0.1.0",
        "repo_id": "syamaner/coffee-first-crack-detection",
        "onnx_model_filename": "onnx/int8/model_quantized.onnx",
        "feature_extractor_filename": "onnx/int8/preprocessor_config.json",
        "window_sequence_number": 9,
        "confidence": 0.93,
    }
    clock.monotonic_value = 140.0
    store.record_first_crack_detection_snapshot(
        session,
        detected_at_monotonic_seconds=137.25,
        payload=metadata,
    )
    clock.monotonic_value = 170.0
    store.record_event(session, "beans_dropped")

    export = export_roast_snapshot(session)

    jsonl_events = [
        json.loads(line) for line in export.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    first_crack_jsonl = jsonl_events[1]
    assert first_crack_jsonl["kind"] == "first_crack_detected"
    assert first_crack_jsonl["monotonic_seconds"] == 37.25
    assert first_crack_jsonl["payload"] == metadata

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        csv_rows = list(reader)
    assert reader.fieldnames == EXPECTED_CSV_FIELDNAMES
    first_crack_csv_row = csv_rows[1]
    assert first_crack_csv_row["event"] == "first_crack_detected"
    assert first_crack_csv_row["elapsed_seconds"] == "32.25"
    assert first_crack_csv_row["phase"] == "development"
    assert first_crack_csv_row["first_crack_detected"] == "True"
    assert first_crack_csv_row["fc_model_repo"] == "syamaner/coffee-first-crack-detection"
    assert first_crack_csv_row["fc_model_revision"] == "v0.1.0"
    assert first_crack_csv_row["fc_model_precision"] == "int8"

    summary = json.loads(export.summary_path.read_text(encoding="utf-8"))
    assert summary["started_at_utc"] == "2026-05-17T14:00:00+00:00"
    assert summary["first_crack_at_utc"] == "2026-05-17T14:00:37.250000+00:00"
    assert summary["total_roast_seconds"] == 65.0
    assert summary["development_time_seconds"] == 32.75
    assert summary["development_time_percent"] == 50.385
    assert summary["roaster_driver"] == "mock"
    assert summary["first_crack_model"] == {
        "repo_id": "syamaner/coffee-first-crack-detection",
        "revision": "v0.1.0",
        "precision": "int8",
    }
    assert summary["metrics"]["development_time_seconds"] == 32.75
    assert summary["metrics"]["development_percent"] == 50.385
    assert summary["metrics"]["development_time_percent"] == 50.385
    assert summary["metrics"]["bean_ror_c_per_min"] is None
    assert summary["metrics"]["env_ror_c_per_min"] is None


def test_snapshot_export_summary_includes_plan_required_schema(
    tmp_path: Path,
) -> None:
    """Export summary.json with session timing, driver, and model fields."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.utc_value = datetime(2026, 5, 17, 14, 0, 10, tzinfo=UTC)
    clock.monotonic_value = 110.0
    store.record_event(session, "beans_added")
    clock.utc_value = datetime(2026, 5, 17, 14, 1, tzinfo=UTC)
    clock.monotonic_value = 160.0
    store.record_event(
        session,
        "first_crack_detected",
        payload={
            "repo_id": "syamaner/coffee-first-crack-detection",
            "revision": "release-2026-05",
            "precision": "fp32",
            "confidence": 0.91,
        },
    )
    clock.utc_value = datetime(2026, 5, 17, 14, 1, 30, tzinfo=UTC)
    clock.monotonic_value = 190.0
    store.record_event(session, "beans_dropped")

    export = export_roast_snapshot(
        session,
        roaster_driver="hottop_kn8828b_2k_plus",
    )

    summary = json.loads(export.summary_path.read_text(encoding="utf-8"))
    assert summary["session_id"] == session.id
    assert summary["started_at_utc"] == "2026-05-17T14:00:00+00:00"
    assert summary["beans_added_at_utc"] == "2026-05-17T14:00:10+00:00"
    assert summary["first_crack_at_utc"] == "2026-05-17T14:01:00+00:00"
    assert summary["beans_dropped_at_utc"] == "2026-05-17T14:01:30+00:00"
    assert summary["total_roast_seconds"] == 80.0
    assert summary["development_time_seconds"] == 30.0
    assert summary["development_time_percent"] == 37.5
    assert summary["roaster_driver"] == "hottop_kn8828b_2k_plus"
    assert summary["first_crack_model"] == {
        "repo_id": "syamaner/coffee-first-crack-detection",
        "revision": "release-2026-05",
        "precision": "fp32",
    }


def test_snapshot_export_summary_schema_completeness(tmp_path: Path) -> None:
    """Pin the top-level summary, metrics, and model metadata schema keys."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    clock.monotonic_value = 135.0
    store.record_event(
        session,
        "first_crack_detected",
        payload={
            "repo_id": "syamaner/coffee-first-crack-detection",
            "revision": "v0.1.0",
            "precision": "int8",
        },
    )
    clock.monotonic_value = 180.0
    store.record_event(session, "beans_dropped")

    export = export_roast_snapshot(session)

    summary = json.loads(export.summary_path.read_text(encoding="utf-8"))
    assert set(summary) == EXPECTED_SUMMARY_KEYS
    assert set(summary["metrics"]) == EXPECTED_SUMMARY_METRICS_KEYS
    assert set(summary["first_crack_model"]) == EXPECTED_FIRST_CRACK_MODEL_KEYS


def test_snapshot_export_uses_configured_ror_parameters(tmp_path: Path) -> None:
    """Apply configured RoR parameters consistently to summary and CSV rows."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    for monotonic_seconds, bean_temp_c, env_temp_c in (
        (0.0, 150.0, 180.0),
        (12.0, 160.0, 192.0),
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

    export = export_roast_snapshot(
        session,
        ror_window_seconds=60,
        ror_min_sample_seconds=20,
    )

    summary = json.loads(export.summary_path.read_text(encoding="utf-8"))
    assert summary["metrics"]["bean_ror_c_per_min"] is None
    assert summary["metrics"]["env_ror_c_per_min"] is None
    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        csv_rows = list(csv.DictReader(csv_file))
    assert csv_rows[-1]["bean_ror_c_per_min"] == ""
    assert csv_rows[-1]["env_ror_c_per_min"] == ""


def test_snapshot_export_csv_includes_plan_columns_for_telemetry_and_events(
    tmp_path: Path,
) -> None:
    """Export telemetry and event rows with the planned CSV schema."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()

    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 0, 1, tzinfo=UTC),
            monotonic_seconds=1.0,
            bean_temp_c=150.0,
            env_temp_c=180.0,
            heat_level_percent=50,
            fan_level_percent=20,
            cooling_on=False,
        ),
    )
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 0, 15, tzinfo=UTC),
            monotonic_seconds=15.0,
            bean_temp_c=160.0,
            env_temp_c=192.0,
            heat_level_percent=60,
            fan_level_percent=25,
            cooling_on=False,
        ),
    )
    metadata: dict[str, EventPayloadValue] = {
        "repo_id": "syamaner/coffee-first-crack-detection",
        "revision": "v0.1.0",
        "precision": "int8",
    }
    clock.monotonic_value = 135.0
    store.record_event(session, "first_crack_detected", payload=metadata)
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 1, 15, tzinfo=UTC),
            monotonic_seconds=75.0,
            bean_temp_c=170.0,
            env_temp_c=204.0,
            heat_level_percent=45,
            fan_level_percent=40,
            cooling_on=False,
        ),
    )
    clock.monotonic_value = 180.0
    store.record_event(session, "beans_dropped")

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
    assert reader.fieldnames == EXPECTED_CSV_FIELDNAMES
    assert [row["event"] for row in rows] == [
        "",
        "beans_added",
        "",
        "first_crack_detected",
        "",
        "beans_dropped",
    ]
    first_telemetry = rows[0]
    assert first_telemetry["phase"] == "pre_roast"
    assert first_telemetry["elapsed_seconds"] == ""
    assert first_telemetry["bean_temp_c"] == "150.0"
    assert first_telemetry["event"] == ""

    post_fc_telemetry = rows[4]
    assert post_fc_telemetry["phase"] == "development"
    assert post_fc_telemetry["elapsed_seconds"] == "70.0"
    assert post_fc_telemetry["beans_added"] == "True"
    assert post_fc_telemetry["first_crack_detected"] == "True"
    assert post_fc_telemetry["beans_dropped"] == "False"
    assert post_fc_telemetry["development_time_percent"] == "57.143"
    assert post_fc_telemetry["bean_ror_c_per_min"] == "10.0"
    assert post_fc_telemetry["env_ror_c_per_min"] == "12.0"
    assert post_fc_telemetry["bean_delta_60s_c"] == "10.0"
    assert post_fc_telemetry["env_delta_60s_c"] == "12.0"
    assert post_fc_telemetry["fc_model_repo"] == "syamaner/coffee-first-crack-detection"
    assert post_fc_telemetry["fc_model_revision"] == "v0.1.0"
    assert post_fc_telemetry["fc_model_precision"] == "int8"

    drop_event = rows[5]
    assert drop_event["phase"] == "dropped"
    assert drop_event["elapsed_seconds"] == "75.0"
    assert drop_event["beans_dropped"] == "True"


def test_snapshot_export_csv_orders_same_time_events_before_telemetry(
    tmp_path: Path,
) -> None:
    """Emit same-time event rows before telemetry without future sample data."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 0, 5, tzinfo=UTC),
            monotonic_seconds=5.0,
            bean_temp_c=150.0,
            env_temp_c=180.0,
            heat_level_percent=50,
            fan_level_percent=20,
            cooling_on=False,
        ),
    )

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert [row["event"] for row in rows] == ["beans_added", ""]
    assert rows[0]["phase"] == "roasting"
    assert rows[0]["bean_temp_c"] == ""
    assert rows[0]["heat_level_percent"] == ""
    assert rows[0]["bean_delta_60s_c"] == ""
    assert rows[1]["phase"] == "roasting"
    assert rows[1]["bean_temp_c"] == "150.0"
    assert rows[1]["heat_level_percent"] == "50"


def test_snapshot_export_csv_keeps_same_time_event_rows_chronological(
    tmp_path: Path,
) -> None:
    """Keep same-time event rows scoped to their own lifecycle order."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.record_event(
        session,
        "first_crack_detected",
        payload={
            "repo_id": "syamaner/coffee-first-crack-detection",
            "revision": "v0.1.0",
            "precision": "int8",
        },
    )

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert [row["event"] for row in rows] == ["beans_added", "first_crack_detected"]
    assert rows[0]["phase"] == "roasting"
    assert rows[0]["first_crack_detected"] == "False"
    assert rows[0]["fc_model_repo"] == ""
    assert rows[1]["phase"] == "development"
    assert rows[1]["first_crack_detected"] == "True"
    assert rows[1]["fc_model_repo"] == "syamaner/coffee-first-crack-detection"


def test_snapshot_export_csv_event_rows_use_transition_control_state(
    tmp_path: Path,
) -> None:
    """Use event transition state instead of stale telemetry on event rows."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 1, 0, tzinfo=UTC),
            monotonic_seconds=60.0,
            bean_temp_c=170.0,
            env_temp_c=204.0,
            heat_level_percent=45,
            fan_level_percent=40,
            cooling_on=False,
        ),
    )
    clock.monotonic_value = 180.0
    store.record_event(session, "beans_dropped")
    store.record_event(session, "cooling_started")
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 1, 5, tzinfo=UTC),
            monotonic_seconds=82.0,
            bean_temp_c=168.0,
            env_temp_c=200.0,
            heat_level_percent=0,
            fan_level_percent=40,
            cooling_on=True,
        ),
    )
    clock.monotonic_value = 185.0
    store.record_event(session, "cooling_stopped")

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    drop_row = next(row for row in rows if row["event"] == "beans_dropped")
    cooling_row = next(row for row in rows if row["event"] == "cooling_started")
    cooling_stopped_row = next(row for row in rows if row["event"] == "cooling_stopped")
    assert drop_row["heat_level_percent"] == "0"
    assert drop_row["cooling_on"] == "False"
    assert cooling_row["cooling_on"] == "True"
    assert cooling_stopped_row["cooling_on"] == "False"


def test_snapshot_export_csv_keeps_fault_phase_for_recovery_cooling_stop(
    tmp_path: Path,
) -> None:
    """Keep post-emergency recovery rows classified as faulted."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.emergency_stop(
        session,
        reason="unit-test",
        safety_payload={
            "driver": "mock",
            "driver_safety_method": "emergency_stop",
            "heat_level_percent": 0,
            "fan_level_percent": 100,
            "cooling_on": True,
        },
    )
    clock.monotonic_value = 110.0
    reservation = store.reserve_driver_stop_cooling_recovery(session)
    store.complete_reserved_driver_stop_cooling_recovery_snapshot(
        session,
        reservation=reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=False,
    )

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    fault_row = next(row for row in rows if row["event"] == "fault")
    recovery_row = next(row for row in rows if row["event"] == "cooling_stopped")
    assert fault_row["phase"] == "fault"
    assert recovery_row["phase"] == "fault"
    assert recovery_row["cooling_on"] == "False"


def test_snapshot_export_csv_uses_driver_transition_payload_state(
    tmp_path: Path,
) -> None:
    """Use driver-returned transition payload state for drop and cooling rows."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    clock.monotonic_value = 105.0
    store.record_event(session, "beans_added")
    store.append_telemetry(
        session,
        TelemetrySample(
            recorded_at_utc=datetime(2026, 5, 17, 14, 1, 0, tzinfo=UTC),
            monotonic_seconds=60.0,
            bean_temp_c=170.0,
            env_temp_c=204.0,
            heat_level_percent=45,
            fan_level_percent=40,
            cooling_on=False,
        ),
    )
    clock.monotonic_value = 180.0
    drop_reservation = store.reserve_driver_drop(session)
    assert drop_reservation.reservation is not None
    store.complete_reserved_driver_drop_snapshot(
        session,
        reservation=drop_reservation.reservation,
        heat_level_percent=0,
        fan_level_percent=100,
        cooling_on=True,
    )

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    drop_row = next(row for row in rows if row["event"] == "beans_dropped")
    cooling_row = next(row for row in rows if row["event"] == "cooling_started")
    assert drop_row["heat_level_percent"] == "0"
    assert drop_row["fan_level_percent"] == "100"
    assert drop_row["cooling_on"] == "True"
    assert cooling_row["heat_level_percent"] == "0"
    assert cooling_row["fan_level_percent"] == "100"
    assert cooling_row["cooling_on"] == "True"


def test_snapshot_export_csv_telemetry_metrics_do_not_use_later_same_time_samples(
    tmp_path: Path,
) -> None:
    """Compute telemetry row metrics only from samples visible up to that row."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    for bean_temp_c, env_temp_c, monotonic_seconds in (
        (150.0, 180.0, 0.0),
        (160.0, 192.0, 60.0),
        (190.0, 228.0, 60.0),
    ):
        store.append_telemetry(
            session,
            TelemetrySample(
                recorded_at_utc=datetime(2026, 5, 17, 14, 1, tzinfo=UTC),
                monotonic_seconds=monotonic_seconds,
                bean_temp_c=bean_temp_c,
                env_temp_c=env_temp_c,
            ),
        )

    export = export_roast_snapshot(session)

    with export.csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    first_same_time_row = rows[1]
    second_same_time_row = rows[2]
    assert first_same_time_row["bean_temp_c"] == "160.0"
    assert first_same_time_row["bean_delta_60s_c"] == "10.0"
    assert first_same_time_row["bean_ror_c_per_min"] == "10.0"
    assert second_same_time_row["bean_temp_c"] == "190.0"
    assert second_same_time_row["bean_delta_60s_c"] == "40.0"
    assert second_same_time_row["bean_ror_c_per_min"] == "40.0"


def test_snapshot_export_rejects_existing_jsonl_directory(tmp_path: Path) -> None:
    """Reject non-file JSONL export paths instead of reporting readiness."""
    clock = ClockHarness()
    store = RoastSessionStore(
        utc_now=clock.utc_now,
        monotonic_now=clock.monotonic_now,
        default_log_dir=tmp_path / "roasts",
    )
    session = store.start_session()
    store.record_event(session, "beans_added")
    assert session.log_writer is not None
    jsonl_path = session.log_writer.log_dir / "roast.jsonl"
    jsonl_path.unlink()
    jsonl_path.mkdir(parents=True)

    with pytest.raises(ValueError, match="JSONL export path exists but is not a file"):
        export_roast_snapshot(session)
