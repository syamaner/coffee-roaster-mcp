"""In-process MCP tool coverage for RoastPilot."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from coffee_roaster_mcp.mcp_server import ServerContext, build_server_context, create_mcp_server


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
    assert drop.phase == "dropped"
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


def _ctx(server_context: ServerContext) -> Any:
    """Build the minimal context shape used by FastMCP tool functions."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server_context))


def _call_tool(server: FastMCP, tool_name: str, ctx: Any, **kwargs: object) -> Any:
    """Call one registered FastMCP tool function directly."""
    tool_manager = server._tool_manager  # pyright: ignore[reportPrivateUsage]
    tool = tool_manager.get_tool(tool_name)
    assert tool is not None
    return tool.fn(ctx, **kwargs)
