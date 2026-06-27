"""Snapshot export helpers for completed or in-progress roast sessions."""

from __future__ import annotations

import csv
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from coffee_roaster_mcp.session import (
    EventPayloadValue,
    RoastEvent,
    RoastPhase,
    RoastSession,
    TelemetrySample,
    compute_roast_metrics,
)

_CSV_FIELDNAMES: tuple[str, ...] = (
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
)


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
    roaster_driver: str = "mock",
    ror_window_seconds: float = 60.0,
    ror_min_sample_seconds: float = 10.0,
) -> RoastLogExport:
    """Write a deterministic snapshot export for one roast session.

    Epic 2 exports the current session snapshot so the one-process mock flow can
    prove end-to-end file readiness. Epic 5 will replace this narrow event export
    with append-only telemetry and finalized log schemas.

    Args:
        session: Copied session snapshot to export.
        roaster_driver: Configured roaster driver used for the session.
        ror_window_seconds: Rolling telemetry window for RoR calculations.
        ror_min_sample_seconds: Minimum valid sensor sample span required for RoR.

    Returns:
        Export file paths and readiness metadata.

    Raises:
        ValueError: If the session has no log writer target or the JSONL path
            is not a regular file.
    """
    if session.log_writer is None:
        raise ValueError("Session log target is unavailable.")

    log_dir = session.log_writer.log_dir.resolve()
    jsonl_path = log_dir / "roast.jsonl"
    csv_path = log_dir / "roast.csv"
    summary_path = log_dir / "summary.json"

    log_dir.mkdir(parents=True, exist_ok=True)
    if jsonl_path.exists() and not jsonl_path.is_file():
        raise ValueError(f"JSONL export path exists but is not a file: {jsonl_path}")
    if not jsonl_path.exists():
        _write_event_jsonl(jsonl_path, session=session)
    _write_event_csv(
        csv_path,
        session=session,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )
    _write_summary_json(
        summary_path,
        session=session,
        roaster_driver=roaster_driver,
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
            "Snapshot CSV and summary export written from the current "
            "in-process session. JSONL is append-only during the roast."
        ),
    )


def _write_event_jsonl(path: Path, *, session: RoastSession) -> None:
    """Write one JSONL row per recorded event."""
    with path.open("w", encoding="utf-8") as output:
        for event in session.event_timeline:
            output.write(json.dumps(_event_row(session=session, event=event), sort_keys=True))
            output.write("\n")


def _write_event_csv(
    path: Path,
    *,
    session: RoastSession,
    ror_window_seconds: float,
    ror_min_sample_seconds: float,
) -> None:
    """Write CSV rows with the planned roast log schema."""
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        for row in _csv_rows(
            session,
            ror_window_seconds=ror_window_seconds,
            ror_min_sample_seconds=ror_min_sample_seconds,
        ):
            writer.writerow(row)


def _write_summary_json(
    path: Path,
    *,
    session: RoastSession,
    roaster_driver: str,
    ror_window_seconds: float = 60.0,
    ror_min_sample_seconds: float = 10.0,
) -> None:
    """Write a session-level summary with the planned summary schema fields."""
    metrics = compute_roast_metrics(
        session,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )
    first_crack_model = _summary_first_crack_model(session)
    first_crack_detection = _summary_first_crack_detection(session)
    payload: dict[str, Any] = {
        "session_id": session.id,
        "active": session.active,
        "phase": session.phase,
        "started_at_utc": session.created_at_utc.isoformat(),
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
        "total_roast_seconds": metrics.roast_elapsed_seconds,
        "development_time_seconds": metrics.development_time_seconds,
        "development_time_percent": metrics.development_percent,
        "roaster_driver": roaster_driver,
        "first_crack_model": first_crack_model,
        "first_crack_detection": first_crack_detection,
        "metrics": {
            "roast_elapsed_seconds": metrics.roast_elapsed_seconds,
            "development_time_seconds": metrics.development_time_seconds,
            "development_percent": metrics.development_percent,
            "development_time_percent": metrics.development_percent,
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


def _summary_first_crack_model(session: RoastSession) -> dict[str, EventPayloadValue | None]:
    """Return first-crack model metadata for summary export."""
    payload = _first_crack_payload_from(session.event_timeline)
    return {
        "repo_id": _string_payload_value(payload.get("repo_id")),
        "revision": _string_payload_value(payload.get("revision")),
        "precision": _string_payload_value(payload.get("precision")),
        "onnx_model_filename": _string_payload_value(payload.get("onnx_model_filename")),
        "feature_extractor_filename": _string_payload_value(
            payload.get("feature_extractor_filename")
        ),
        "confidence": _number_payload_value(payload.get("confidence")),
        "confidence_threshold": _number_payload_value(payload.get("confidence_threshold")),
        "min_positive_windows": _integer_payload_value(payload.get("min_positive_windows")),
        "confirmation_window_seconds": _number_payload_value(
            payload.get("confirmation_window_seconds")
        ),
    }


def _summary_first_crack_detection(session: RoastSession) -> dict[str, EventPayloadValue]:
    """Return the first-crack inference picture for the summary export (#175).

    Unlike `first_crack_model` (which is empty when the model never confirmed),
    these aggregates are always present, so a miss is diagnosable: it records the
    max confidence ever seen, the total window count, the highest positive-window
    count reached, and whether the audio model confirmed first crack. A no-fire
    roast then reads as "peaked at 0.55, never reached the threshold."

    Args:
        session: Session snapshot to summarize.

    Returns:
        First-crack detection aggregates for the summary export.
    """
    return {
        "model_confirmed": session.fc_model_confirmed,
        "max_confidence": session.fc_max_confidence,
        "window_count": session.fc_window_count,
        "max_positive_window_count": session.fc_max_positive_window_count,
    }


def _string_payload_value(value: EventPayloadValue | None) -> str | None:
    """Return a payload value only when it is string metadata."""
    return value if isinstance(value, str) else None


def _number_payload_value(value: EventPayloadValue | None) -> float | int | None:
    """Return a payload value only when it is numeric metadata."""
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) else None


def _integer_payload_value(value: EventPayloadValue | None) -> int | None:
    """Return a payload value only when it is integer metadata."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _csv_rows(
    session: RoastSession,
    *,
    ror_window_seconds: float,
    ror_min_sample_seconds: float,
) -> list[dict[str, object]]:
    """Return chronologically ordered CSV rows for telemetry samples and events."""
    rows: list[dict[str, object]] = []
    sorted_events = sorted(session.event_timeline, key=_event_sort_key)
    telemetry_by_monotonic = list(session.telemetry_buffer)
    combined: list[tuple[float, int, RoastEvent | TelemetrySample]] = [
        (event.monotonic_seconds, 0, event) for event in sorted_events
    ]
    combined.extend((sample.monotonic_seconds, 1, sample) for sample in telemetry_by_monotonic)
    last_sample: TelemetrySample | None = None
    for _, _, item in sorted(combined, key=lambda row: (row[0], row[1])):
        if isinstance(item, TelemetrySample):
            last_sample = item
            rows.append(
                _csv_telemetry_row(
                    session,
                    item,
                    ror_window_seconds=ror_window_seconds,
                    ror_min_sample_seconds=ror_min_sample_seconds,
                )
            )
            continue
        event_sample = _latest_sample_before(
            session.telemetry_buffer,
            monotonic_seconds=item.monotonic_seconds,
        )
        rows.append(
            _csv_event_row(
                session,
                item,
                telemetry=event_sample or last_sample,
                ror_window_seconds=ror_window_seconds,
                ror_min_sample_seconds=ror_min_sample_seconds,
            )
        )
    return rows


def _csv_telemetry_row(
    session: RoastSession,
    sample: TelemetrySample,
    *,
    ror_window_seconds: float,
    ror_min_sample_seconds: float,
) -> dict[str, object]:
    """Return one CSV row for a retained telemetry sample."""
    scoped_events = _events_visible_at(session, monotonic_seconds=sample.monotonic_seconds)
    metrics = compute_roast_metrics(
        _session_view_at(
            session,
            monotonic_seconds=sample.monotonic_seconds,
            visible_events=scoped_events,
            current_sample=sample,
        ),
        monotonic_now=lambda: session.monotonic_start + sample.monotonic_seconds,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )
    fc_payload = _first_crack_payload_from(scoped_events)
    return {
        "timestamp_utc": sample.recorded_at_utc.isoformat(),
        "elapsed_seconds": _roast_elapsed_at(scoped_events, sample.monotonic_seconds),
        "phase": _phase_from(scoped_events),
        "bean_temp_c": sample.bean_temp_c,
        "env_temp_c": sample.env_temp_c,
        "heat_level_percent": sample.heat_level_percent,
        "fan_level_percent": sample.fan_level_percent,
        "cooling_on": sample.cooling_on,
        "event": None,
        "beans_added": _event_seen_in(scoped_events, "beans_added"),
        "first_crack_detected": _event_seen_in(scoped_events, "first_crack_detected"),
        "beans_dropped": _event_seen_in(scoped_events, "beans_dropped"),
        "development_time_percent": metrics.development_percent,
        "bean_ror_c_per_min": metrics.bean_ror_c_per_min,
        "env_ror_c_per_min": metrics.env_ror_c_per_min,
        "bean_delta_60s_c": metrics.bean_temp_delta_60s_c,
        "env_delta_60s_c": metrics.env_temp_delta_60s_c,
        "fc_model_repo": fc_payload.get("repo_id"),
        "fc_model_revision": fc_payload.get("revision"),
        "fc_model_precision": fc_payload.get("precision"),
    }


def _csv_event_row(
    session: RoastSession,
    event: RoastEvent,
    *,
    telemetry: TelemetrySample | None,
    ror_window_seconds: float,
    ror_min_sample_seconds: float,
) -> dict[str, object]:
    """Return one CSV row for a recorded session event."""
    scoped_events = _events_visible_at(
        session,
        monotonic_seconds=event.monotonic_seconds,
        current_event=event,
    )
    metrics = compute_roast_metrics(
        _session_view_at(
            session,
            monotonic_seconds=event.monotonic_seconds,
            visible_events=scoped_events,
            include_same_time_telemetry=False,
        ),
        monotonic_now=lambda: session.monotonic_start + event.monotonic_seconds,
        ror_window_seconds=ror_window_seconds,
        ror_min_sample_seconds=ror_min_sample_seconds,
    )
    fc_payload = _first_crack_payload_from(scoped_events)
    return {
        "timestamp_utc": event.recorded_at_utc.isoformat(),
        "elapsed_seconds": _roast_elapsed_at(scoped_events, event.monotonic_seconds),
        "phase": _phase_from(scoped_events),
        "bean_temp_c": None if telemetry is None else telemetry.bean_temp_c,
        "env_temp_c": None if telemetry is None else telemetry.env_temp_c,
        "heat_level_percent": _event_control_value(
            event,
            telemetry=telemetry,
            key="heat_level_percent",
        ),
        "fan_level_percent": _event_control_value(
            event,
            telemetry=telemetry,
            key="fan_level_percent",
        ),
        "cooling_on": _event_cooling_value(event, telemetry=telemetry),
        "event": event.kind,
        "beans_added": _event_seen_in(scoped_events, "beans_added"),
        "first_crack_detected": _event_seen_in(scoped_events, "first_crack_detected"),
        "beans_dropped": _event_seen_in(scoped_events, "beans_dropped"),
        "development_time_percent": metrics.development_percent,
        "bean_ror_c_per_min": metrics.bean_ror_c_per_min,
        "env_ror_c_per_min": metrics.env_ror_c_per_min,
        "bean_delta_60s_c": metrics.bean_temp_delta_60s_c,
        "env_delta_60s_c": metrics.env_temp_delta_60s_c,
        "fc_model_repo": fc_payload.get("repo_id"),
        "fc_model_revision": fc_payload.get("revision"),
        "fc_model_precision": fc_payload.get("precision"),
    }


def _session_view_at(
    session: RoastSession,
    *,
    monotonic_seconds: float,
    visible_events: list[RoastEvent],
    include_same_time_telemetry: bool = True,
    current_sample: TelemetrySample | None = None,
) -> RoastSession:
    """Return a point-in-time session view for metric computation."""
    view = RoastSession(
        id=session.id,
        created_at_utc=session.created_at_utc,
        monotonic_start=session.monotonic_start,
        event_timeline=list(visible_events),
        telemetry_buffer=_telemetry_visible_at(
            session.telemetry_buffer,
            monotonic_seconds=monotonic_seconds,
            include_same_time=include_same_time_telemetry,
            current_sample=current_sample,
        ),
        log_writer=session.log_writer,
        stopped_at_utc=session.created_at_utc,
        monotonic_stop=session.monotonic_start + monotonic_seconds,
    )
    for event in visible_events:
        _apply_view_event(view, event)
    view.phase = _phase_from(visible_events)
    return view


def _telemetry_visible_at(
    samples: deque[TelemetrySample],
    *,
    monotonic_seconds: float,
    include_same_time: bool,
    current_sample: TelemetrySample | None,
) -> deque[TelemetrySample]:
    """Return telemetry samples visible to one point-in-time row."""
    visible: deque[TelemetrySample] = deque()
    for sample in samples:
        if sample.monotonic_seconds < monotonic_seconds:
            visible.append(sample)
            continue
        if sample.monotonic_seconds > monotonic_seconds or not include_same_time:
            break
        visible.append(sample)
        if current_sample is not None and sample is current_sample:
            break
    return visible


def _apply_view_event(session: RoastSession, event: RoastEvent) -> None:
    """Apply one visible event's timestamp fields to a point-in-time view."""
    if event.kind == "beans_added":
        session.beans_added_at_utc = event.recorded_at_utc
        session.beans_added_monotonic_seconds = event.monotonic_seconds
    elif event.kind == "first_crack_detected":
        session.first_crack_at_utc = event.recorded_at_utc
        session.first_crack_monotonic_seconds = event.monotonic_seconds
    elif event.kind == "beans_dropped":
        session.beans_dropped_at_utc = event.recorded_at_utc
        session.beans_dropped_monotonic_seconds = event.monotonic_seconds
    elif event.kind == "cooling_started":
        session.cooling_started_at_utc = event.recorded_at_utc
        session.cooling_started_monotonic_seconds = event.monotonic_seconds
    elif event.kind == "cooling_stopped":
        session.cooling_stopped_at_utc = event.recorded_at_utc
        session.cooling_stopped_monotonic_seconds = event.monotonic_seconds
    elif event.kind == "fault":
        session.faulted_at_utc = event.recorded_at_utc
        session.faulted_monotonic_seconds = event.monotonic_seconds


def _phase_from(events: list[RoastEvent]) -> RoastPhase:
    """Return the lifecycle phase implied by visible events."""
    phase: RoastPhase = "pre_roast"
    for event in events:
        if event.kind == "beans_added":
            phase = "roasting"
        elif event.kind == "first_crack_detected":
            phase = "development"
        elif event.kind == "beans_dropped":
            phase = "dropped"
        elif event.kind == "cooling_started":
            phase = "cooling"
        elif (
            event.kind == "cooling_stopped"
            and phase == "fault"
            and event.payload.get("recovery_after_fault") is True
        ):
            phase = "fault"
        elif event.kind == "cooling_stopped":
            phase = "complete"
        elif event.kind == "fault":
            phase = "fault"
    return phase


def _roast_elapsed_at(events: list[RoastEvent], monotonic_seconds: float) -> float | None:
    """Return roast elapsed seconds at one row timestamp."""
    beans_added_seconds = _event_monotonic_seconds(events, "beans_added")
    if beans_added_seconds is None:
        return None
    if monotonic_seconds < beans_added_seconds:
        return None
    end_seconds = monotonic_seconds
    beans_dropped_seconds = _event_monotonic_seconds(events, "beans_dropped")
    if beans_dropped_seconds is not None and monotonic_seconds >= beans_dropped_seconds:
        end_seconds = beans_dropped_seconds
    return round(max(0.0, end_seconds - beans_added_seconds), 3)


def _event_seen_in(events: list[RoastEvent], kind: str) -> bool:
    """Return whether one event kind is visible for the current row."""
    return any(event.kind == kind for event in events)


def _first_crack_payload_from(events: list[RoastEvent]) -> dict[str, EventPayloadValue]:
    """Return first-crack metadata visible for the current row."""
    for event in events:
        if event.kind == "first_crack_detected":
            return dict(event.payload)
    return {}


def _events_visible_at(
    session: RoastSession,
    *,
    monotonic_seconds: float,
    current_event: RoastEvent | None = None,
) -> list[RoastEvent]:
    """Return events visible at one row without leaking later same-time events."""
    visible_events: list[RoastEvent] = []
    for event in sorted(session.event_timeline, key=_event_sort_key):
        if event.monotonic_seconds > monotonic_seconds:
            break
        if event.monotonic_seconds == monotonic_seconds and current_event is not None:
            visible_events.append(event)
            if event is current_event:
                break
            continue
        visible_events.append(event)
    return visible_events


def _event_monotonic_seconds(events: list[RoastEvent], kind: str) -> float | None:
    """Return the first visible monotonic timestamp for an event kind."""
    for event in events:
        if event.kind == kind:
            return event.monotonic_seconds
    return None


def _latest_sample_before(
    samples: deque[TelemetrySample],
    *,
    monotonic_seconds: float,
) -> TelemetrySample | None:
    """Return the latest telemetry sample strictly before one timestamp."""
    latest: TelemetrySample | None = None
    for sample in samples:
        if sample.monotonic_seconds >= monotonic_seconds:
            break
        latest = sample
    return latest


def _event_control_value(
    event: RoastEvent,
    *,
    telemetry: TelemetrySample | None,
    key: str,
) -> int | None:
    """Return an event row control value with transition-aware overrides."""
    value = event.payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if event.kind == "beans_dropped" and key == "heat_level_percent":
        return 0
    if telemetry is None:
        return None
    if key == "heat_level_percent":
        return telemetry.heat_level_percent
    return telemetry.fan_level_percent


def _event_cooling_value(event: RoastEvent, *, telemetry: TelemetrySample | None) -> bool | None:
    """Return an event row cooling state with transition-aware overrides."""
    value = event.payload.get("cooling_on")
    if isinstance(value, bool):
        return value
    if event.kind == "cooling_started":
        return True
    if event.kind == "cooling_stopped":
        return False
    return None if telemetry is None else telemetry.cooling_on


def _event_sort_key(event: RoastEvent) -> tuple[float, int]:
    """Return deterministic event ordering key for CSV export."""
    if event.kind == "cooling_stopped" and event.payload.get("recovery_after_fault") is True:
        return (event.monotonic_seconds, _event_order("fault") + 1)
    return (event.monotonic_seconds, _event_order(event.kind))


def _event_order(kind: str) -> int:
    """Return lifecycle ordering for same-timestamp event rows."""
    order_by_kind = {
        "beans_added": 0,
        "first_crack_detected": 1,
        "beans_dropped": 2,
        "cooling_started": 3,
        "cooling_stopped": 4,
        "fault": 5,
    }
    return order_by_kind.get(kind, 99)


def _iso_or_none(value: datetime | None) -> str | None:
    """Return ISO text when a timestamp exists."""
    return value.isoformat() if value is not None else None
