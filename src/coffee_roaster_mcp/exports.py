"""Snapshot export helpers for completed or in-progress roast sessions."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from coffee_roaster_mcp.session import RoastEvent, RoastSession, compute_roast_metrics


@dataclass(frozen=True)
class RoastLogExport:
    """File paths and readiness state for a roast log export.

    Attributes:
        session_id: Session identifier exported.
        log_dir: Directory containing the export files.
        jsonl_path: JSON Lines event export path.
        csv_path: CSV event export path.
        summary_path: Summary JSON export path.
        ready: Whether all export files were written.
        note: Human-readable export scope note.
    """

    session_id: str
    log_dir: Path
    jsonl_path: Path
    csv_path: Path
    summary_path: Path
    ready: bool
    note: str


def export_roast_snapshot(
    session: RoastSession,
    *,
    ror_window_seconds: float = 60.0,
    ror_min_sample_seconds: float = 10.0,
) -> RoastLogExport:
    """Write a deterministic snapshot export for one roast session.

    Epic 2 exports the current session snapshot so the one-process mock flow can
    prove end-to-end file readiness. Epic 5 will replace this narrow event export
    with append-only telemetry and finalized log schemas.

    Args:
        session: Copied session snapshot to export.
        ror_window_seconds: Rolling telemetry window for RoR calculations.
        ror_min_sample_seconds: Minimum valid sensor sample span required for RoR.

    Returns:
        Export file paths and readiness metadata.

    Raises:
        ValueError: If the session has no log writer target.
    """
    if session.log_writer is None:
        raise ValueError("Session log target is unavailable.")

    log_dir = session.log_writer.log_dir.resolve()
    jsonl_path = log_dir / "roast.jsonl"
    csv_path = log_dir / "roast.csv"
    summary_path = log_dir / "summary.json"

    log_dir.mkdir(parents=True, exist_ok=True)
    _write_event_jsonl(jsonl_path, session=session)
    _write_event_csv(csv_path, session=session)
    _write_summary_json(
        summary_path,
        session=session,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )

    return RoastLogExport(
        session_id=session.id,
        log_dir=log_dir,
        jsonl_path=jsonl_path,
        csv_path=csv_path,
        summary_path=summary_path,
        ready=True,
        note=(
            "Snapshot export written from the current in-process session. "
            "Append-only telemetry writers and final log schemas land in Epic 5."
        ),
    )


def _write_event_jsonl(path: Path, *, session: RoastSession) -> None:
    """Write one JSONL row per recorded event."""
    with path.open("w", encoding="utf-8") as output:
        for event in session.event_timeline:
            output.write(json.dumps(_event_row(session=session, event=event), sort_keys=True))
            output.write("\n")


def _write_event_csv(path: Path, *, session: RoastSession) -> None:
    """Write one CSV row per recorded event."""
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "session_id",
                "kind",
                "recorded_at_utc",
                "monotonic_seconds",
                "payload_json",
            ],
        )
        writer.writeheader()
        for event in session.event_timeline:
            writer.writerow(
                {
                    "session_id": session.id,
                    "kind": event.kind,
                    "recorded_at_utc": event.recorded_at_utc.isoformat(),
                    "monotonic_seconds": event.monotonic_seconds,
                    "payload_json": json.dumps(event.payload, sort_keys=True),
                }
            )


def _write_summary_json(
    path: Path,
    *,
    session: RoastSession,
    ror_window_seconds: float = 60.0,
    ror_min_sample_seconds: float = 10.0,
) -> None:
    """Write a compact summary for the exported session snapshot."""
    metrics = compute_roast_metrics(
        session,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )
    payload: dict[str, Any] = {
        "session_id": session.id,
        "active": session.active,
        "phase": session.phase,
        "created_at_utc": session.created_at_utc.isoformat(),
        "stopped_at_utc": _iso_or_none(session.stopped_at_utc),
        "beans_added_at_utc": _iso_or_none(session.beans_added_at_utc),
        "first_crack_at_utc": _iso_or_none(session.first_crack_at_utc),
        "beans_dropped_at_utc": _iso_or_none(session.beans_dropped_at_utc),
        "cooling_started_at_utc": _iso_or_none(session.cooling_started_at_utc),
        "cooling_stopped_at_utc": _iso_or_none(session.cooling_stopped_at_utc),
        "faulted_at_utc": _iso_or_none(session.faulted_at_utc),
        "heat_level_percent": session.heat_level_percent,
        "fan_level_percent": session.fan_level_percent,
        "cooling_on": session.cooling_on,
        "event_count": len(session.event_timeline),
        "metrics": {
            "roast_elapsed_seconds": metrics.roast_elapsed_seconds,
            "development_time_seconds": metrics.development_time_seconds,
            "development_percent": metrics.development_percent,
            "bean_temp_delta_60s_c": metrics.bean_temp_delta_60s_c,
            "env_temp_delta_60s_c": metrics.env_temp_delta_60s_c,
            "bean_ror_c_per_min": metrics.bean_ror_c_per_min,
            "env_ror_c_per_min": metrics.env_ror_c_per_min,
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _event_row(*, session: RoastSession, event: RoastEvent) -> dict[str, Any]:
    """Return a JSON-serializable event export row."""
    return {
        "session_id": session.id,
        "type": "event",
        "kind": event.kind,
        "recorded_at_utc": event.recorded_at_utc.isoformat(),
        "monotonic_seconds": event.monotonic_seconds,
        "payload": dict(event.payload),
    }


def _iso_or_none(value: datetime | None) -> str | None:
    """Return ISO text when a timestamp exists."""
    return value.isoformat() if value is not None else None
