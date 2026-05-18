"""In-process MCP tool coverage for RoastPilot."""

from __future__ import annotations

import json
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
from coffee_roaster_mcp.mcp_server import ServerContext, build_server_context, create_mcp_server
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore, SessionLifecycleError


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

    events = [json.loads(line) for line in Path(export.jsonl_path).read_text().splitlines()]
    assert [event["kind"] for event in events] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]


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


def test_stale_command_does_not_emergency_stop_newer_active_session(tmp_path: Path) -> None:
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
    second_start = _call_tool(server, "start_roast_session", ctx)
    release_command.set()
    heat_thread.join(timeout=1.0)

    assert not heat_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    assert second_start.session.session_id != first_start.session.session_id
    second_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=second_start.session.session_id,
    )
    assert second_state.active is True
    assert second_state.phase == "pre_roast"
    assert driver.actions == [
        "connect",
        "set_heat:65",
        "emergency_stop:unit-test",
        "connect",
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
        stop_cooling_stays_on: bool = False,
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
        self.stop_cooling_stays_on = stop_cooling_stays_on
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
        self.cooling_on = self.stop_cooling_stays_on
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

    def __init__(self, *, status: str = "pending", reason: str | None = None) -> None:
        self.status = status
        self.reason = reason
        self.active_session_id: str | None = None
        self.started_sessions: list[str] = []
        self.processed_sessions: list[str] = []
        self.stopped_sessions: list[str] = []

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
        return self.snapshot()

    def stop_for_session(self, session_id: str, *, reason: str) -> FirstCrackRuntimeSnapshot:
        self.stopped_sessions.append(session_id)
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
