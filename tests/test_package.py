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
        assert tool_names == {"get_server_info", "get_runtime_config"}

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
