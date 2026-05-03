"""Package and CLI smoke coverage for RoastPilot."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.cli import build_parser, main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_program_name() -> None:
    parser = build_parser()

    assert parser.prog == "coffee-roaster-mcp"


def test_main_returns_success() -> None:
    assert main([]) == 0


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
        await session.initialize()

        tools = await session.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        assert tool_names == {"get_server_info", "get_runtime_config"}

        server_info = await session.call_tool("get_server_info", {})
        assert server_info.structuredContent is not None
        assert server_info.structuredContent["product_name"] == "RoastPilot"
        assert server_info.structuredContent["transport"] == "stdio"
        assert server_info.structuredContent["bootstrap_safe"] is True

        runtime_config = await session.call_tool("get_runtime_config", {})
        assert runtime_config.structuredContent is not None
        assert runtime_config.structuredContent["roaster_driver"] == "mock"
        assert runtime_config.structuredContent["first_crack_mode"] == "disabled"


def _build_clean_server_env() -> dict[str, str]:
    """Return a deterministic subprocess environment for MCP startup tests."""
    return {
        key: value
        for key, value in {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}.items()
        if not key.startswith("COFFEE_")
    }
