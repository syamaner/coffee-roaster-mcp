"""Package and CLI smoke coverage for RoastPilot."""

import asyncio
import os
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.cli import build_parser, main
from coffee_roaster_mcp.mcp_server import build_server_context

REPO_ROOT = Path(__file__).resolve().parents[1]
_T = TypeVar("_T")


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_program_name() -> None:
    parser = build_parser()

    assert parser.prog == "coffee-roaster-mcp"


def test_main_without_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "usage: coffee-roaster-mcp" in output
    assert "{serve}" in output


def test_main_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"coffee-roaster-mcp {__version__}"


def test_main_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: coffee-roaster-mcp" in output
    assert "RoastPilot" in output


def test_stdio_server_starts_and_exposes_bootstrap_tools(tmp_path: Path) -> None:
    asyncio.run(_assert_stdio_server_tools(tmp_path))


async def _assert_stdio_server_tools(tmp_path: Path) -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve"],
        env=_build_clean_server_env(),
        cwd=tmp_path,
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await _call_with_timeout(session.initialize())

        tools = cast(Any, await _call_with_timeout(session.list_tools()))
        tool_names = {tool.name for tool in tools.tools}
        assert tool_names == {
            "drop_beans",
            "emergency_stop",
            "export_roast_log",
            "get_roast_state",
            "get_runtime_config",
            "get_server_info",
            "mark_beans_added",
            "mark_first_crack",
            "set_fan",
            "set_heat",
            "start_cooling",
            "start_roast_session",
            "stop_cooling",
        }

        server_info = cast(Any, await _call_with_timeout(session.call_tool("get_server_info", {})))
        assert server_info.structuredContent is not None
        assert server_info.structuredContent["product_name"] == "RoastPilot"
        assert server_info.structuredContent["transport"] == "stdio"
        assert server_info.structuredContent["bootstrap_safe"] is True

        runtime_config = cast(
            Any, await _call_with_timeout(session.call_tool("get_runtime_config", {}))
        )
        assert runtime_config.structuredContent is not None
        assert runtime_config.structuredContent["roaster_driver"] == "mock"
        assert runtime_config.structuredContent["first_crack_mode"] == "disabled"


def test_stdio_server_supports_basic_mock_roast_tool_flow(tmp_path: Path) -> None:
    asyncio.run(_assert_basic_mock_roast_flow(tmp_path))


async def _assert_basic_mock_roast_flow(tmp_path: Path) -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve"],
        env=_build_clean_server_env(),
        cwd=tmp_path,
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await _call_with_timeout(session.initialize())

        start_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("start_roast_session", {})),
        )
        started_session = start_result.structuredContent["session"]
        session_id = started_session["session_id"]
        assert started_session["phase"] == "pre_roast"
        assert started_session["active"] is True

        heat_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("set_heat", {"heat_level_percent": 60})),
        )
        assert heat_result.structuredContent["heat_level_percent"] == 60

        fan_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("set_fan", {"fan_level_percent": 35})),
        )
        assert fan_result.structuredContent["fan_level_percent"] == 35

        beans_added_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_beans_added", {})),
        )
        assert beans_added_result.structuredContent["event"]["kind"] == "beans_added"
        assert beans_added_result.structuredContent["phase"] == "roasting"

        first_crack_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_first_crack", {})),
        )
        assert first_crack_result.structuredContent["event"]["kind"] == "first_crack_detected"
        assert first_crack_result.structuredContent["phase"] == "development"

        repeated_beans_added_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_beans_added", {})),
        )
        assert repeated_beans_added_result.structuredContent["event"]["kind"] == "beans_added"
        assert repeated_beans_added_result.structuredContent["event_count"] == 2

        drop_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("drop_beans", {})),
        )
        assert drop_result.structuredContent["event"]["kind"] == "beans_dropped"
        assert drop_result.structuredContent["phase"] == "dropped"

        repeated_drop_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("drop_beans", {})),
        )
        assert repeated_drop_result.structuredContent["event"]["kind"] == "beans_dropped"
        assert repeated_drop_result.structuredContent["event_count"] == 3

        cooling_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("start_cooling", {})),
        )
        assert cooling_result.structuredContent["event"]["kind"] == "cooling_started"
        assert cooling_result.structuredContent["phase"] == "cooling"

        stopped_cooling_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("stop_cooling", {})),
        )
        assert stopped_cooling_result.structuredContent["event"]["kind"] == "cooling_stopped"
        assert stopped_cooling_result.structuredContent["phase"] == "complete"

        state_result = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("get_roast_state", {"session_id": session_id})
            ),
        )
        assert state_result.structuredContent["session_id"] == session_id
        assert state_result.structuredContent["active"] is False
        assert state_result.structuredContent["heat_level_percent"] == 0
        assert state_result.structuredContent["fan_level_percent"] == 35
        assert state_result.structuredContent["cooling_on"] is False
        assert [event["kind"] for event in state_result.structuredContent["events"]] == [
            "beans_added",
            "first_crack_detected",
            "beans_dropped",
            "cooling_started",
            "cooling_stopped",
        ]
        assert state_result.structuredContent["events"][2]["payload"] == {}

        export_result = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("export_roast_log", {"session_id": session_id})
            ),
        )
        assert export_result.structuredContent["session_id"] == session_id
        assert export_result.structuredContent["ready"] is False
        export_log_dir = Path(export_result.structuredContent["log_dir"])
        export_jsonl_path = Path(export_result.structuredContent["jsonl_path"])
        assert export_log_dir.is_absolute()
        assert export_jsonl_path.is_absolute()
        assert export_jsonl_path.name == "roast.jsonl"
        assert not export_log_dir.exists()

        second_start_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("start_roast_session", {})),
        )
        second_session_id = second_start_result.structuredContent["session"]["session_id"]
        assert second_session_id != session_id

        emergency_stop_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("emergency_stop", {"reason": "test-path"})),
        )
        assert emergency_stop_result.structuredContent["session_id"] == second_session_id
        assert emergency_stop_result.structuredContent["event"]["kind"] == "fault"
        assert emergency_stop_result.structuredContent["phase"] == "fault"
        assert emergency_stop_result.structuredContent["event"]["payload"] == {
            "reason": "test-path"
        }

        faulted_state_result = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("get_roast_state", {"session_id": second_session_id})
            ),
        )
        assert faulted_state_result.structuredContent["active"] is False
        assert faulted_state_result.structuredContent["phase"] == "fault"
        assert faulted_state_result.structuredContent["events"][-1]["payload"] == {
            "reason": "test-path"
        }

        third_start_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("start_roast_session", {})),
        )
        assert third_start_result.structuredContent["session"]["session_id"] != second_session_id

        old_session_state_after_rollover = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("get_roast_state", {"session_id": session_id})
            ),
        )
        assert old_session_state_after_rollover.structuredContent["session_id"] == session_id
        assert old_session_state_after_rollover.structuredContent["active"] is False


def test_stdio_server_rejects_manual_first_crack_override_when_disabled(tmp_path: Path) -> None:
    asyncio.run(_assert_manual_override_disabled(tmp_path))


async def _assert_manual_override_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "first_crack:\n  allow_manual_override: false\n",
        encoding="utf-8",
    )
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve"],
        env=_build_clean_server_env(),
        cwd=tmp_path,
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await _call_with_timeout(session.initialize())
        await _call_with_timeout(session.call_tool("start_roast_session", {}))

        result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_first_crack", {})),
        )
        assert result.isError is True
        assert result.content is not None
        assert "Manual first-crack override is disabled" in result.content[0].text


def test_stdio_server_reports_manual_mode_as_bootstrap_safe(tmp_path: Path) -> None:
    asyncio.run(_assert_manual_mode_bootstrap_safe(tmp_path))


async def _assert_manual_mode_bootstrap_safe(tmp_path: Path) -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve"],
        env=_build_clean_server_env({"COFFEE_FIRST_CRACK_MODE": "manual"}),
        cwd=tmp_path,
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await _call_with_timeout(session.initialize())

        server_info = cast(Any, await _call_with_timeout(session.call_tool("get_server_info", {})))
        assert server_info.structuredContent is not None
        assert server_info.structuredContent["first_crack_mode"] == "manual"
        assert server_info.structuredContent["bootstrap_safe"] is True


def test_stdio_server_uses_configured_log_root_for_session_store(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("logging:\n  log_dir: ./custom-logs\n", encoding="utf-8")

    server_context = build_server_context(config_path=config_path)
    session = server_context.session_store.start_session()

    assert session.log_writer is not None
    assert session.log_writer.log_dir == Path("custom-logs/roasts") / session.id


def _build_clean_server_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return a minimal subprocess environment for MCP startup tests."""
    pythonpath_parts = [str(REPO_ROOT / "src")]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)

    base_env = {"PYTHONPATH": os.pathsep.join(pythonpath_parts)}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"):
        value = os.environ.get(key)
        if value is not None:
            base_env[key] = value

    if overrides is not None:
        base_env.update(overrides)
    return base_env


async def _call_with_timeout(awaitable: Awaitable[_T], timeout_seconds: float = 5.0) -> _T:
    """Fail fast if the MCP smoke test subprocess stops responding."""
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
