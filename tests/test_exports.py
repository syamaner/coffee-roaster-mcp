"""Snapshot export coverage for first-crack metadata."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from coffee_roaster_mcp.exports import export_roast_snapshot
from coffee_roaster_mcp.session import EventPayloadValue, RoastSessionStore, TelemetrySample


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
        csv_rows = list(csv.DictReader(csv_file))
    first_crack_csv_payload = json.loads(csv_rows[1]["payload_json"])
    assert csv_rows[1]["kind"] == "first_crack_detected"
    assert float(csv_rows[1]["monotonic_seconds"]) == 37.25
    assert first_crack_csv_payload == metadata

    summary = json.loads(export.summary_path.read_text(encoding="utf-8"))
    assert summary["first_crack_at_utc"] == "2026-05-17T14:00:37.250000+00:00"
    assert summary["metrics"]["development_time_seconds"] == 32.75
    assert summary["metrics"]["development_percent"] == 50.385
    assert summary["metrics"]["bean_ror_c_per_min"] is None
    assert summary["metrics"]["env_ror_c_per_min"] is None


def test_snapshot_export_uses_configured_ror_parameters(tmp_path: Path) -> None:
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
