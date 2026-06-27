"""In-process MCP tool coverage for RoastPilot."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP

from coffee_roaster_mcp.drivers import EmergencyStopResult, MockRoasterDriver, RoasterState
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackRuntimeState,
)
from coffee_roaster_mcp.mcp_server import (
    SDK_REQUEST_LOGGER_NAME,
    ServerContext,
    build_server_context,
    create_mcp_server,
    quiet_sdk_per_request_log,
)
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore, SessionLifecycleError


def test_sdk_request_logger_name_matches_installed_sdk() -> None:
    """The pinned SDK logger name must match the SDK we suppress, or the guard misses."""
    import mcp.server.lowlevel.server as sdk_server

    assert sdk_server.logger.name == SDK_REQUEST_LOGGER_NAME


def test_quiet_sdk_per_request_log_suppresses_info_keeps_warning() -> None:
    """Quieting raises the SDK per-request logger to WARNING without touching others."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    project_logger = logging.getLogger("coffee_roaster_mcp.audio")
    original_sdk_level = sdk_logger.level
    original_project_level = project_logger.level
    sdk_logger.setLevel(logging.INFO)
    project_logger.setLevel(logging.INFO)
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.WARNING
        assert sdk_logger.isEnabledFor(logging.WARNING)
        assert not sdk_logger.isEnabledFor(logging.INFO)
        # The project's own INFO logging (e.g. mic-overflow recovery) is untouched.
        assert project_logger.level == logging.INFO
    finally:
        sdk_logger.setLevel(original_sdk_level)
        project_logger.setLevel(original_project_level)


def test_quiet_sdk_per_request_log_only_raises_never_lowers() -> None:
    """A stricter user/config level (e.g. ERROR) is preserved, not trampled to WARNING."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    original_sdk_level = sdk_logger.level
    sdk_logger.setLevel(logging.ERROR)
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.ERROR
    finally:
        sdk_logger.setLevel(original_sdk_level)


def test_quiet_sdk_per_request_log_pins_warning_before_logging_configured() -> None:
    """Regression (#162): the production order is quiet() THEN the SDK's .run() configures
    INFO. At the quiet call the SDK logger is NOTSET (no explicit level), so the guard must
    key off .level (NOTSET → pin WARNING), not getEffectiveLevel() (the inherited default
    WARNING, which made the old guard skip — then INFO was configured and it flooded)."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    root = logging.getLogger()
    original_sdk_level = sdk_logger.level
    original_root_level = root.level
    sdk_logger.setLevel(logging.NOTSET)  # the real startup state — no explicit level
    root.setLevel(logging.WARNING)  # nothing has configured INFO yet
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.WARNING  # explicit, set despite inherited WARNING
        # the SDK's .run() configures INFO on the root afterwards; the explicit WARNING wins.
        root.setLevel(logging.INFO)
        assert sdk_logger.getEffectiveLevel() == logging.WARNING
        assert not sdk_logger.isEnabledFor(logging.INFO)
    finally:
        sdk_logger.setLevel(original_sdk_level)
        root.setLevel(original_root_level)


def test_in_process_mcp_tools_cover_mock_roast_and_export(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    server_info = _call_tool(server, "get_server_info", ctx)
    assert server_info.bootstrap_safe is True
    assert "export_roast_log" in server_info.available_bootstrap_tools

    runtime_config = _call_tool(server, "get_runtime_config", ctx)
    assert runtime_config.config_source == str(config_path)
    assert runtime_config.first_crack_mode == "disabled"

    start_result = _call_tool(server, "start_roast_session", ctx)
    session_id = start_result.session.session_id
    assert start_result.session.phase == "pre_roast"
    assert start_result.session.log_dir is not None

    heat_result = _call_tool(server, "set_heat", ctx, heat_level_percent=70)
    fan_result = _call_tool(server, "set_fan", ctx, fan_level_percent=40)
    assert heat_result.heat_level_percent == 70
    assert fan_result.fan_level_percent == 40

    beans_added = _call_tool(server, "mark_beans_added", ctx)
    first_crack = _call_tool(server, "mark_first_crack", ctx)
    drop = _call_tool(server, "drop_beans", ctx)
    cooling = _call_tool(server, "start_cooling", ctx)
    complete = _call_tool(server, "stop_cooling", ctx)
    assert beans_added.event.kind == "beans_added"
    assert first_crack.event.kind == "first_crack_detected"
    assert drop.phase == "cooling"
    assert cooling.phase == "cooling"
    assert complete.phase == "complete"

    state = _call_tool(server, "get_roast_state", ctx, session_id=session_id)
    assert state.session_id == session_id
    assert state.active is False
    assert state.phase == "complete"
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]
    assert state.first_crack_at_utc is not None
    assert state.development_time_seconds is not None

    export = _call_tool(server, "export_roast_log", ctx, session_id=session_id)
    assert export.ready is True
    assert export.session_id == session_id
    assert Path(export.jsonl_path).exists()
    assert Path(export.csv_path).exists()
    assert Path(export.summary_path).exists()

    rows = [json.loads(line) for line in Path(export.jsonl_path).read_text().splitlines()]
    events = [row for row in rows if row["type"] == "event"]
    assert [event["kind"] for event in events] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]


def test_set_recording_metadata_tool_stores_and_echoes(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    # The tool is registered and listed as a bootstrap-safe tool.
    server_info = _call_tool(server, "get_server_info", ctx)
    assert "set_recording_metadata" in server_info.available_bootstrap_tools

    result = _call_tool(server, "set_recording_metadata", ctx, origin="brazil", roast_num=7)
    assert result.origin == "brazil"
    assert result.roast_num == 7

    # The metadata reached the runtime, so the recorder it builds for a roast
    # names the WAV for the annotation pipeline.
    from dataclasses import replace

    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    recording_config = replace(
        server_context.config,
        recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
    )
    session = server_context.session_store.start_session()
    metadata = server_context.first_crack_runtime.set_recording_metadata(
        origin="brazil", roast_num=7
    )
    recorder = build_session_recorder(recording_config, session, metadata=metadata)
    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path.name == "mic1-brazil-roast7.wav"

    # Invalid input is rejected.
    with pytest.raises(ValueError, match="origin must not be blank"):
        _call_tool(server, "set_recording_metadata", ctx, origin="  ", roast_num=1)


def test_in_process_mcp_tools_surface_errors_and_audio_bootstrap_state(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                "  allow_manual_override: false",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(server_context, FakeFirstCrackRuntime())
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    server_info = _call_tool(server, "get_server_info", ctx)
    runtime_config = _call_tool(server, "get_runtime_config", ctx)
    assert server_info.first_crack_mode == "audio"
    assert server_info.bootstrap_safe is False
    assert runtime_config.allow_manual_override is False

    with pytest.raises(ValueError, match="No active roast session"):
        _call_tool(server, "set_heat", ctx, heat_level_percent=10)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    with pytest.raises(ValueError, match="Manual first-crack override is disabled"):
        _call_tool(server, "mark_first_crack", ctx)

    with pytest.raises(ValueError, match="Unknown session_id"):
        _call_tool(server, "get_roast_state", ctx, session_id="missing-session")


def test_mcp_roast_controls_call_configured_driver_boundary(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    heat = _call_tool(server, "set_heat", ctx, heat_level_percent=65)
    fan = _call_tool(server, "set_fan", ctx, fan_level_percent=45)
    _call_tool(server, "mark_beans_added", ctx)
    drop = _call_tool(server, "drop_beans", ctx)
    repeated_drop = _call_tool(server, "drop_beans", ctx)
    cooling = _call_tool(server, "start_cooling", ctx)
    complete = _call_tool(server, "stop_cooling", ctx)

    assert driver.actions == [
        "connect",
        "set_heat:65",
        "set_fan:45",
        "drop_beans",
        "stop_cooling",
    ]
    assert heat.heat_level_percent == 65
    assert fan.fan_level_percent == 45
    assert drop.event.kind == "beans_dropped"
    assert drop.phase == "cooling"
    assert repeated_drop.event.kind == "beans_dropped"
    assert repeated_drop.phase == "cooling"
    assert cooling.event.kind == "cooling_started"
    assert complete.phase == "complete"


def test_get_roast_state_exposes_current_driver_state_and_event_timestamps(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(
        bean_temp_c=151.25,
        env_temp_c=204.5,
        raw_vendor_data={"status_packet_count": 7, "vendor_note": "ready"},
    )
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "set_heat", ctx, heat_level_percent=55)
    _call_tool(server, "set_fan", ctx, fan_level_percent=35)
    beans_added = _call_tool(server, "mark_beans_added", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    assert state.device_state is not None
    assert state.device_state.driver == "recording"
    assert state.device_state.connected is True
    assert state.device_state.bean_temp_c == 151.25
    assert state.device_state.env_temp_c == 204.5
    assert state.device_state.heat_level_percent == 55
    assert state.device_state.fan_level_percent == 35
    assert state.device_state.cooling_on is False
    assert state.device_state.raw_vendor_data == {
        "status_packet_count": 7,
        "vendor_note": "ready",
    }
    assert state.beans_added_at_utc == beans_added.event.recorded_at_utc
    assert state.beans_added_monotonic_seconds == beans_added.event.monotonic_seconds
    assert state.first_crack_status.status == "disabled"
    assert state.first_crack_status.mode == "disabled"
    assert state.first_crack_status.detected_at_utc is None
    assert state.first_crack_status.detected_monotonic_seconds is None
    assert state.t0_status.status == "detected"
    assert state.t0_status.auto_detection_enabled is False


def test_get_roast_state_appends_normalized_driver_telemetry_samples(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=151.25, env_temp_c=204.5)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    driver.bean_temp_c = 152.0
    driver.env_temp_c = 205.25
    driver.heat_level_percent = 45
    driver.fan_level_percent = 25
    driver.cooling_on = True
    _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    session = server_context.session_store.get_session_snapshot(
        session_id=start_result.session.session_id
    )
    samples = list(session.telemetry_buffer)
    assert len(samples) == 2
    assert [sample.bean_temp_c for sample in samples] == [151.25, 152.0]
    assert [sample.env_temp_c for sample in samples] == [204.5, 205.25]
    assert samples[1].heat_level_percent == 45
    assert samples[1].fan_level_percent == 25
    assert samples[1].cooling_on is True
    assert samples[0].monotonic_seconds <= samples[1].monotonic_seconds


def test_get_roast_state_driver_read_failure_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)

    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    driver.fail_read = False
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert [event.kind for event in state.events] == ["beans_added"]
    assert state.phase == "roasting"


def test_get_roast_state_auto_t0_driver_read_failure_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True, bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    driver.fail_read = False
    driver.bean_temp_c = 145.0
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "pre_roast"
    assert state.events == ()
    assert state.t0_status.status == "pending"
    assert state.t0_status.charge_temperature_c == 145.0
    assert state.t0_status.current_drop_c == 0.0


def test_get_roast_state_records_automatic_t0_after_configured_drop(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    first_state = _call_tool(server, "get_roast_state", ctx)
    assert first_state.phase == "pre_roast"
    assert first_state.beans_added_at_utc is None
    assert first_state.t0_status.status == "pending"
    assert first_state.t0_status.charge_temperature_c == 170.0
    assert first_state.t0_status.current_drop_c == 0.0

    driver.bean_temp_c = 145.0
    threshold_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )
    assert threshold_state.phase == "roasting"
    assert threshold_state.beans_added_at_utc is not None
    assert threshold_state.t0_status.status == "detected"
    assert threshold_state.t0_status.auto_detection_enabled is True
    assert threshold_state.t0_status.charge_temperature_c == 170.0
    assert threshold_state.t0_status.current_drop_c == 25.0
    assert threshold_state.t0_status.drop_threshold_c == 25.0
    assert threshold_state.t0_status.detected_bean_temperature_c == 145.0
    assert [event.kind for event in threshold_state.events] == ["beans_added"]
    t0_payload = threshold_state.events[0].payload
    assert t0_payload["source"] == "auto_t0"
    assert t0_payload["charge_temperature_c"] == 170.0
    assert t0_payload["detected_bean_temperature_c"] == 145.0
    assert t0_payload["drop_c"] == 25.0
    assert t0_payload["drop_threshold_c"] == 25.0
    # T0 is backdated to the candidate turning point (the 170 °C local max),
    # while the raw confirmation timestamp stays available (#167).
    assert "turning_point_monotonic_seconds" in t0_payload
    assert "confirmed_at_monotonic_seconds" in t0_payload
    assert "confirmed_at_utc" in t0_payload
    assert cast(float, t0_payload["turning_point_monotonic_seconds"]) <= cast(
        float, t0_payload["confirmed_at_monotonic_seconds"]
    )


def test_get_roast_state_discards_queued_first_crack_windows_after_auto_t0(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    runtime = FakeFirstCrackRuntime(record_first_crack_on_process=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    _set_first_crack_runtime(server_context, runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 145.0
    state = _call_tool(server, "get_roast_state", ctx)

    assert state.phase == "development"
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
    ]
    assert runtime.discarded_sessions == [state.session_id]
    assert runtime.processed_sessions == [state.session_id, state.session_id]


def test_get_roast_state_auto_t0_uses_max_preheat_and_ignores_small_drops(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 30",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=160.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 175.0
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 150.1
    small_drop_state = _call_tool(server, "get_roast_state", ctx)
    assert small_drop_state.phase == "pre_roast"
    assert small_drop_state.t0_status.status == "pending"
    assert small_drop_state.t0_status.charge_temperature_c == 175.0
    assert small_drop_state.t0_status.current_drop_c is not None
    assert abs(small_drop_state.t0_status.current_drop_c - 24.9) < 0.000001

    driver.bean_temp_c = 144.9
    detected_state = _call_tool(server, "get_roast_state", ctx)
    assert detected_state.phase == "roasting"
    assert detected_state.t0_status.status == "detected"
    assert detected_state.t0_status.charge_temperature_c == 175.0
    assert detected_state.t0_status.current_drop_c is not None
    assert abs(detected_state.t0_status.current_drop_c - 30.1) < 0.000001


def test_get_roast_state_auto_t0_ignores_disconnected_driver_readings(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.connected = False
    driver.bean_temp_c = 140.0
    disconnected_state = _call_tool(server, "get_roast_state", ctx)

    assert disconnected_state.phase == "pre_roast"
    assert disconnected_state.events == ()
    assert disconnected_state.device_state is not None
    assert disconnected_state.device_state.connected is False
    assert disconnected_state.t0_status.status == "pending"
    assert disconnected_state.t0_status.charge_temperature_c == 170.0
    assert disconnected_state.t0_status.current_drop_c == 0.0


def test_get_roast_state_auto_t0_pending_drop_does_not_round_to_threshold(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 145.0004
    state = _call_tool(server, "get_roast_state", ctx)

    assert state.phase == "pre_roast"
    assert state.t0_status.status == "pending"
    assert state.t0_status.current_drop_c is not None
    assert abs(state.t0_status.current_drop_c - 24.9996) < 0.000001
    assert state.t0_status.current_drop_c < state.t0_status.drop_threshold_c


def test_get_roast_state_auto_t0_waits_for_valid_baseline(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=None)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    no_temp_state = _call_tool(server, "get_roast_state", ctx)
    assert no_temp_state.phase == "pre_roast"
    assert no_temp_state.t0_status.status == "pending"
    assert no_temp_state.t0_status.charge_temperature_c is None

    driver.bean_temp_c = 125.0
    first_temp_state = _call_tool(server, "get_roast_state", ctx)
    assert first_temp_state.phase == "pre_roast"
    assert first_temp_state.t0_status.status == "pending"
    assert first_temp_state.t0_status.charge_temperature_c == 125.0
    assert first_temp_state.t0_status.current_drop_c == 0.0


def test_get_roast_state_reads_driver_before_detector_side_effects(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True)
    runtime = FakeFirstCrackRuntime(record_first_crack_on_process=False)
    object.__setattr__(server_context, "roaster_driver", driver)
    _set_first_crack_runtime(server_context, runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    runtime.record_first_crack_on_process = True

    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    failed_read_snapshot = server_context.session_store.get_session_snapshot(
        session_id=start_result.session.session_id
    )
    assert failed_read_snapshot.first_crack_at_utc is None
    assert runtime.processed_sessions == [start_result.session.session_id]

    driver.fail_read = False
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    assert state.phase == "development"
    assert state.first_crack_at_utc is not None
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
    ]


def test_mark_beans_added_returns_snapshot_after_immediate_detector_confirmation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(
        server_context,
        FakeFirstCrackRuntime(record_first_crack_on_process=True),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    beans_added = _call_tool(server, "mark_beans_added", ctx)

    assert beans_added.event.kind == "beans_added"
    assert beans_added.phase == "development"
    assert beans_added.event_count == 2


def test_get_roast_state_exposes_first_crack_statuses(tmp_path: Path) -> None:
    manual_config_path = tmp_path / "manual.yaml"
    manual_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: manual",
                f"logging:\n  log_dir: {tmp_path / 'manual-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    manual_context = build_server_context(config_path=manual_config_path)
    manual_server = create_mcp_server(config_path=manual_config_path)
    manual_ctx = _ctx(manual_context)
    manual_start = _call_tool(manual_server, "start_roast_session", manual_ctx)
    manual_state = _call_tool(
        manual_server,
        "get_roast_state",
        manual_ctx,
        session_id=manual_start.session.session_id,
    )
    assert manual_state.first_crack_status.status == "manual"
    assert manual_state.first_crack_status.allow_manual_override is True

    manual_unavailable_config_path = tmp_path / "manual-unavailable.yaml"
    manual_unavailable_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: manual",
                "  allow_manual_override: false",
                f"logging:\n  log_dir: {tmp_path / 'manual-unavailable-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    manual_unavailable_context = build_server_context(config_path=manual_unavailable_config_path)
    manual_unavailable_server = create_mcp_server(config_path=manual_unavailable_config_path)
    manual_unavailable_ctx = _ctx(manual_unavailable_context)
    manual_unavailable_start = _call_tool(
        manual_unavailable_server,
        "start_roast_session",
        manual_unavailable_ctx,
    )
    manual_unavailable_state = _call_tool(
        manual_unavailable_server,
        "get_roast_state",
        manual_unavailable_ctx,
        session_id=manual_unavailable_start.session.session_id,
    )
    assert manual_unavailable_state.first_crack_status.status == "unavailable"
    assert manual_unavailable_state.first_crack_status.allow_manual_override is False
    assert (
        manual_unavailable_state.first_crack_status.reason
        == "Manual first-crack mode is configured, but manual override is disabled."
    )

    audio_config_path = tmp_path / "audio.yaml"
    audio_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    audio_context = build_server_context(config_path=audio_config_path)
    audio_runtime = FakeFirstCrackRuntime()
    _set_first_crack_runtime(audio_context, audio_runtime)
    audio_server = create_mcp_server(config_path=audio_config_path)
    audio_ctx = _ctx(audio_context)
    audio_start = _call_tool(audio_server, "start_roast_session", audio_ctx)
    audio_state = _call_tool(
        audio_server,
        "get_roast_state",
        audio_ctx,
        session_id=audio_start.session.session_id,
    )
    assert audio_state.first_crack_status.status == "pending"

    _call_tool(audio_server, "mark_beans_added", audio_ctx)
    detected = _call_tool(audio_server, "mark_first_crack", audio_ctx)
    detected_state = _call_tool(
        audio_server,
        "get_roast_state",
        audio_ctx,
        session_id=audio_start.session.session_id,
    )
    assert detected_state.first_crack_status.status == "detected"
    assert detected_state.first_crack_status.detected_at_utc == detected.event.recorded_at_utc
    assert (
        detected_state.first_crack_status.detected_monotonic_seconds
        == detected.event.monotonic_seconds
    )
    assert audio_runtime.stopped_sessions == [audio_start.session.session_id]

    audio_unavailable_config_path = tmp_path / "audio-unavailable.yaml"
    audio_unavailable_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-unavailable-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    audio_unavailable_context = build_server_context(config_path=audio_unavailable_config_path)
    _set_first_crack_runtime(
        audio_unavailable_context,
        FakeFirstCrackRuntime(status="unavailable", reason="missing detector artifacts"),
    )
    audio_unavailable_server = create_mcp_server(config_path=audio_unavailable_config_path)
    audio_unavailable_ctx = _ctx(audio_unavailable_context)
    audio_unavailable_start = _call_tool(
        audio_unavailable_server,
        "start_roast_session",
        audio_unavailable_ctx,
    )
    audio_unavailable_state = _call_tool(
        audio_unavailable_server,
        "get_roast_state",
        audio_unavailable_ctx,
        session_id=audio_unavailable_start.session.session_id,
    )
    assert audio_unavailable_state.first_crack_status.status == "unavailable"
    assert audio_unavailable_state.first_crack_status.reason == "missing detector artifacts"

    fault_config_path = tmp_path / "fault.yaml"
    fault_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'fault-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    fault_context = build_server_context(config_path=fault_config_path)
    _set_first_crack_runtime(fault_context, FakeFirstCrackRuntime())
    fault_server = create_mcp_server(config_path=fault_config_path)
    fault_ctx = _ctx(fault_context)
    fault_start = _call_tool(fault_server, "start_roast_session", fault_ctx)
    _call_tool(fault_server, "emergency_stop", fault_ctx, reason="unit-test")
    fault_state = _call_tool(
        fault_server,
        "get_roast_state",
        fault_ctx,
        session_id=fault_start.session.session_id,
    )
    assert fault_state.first_crack_status.status == "faulted"


def test_get_roast_state_scopes_runtime_metrics_to_requested_session(tmp_path: Path) -> None:
    config_path = tmp_path / "audio.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(
        server_context,
        FakeFirstCrackRuntime(
            audio_running=True,
            queued_window_count=2,
            emitted_window_count=3,
            dropped_window_count=4,
            processed_window_count=5,
        ),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    first_start = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "mark_first_crack", ctx)
    _call_tool(server, "drop_beans", ctx)
    _call_tool(server, "start_cooling", ctx)
    _call_tool(server, "stop_cooling", ctx)
    second_start = _call_tool(server, "start_roast_session", ctx)
    first_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=first_start.session.session_id,
    )

    assert first_state.first_crack_status.status == "detected"
    assert first_state.first_crack_status.audio_running is False
    assert first_state.first_crack_status.queued_window_count == 0
    assert first_state.first_crack_status.emitted_window_count == 0
    assert first_state.first_crack_status.dropped_window_count == 0
    assert first_state.first_crack_status.processed_window_count == 0

    second_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=second_start.session.session_id,
    )
    assert second_state.first_crack_status.status == "pending"
    assert second_state.first_crack_status.audio_running is True
    assert second_state.first_crack_status.queued_window_count == 2
    assert second_state.first_crack_status.emitted_window_count == 3
    assert second_state.first_crack_status.dropped_window_count == 4
    assert second_state.first_crack_status.processed_window_count == 5


def test_driver_command_failure_does_not_mutate_session_state(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_heat=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(RuntimeError, match="heat command failed"):
        _call_tool(server, "set_heat", ctx, heat_level_percent=65)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert driver.actions == ["connect", "set_heat:65"]
    assert state.heat_level_percent == 0
    assert state.fan_level_percent == 0
    assert state.cooling_on is False
    assert state.events == ()


def test_invalid_event_phase_blocks_driver_command(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(SessionLifecycleError, match="roasting, development"):
        _call_tool(server, "drop_beans", ctx)
    with pytest.raises(
        SessionLifecycleError, match="Cooling can only start after beans are dropped"
    ):
        _call_tool(server, "start_cooling", ctx)
    with pytest.raises(SessionLifecycleError, match="Cooling cannot stop before beans are dropped"):
        _call_tool(server, "stop_cooling", ctx)

    assert driver.actions == ["connect"]


def test_driver_connect_failure_prevents_session_creation(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    object.__setattr__(server_context, "roaster_driver", RecordingRoasterDriver(fail_connect=True))
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    with pytest.raises(RuntimeError, match="connect failed"):
        _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(ValueError, match="No roast session exists"):
        _call_tool(server, "get_roast_state", ctx)


def test_concurrent_session_start_reserves_before_driver_connect(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    connect_started = Event()
    release_connect = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_connect=(connect_started, release_connect))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    results: list[object] = []
    errors: list[BaseException] = []

    start_thread = Thread(
        target=_record_tool_result,
        args=(results, errors, server, "start_roast_session", ctx),
    )
    start_thread.start()
    assert connect_started.wait(timeout=1.0)

    with pytest.raises(SessionLifecycleError, match="start is already in progress"):
        _call_tool(server, "start_roast_session", ctx)

    release_connect.set()
    start_thread.join(timeout=1.0)

    assert not start_thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert driver.actions == ["connect"]


def test_stale_heat_command_fails_closed_after_emergency_stop(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_heat=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    _call_tool(server, "start_roast_session", ctx)
    heat_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "set_heat", ctx),
        kwargs={"heat_level_percent": 65},
    )
    heat_thread.start()
    assert command_started.wait(timeout=1.0)

    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    release_command.set()
    heat_thread.join(timeout=1.0)

    assert not heat_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    assert emergency.event.kind == "fault"
    assert driver.heat_level_percent == 0
    assert driver.fan_level_percent == 100
    assert driver.cooling_on is True
    assert driver.actions == [
        "connect",
        "set_heat:65",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_pending_post_fault_cooling_blocks_newer_active_session(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_heat=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    first_start = _call_tool(server, "start_roast_session", ctx)
    heat_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "set_heat", ctx),
        kwargs={"heat_level_percent": 65},
    )
    heat_thread.start()
    assert command_started.wait(timeout=1.0)

    _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    with pytest.raises(SessionLifecycleError, match="post-fault cooling recovery"):
        _call_tool(server, "start_roast_session", ctx)
    release_command.set()
    heat_thread.join(timeout=1.0)

    assert not heat_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=first_start.session.session_id,
    )
    assert state.active is False
    assert state.phase == "fault"
    assert state.cooling_on is True
    assert driver.actions == [
        "connect",
        "set_heat:65",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_blocked_drop_command_does_not_block_emergency_stop(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_drop=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    drop_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "drop_beans", ctx),
    )
    drop_thread.start()
    assert command_started.wait(timeout=1.0)

    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    release_command.set()
    drop_thread.join(timeout=1.0)

    assert not drop_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    assert emergency.event.kind == "fault"
    assert driver.actions == [
        "connect",
        "drop_beans",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_uses_driver_cooling_state_before_completing(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_stays_on=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "drop_beans", ctx)

    with pytest.raises(SessionLifecycleError, match="still reports cooling active"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.active is True
    assert state.phase == "cooling"
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == [
        "beans_added",
        "beans_dropped",
        "cooling_started",
    ]
    assert driver.actions == [
        "connect",
        "drop_beans",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_recovers_after_emergency_stop_leaves_cooling_on(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    assert emergency.phase == "fault"
    emergency_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )
    assert emergency_state.active is False
    assert emergency_state.cooling_on is True

    recovered = _call_tool(server, "stop_cooling", ctx)

    assert recovered.session_id == start_result.session.session_id
    assert recovered.event.kind == "cooling_stopped"
    assert recovered.event.payload["recovery_after_fault"] is True
    assert recovered.phase == "fault"
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.cooling_on is False
    assert [event.kind for event in state.events] == ["fault", "cooling_stopped"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
    ]


def test_stop_cooling_recovery_keeps_fault_when_driver_reports_cooling_on(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_stays_on=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")

    with pytest.raises(SessionLifecycleError, match="still reports cooling active"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.active is False
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == ["fault"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_recovery_rejects_driver_heat_after_fault(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_heat_level_percent=20)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")

    with pytest.raises(SessionLifecycleError, match="Heat must be off"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.active is False
    assert state.heat_level_percent == 0
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == ["fault"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stale_stop_cooling_recovery_fails_closed(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_stop_cooling=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    stop_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "stop_cooling", ctx),
    )
    stop_thread.start()
    assert command_started.wait(timeout=1.0)

    latest_session = server_context.session_store.get_latest_session()
    assert latest_session is not None
    server_context.session_store.cancel_pending_driver_command(latest_session)
    release_command.set()
    stop_thread.join(timeout=1.0)

    assert not stop_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.cooling_on is True
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_still_rejects_completed_inactive_session(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "drop_beans", ctx)
    _call_tool(server, "stop_cooling", ctx)

    with pytest.raises(ValueError, match="No active roast session exists"):
        _call_tool(server, "stop_cooling", ctx)


class RecordingRoasterDriver:
    """Driver double that records MCP boundary calls."""

    name = "recording"

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        fail_heat: bool = False,
        fail_read: bool = False,
        block_connect: tuple[Event, Event] | None = None,
        block_heat: tuple[Event, Event] | None = None,
        block_drop: tuple[Event, Event] | None = None,
        block_stop_cooling: tuple[Event, Event] | None = None,
        stop_cooling_stays_on: bool = False,
        stop_cooling_heat_level_percent: int = 0,
        bean_temp_c: float | None = None,
        env_temp_c: float | None = None,
        raw_vendor_data: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        """Initialize a deterministic recording driver."""
        self.actions: list[str] = []
        self.fail_connect = fail_connect
        self.fail_heat = fail_heat
        self.fail_read = fail_read
        self.block_connect = block_connect
        self.block_heat = block_heat
        self.block_drop = block_drop
        self.block_stop_cooling = block_stop_cooling
        self.stop_cooling_stays_on = stop_cooling_stays_on
        self.stop_cooling_heat_level_percent = stop_cooling_heat_level_percent
        self.connected = False
        self.heat_level_percent = 0
        self.fan_level_percent = 0
        self.cooling_on = False
        self.bean_temp_c = bean_temp_c
        self.env_temp_c = env_temp_c
        self.raw_vendor_data = {} if raw_vendor_data is None else dict(raw_vendor_data)

    @property
    def capabilities(self) -> object:
        """Return mock-compatible capabilities for tests."""
        return MockRoasterDriver().capabilities

    def connect(self) -> None:
        """Record connect calls."""
        self.actions.append("connect")
        if self.block_connect is not None:
            started, release = self.block_connect
            started.set()
            assert release.wait(timeout=1.0)
        if self.fail_connect:
            raise RuntimeError("connect failed")
        self.connected = True

    def disconnect(self) -> None:
        """Record disconnect calls."""
        self.actions.append("disconnect")
        self.connected = False

    def read_state(self) -> RoasterState:
        """Return the current test state."""
        if self.fail_read:
            raise RuntimeError("read failed")
        return self._state()

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Record heat commands."""
        self.actions.append(f"set_heat:{heat_level_percent}")
        if self.fail_heat:
            raise RuntimeError("heat command failed")
        if self.block_heat is not None:
            started, release = self.block_heat
            started.set()
            assert release.wait(timeout=1.0)
        self.heat_level_percent = heat_level_percent
        return self._state()

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Record fan commands."""
        self.actions.append(f"set_fan:{fan_level_percent}")
        self.fan_level_percent = fan_level_percent
        return self._state()

    def drop_beans(self) -> RoasterState:
        """Record drop commands and enter cooling."""
        self.actions.append("drop_beans")
        if self.block_drop is not None:
            started, release = self.block_drop
            started.set()
            assert release.wait(timeout=1.0)
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        return self._state()

    def start_cooling(self) -> RoasterState:
        """Record cooling-start commands."""
        self.actions.append("start_cooling")
        self.cooling_on = True
        return self._state()

    def stop_cooling(self) -> RoasterState:
        """Record cooling-stop commands."""
        self.actions.append("stop_cooling")
        if self.block_stop_cooling is not None:
            started, release = self.block_stop_cooling
            started.set()
            assert release.wait(timeout=1.0)
        self.cooling_on = self.stop_cooling_stays_on
        self.heat_level_percent = self.stop_cooling_heat_level_percent
        return self._state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Record emergency-stop commands."""
        self.actions.append(f"emergency_stop:{reason}")
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
        )

    def _state(self) -> RoasterState:
        return RoasterState(
            driver=self.name,
            connected=self.connected,
            bean_temp_c=self.bean_temp_c,
            env_temp_c=self.env_temp_c,
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
            raw_vendor_data=self.raw_vendor_data,
        )


class FakeFirstCrackRuntime:
    """Runtime double that keeps audio-mode MCP tests network-free."""

    def __init__(
        self,
        *,
        status: str = "pending",
        reason: str | None = None,
        record_first_crack_on_process: bool = False,
        audio_running: bool = False,
        queued_window_count: int = 0,
        emitted_window_count: int = 0,
        dropped_window_count: int = 0,
        processed_window_count: int = 0,
    ) -> None:
        self.status = status
        self.reason = reason
        self.record_first_crack_on_process = record_first_crack_on_process
        self.audio_running = audio_running
        self.queued_window_count = queued_window_count
        self.emitted_window_count = emitted_window_count
        self.dropped_window_count = dropped_window_count
        self.processed_window_count = processed_window_count
        self.active_session_id: str | None = None
        self.started_sessions: list[str] = []
        self.processed_sessions: list[str] = []
        self.stopped_sessions: list[str] = []
        self.discarded_sessions: list[str] = []

    def start_for_session(self, session: RoastSession) -> FirstCrackRuntimeSnapshot:
        self.active_session_id = session.id
        self.started_sessions.append(session.id)
        return self.snapshot()

    def process_available_windows(
        self,
        *,
        session_store: RoastSessionStore,
        session: RoastSession,
    ) -> FirstCrackRuntimeSnapshot:
        self.processed_sessions.append(session.id)
        if (
            self.record_first_crack_on_process
            and session.phase == "roasting"
            and session.first_crack_at_utc is None
        ):
            session_store.record_event_snapshot(session, "first_crack_detected")
            self.status = "detected"
        return self.snapshot()

    def stop_for_session(self, session_id: str, *, reason: str) -> FirstCrackRuntimeSnapshot:
        self.stopped_sessions.append(session_id)
        self.reason = reason
        return self.snapshot()

    def discard_queued_windows_for_session(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> FirstCrackRuntimeSnapshot:
        self.discarded_sessions.append(session_id)
        self.reason = reason
        return self.snapshot()

    def shutdown(self) -> FirstCrackRuntimeSnapshot:
        return self.snapshot()

    def snapshot(self) -> FirstCrackRuntimeSnapshot:
        return FirstCrackRuntimeSnapshot(
            status=cast(FirstCrackRuntimeState, self.status),
            active_session_id=self.active_session_id,
            active=self.active_session_id is not None,
            reason=self.reason,
            audio_running=self.audio_running,
            queued_window_count=self.queued_window_count,
            emitted_window_count=self.emitted_window_count,
            dropped_window_count=self.dropped_window_count,
            processed_window_count=self.processed_window_count,
        )


def _ctx(server_context: ServerContext) -> Any:
    """Build the minimal context shape used by FastMCP tool functions."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server_context))


def _set_first_crack_runtime(
    server_context: ServerContext,
    runtime: FakeFirstCrackRuntime,
) -> None:
    object.__setattr__(server_context, "first_crack_runtime", runtime)


def _record_tool_error(
    errors: list[BaseException],
    server: FastMCP,
    tool_name: str,
    ctx: Any,
    **kwargs: object,
) -> None:
    """Run one tool in a background thread and record any exception."""
    try:
        _call_tool(server, tool_name, ctx, **kwargs)
    except BaseException as exc:
        errors.append(exc)


def _record_tool_result(
    results: list[object],
    errors: list[BaseException],
    server: FastMCP,
    tool_name: str,
    ctx: Any,
    **kwargs: object,
) -> None:
    """Run one tool in a background thread and record its result or exception."""
    try:
        results.append(_call_tool(server, tool_name, ctx, **kwargs))
    except BaseException as exc:
        errors.append(exc)


def _call_tool(server: FastMCP, tool_name: str, ctx: Any, **kwargs: object) -> Any:
    """Call one registered FastMCP tool function directly."""
    tool_manager = server._tool_manager  # pyright: ignore[reportPrivateUsage]
    tool = tool_manager.get_tool(tool_name)
    assert tool is not None
    return tool.fn(ctx, **kwargs)
