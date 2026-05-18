"""Package and CLI smoke coverage for RoastPilot."""

import asyncio
import json
import os
import sys
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coffee_roaster_mcp import __version__, cli
from coffee_roaster_mcp.cli import build_parser, main
from coffee_roaster_mcp.config import ConfigError
from coffee_roaster_mcp.mcp_server import build_server_context, run_driver_emergency_stop

REPO_ROOT = Path(__file__).resolve().parents[1]
_T = TypeVar("_T")

_EXPECTED_ROAST_STATE_KEYS = {
    "active",
    "beans_added_at_utc",
    "beans_added_monotonic_seconds",
    "beans_dropped_at_utc",
    "beans_dropped_monotonic_seconds",
    "cooling_on",
    "cooling_started_at_utc",
    "cooling_started_monotonic_seconds",
    "cooling_stopped_at_utc",
    "cooling_stopped_monotonic_seconds",
    "created_at_utc",
    "development_percent",
    "development_time_seconds",
    "device_state",
    "elapsed_monotonic_seconds",
    "events",
    "fan_level_percent",
    "faulted_at_utc",
    "faulted_monotonic_seconds",
    "first_crack_at_utc",
    "first_crack_monotonic_seconds",
    "first_crack_status",
    "heat_level_percent",
    "log_dir",
    "phase",
    "roast_elapsed_seconds",
    "session_id",
    "stopped_at_utc",
    "t0_status",
}
_EXPECTED_DEVICE_STATE_KEYS = {
    "bean_temp_c",
    "connected",
    "cooling_on",
    "driver",
    "env_temp_c",
    "fan_level_percent",
    "heat_level_percent",
    "raw_vendor_data",
}
_EXPECTED_FIRST_CRACK_STATUS_KEYS = {
    "allow_manual_override",
    "detected_at_utc",
    "detected_monotonic_seconds",
    "mode",
    "reason",
    "status",
}
_EXPECTED_T0_STATUS_KEYS = {
    "auto_detection_enabled",
    "charge_temperature_c",
    "current_drop_c",
    "detected_bean_temperature_c",
    "drop_threshold_c",
    "reason",
    "status",
}


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_program_name() -> None:
    parser = build_parser()

    assert parser.prog == "coffee-roaster-mcp"


def test_main_without_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "usage: coffee-roaster-mcp" in output
    assert "{serve,hottop-validate}" in output


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


def test_hottop_validate_returns_nonzero_for_unsuccessful_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SimpleNamespace(hardware_ready_release_label_allowed=False)

    def fake_run_hottop_validation(options: Any) -> Any:
        _ = options
        return report

    def fake_report_to_json(validation_report: Any) -> str:
        _ = validation_report
        return "{}\n"

    monkeypatch.setattr(cli, "run_hottop_validation", fake_run_hottop_validation)
    monkeypatch.setattr(cli, "report_to_json", fake_report_to_json)

    assert main(["hottop-validate", "--i-understand-this-controls-hardware"]) == 1
    assert capsys.readouterr().out == "{}\n"


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
        assert drop_result.structuredContent["phase"] == "cooling"

        repeated_drop_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("drop_beans", {})),
        )
        assert repeated_drop_result.structuredContent["event"]["kind"] == "beans_dropped"
        assert repeated_drop_result.structuredContent["event_count"] == 4

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
        state_content = state_result.structuredContent
        assert state_content is not None
        assert set(state_content) == _EXPECTED_ROAST_STATE_KEYS
        assert state_content["session_id"] == session_id
        assert state_content["active"] is False
        assert state_content["phase"] == "complete"
        assert state_content["heat_level_percent"] == 0
        assert state_content["fan_level_percent"] == 100
        assert state_content["cooling_on"] is False
        assert state_content["roast_elapsed_seconds"] is not None
        assert state_content["development_time_seconds"] is not None
        assert state_content["development_percent"] is not None

        device_state = state_content["device_state"]
        assert set(device_state) == _EXPECTED_DEVICE_STATE_KEYS
        assert device_state["driver"] == "mock"
        assert device_state["connected"] is True
        assert device_state["bean_temp_c"] is not None
        assert device_state["env_temp_c"] is not None
        assert device_state["heat_level_percent"] == 0
        assert device_state["fan_level_percent"] == 100
        assert device_state["cooling_on"] is False
        assert isinstance(device_state["raw_vendor_data"], dict)

        first_crack_status = state_content["first_crack_status"]
        assert set(first_crack_status) == _EXPECTED_FIRST_CRACK_STATUS_KEYS
        assert first_crack_status["mode"] == "disabled"
        assert first_crack_status["status"] == "detected"
        assert first_crack_status["detected_at_utc"] == state_content["first_crack_at_utc"]
        assert (
            first_crack_status["detected_monotonic_seconds"]
            == state_content["first_crack_monotonic_seconds"]
        )
        assert first_crack_status["allow_manual_override"] is True
        assert first_crack_status["reason"] is None

        t0_status = state_content["t0_status"]
        assert set(t0_status) == _EXPECTED_T0_STATUS_KEYS
        assert t0_status["auto_detection_enabled"] is False
        assert t0_status["status"] == "detected"
        assert t0_status["drop_threshold_c"] == 25.0
        assert t0_status["charge_temperature_c"] is None
        assert t0_status["detected_bean_temperature_c"] is None
        assert t0_status["reason"] is None

        assert state_content["beans_added_at_utc"] is not None
        assert state_content["beans_added_monotonic_seconds"] is not None
        assert state_content["first_crack_at_utc"] is not None
        assert state_content["first_crack_monotonic_seconds"] is not None
        assert state_content["beans_dropped_at_utc"] is not None
        assert state_content["beans_dropped_monotonic_seconds"] is not None
        assert state_content["cooling_started_at_utc"] is not None
        assert state_content["cooling_started_monotonic_seconds"] is not None
        assert state_content["cooling_stopped_at_utc"] is not None
        assert state_content["cooling_stopped_monotonic_seconds"] is not None
        assert state_content["faulted_at_utc"] is None
        assert state_content["faulted_monotonic_seconds"] is None
        assert [event["kind"] for event in state_content["events"]] == [
            "beans_added",
            "first_crack_detected",
            "beans_dropped",
            "cooling_started",
            "cooling_stopped",
        ]
        assert state_content["events"][2]["payload"] == {}

        export_result = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("export_roast_log", {"session_id": session_id})
            ),
        )
        assert export_result.structuredContent["session_id"] == session_id
        assert export_result.structuredContent["ready"] is True
        export_log_dir = Path(export_result.structuredContent["log_dir"])
        export_jsonl_path = Path(export_result.structuredContent["jsonl_path"])
        export_csv_path = Path(export_result.structuredContent["csv_path"])
        export_summary_path = Path(export_result.structuredContent["summary_path"])
        assert export_log_dir.is_absolute()
        assert export_jsonl_path.is_absolute()
        assert export_jsonl_path.name == "roast.jsonl"
        assert export_csv_path.name == "roast.csv"
        assert export_summary_path.name == "summary.json"
        assert export_log_dir.exists()
        assert export_jsonl_path.exists()
        assert export_csv_path.exists()
        assert export_summary_path.exists()
        exported_events = [
            json.loads(line) for line in export_jsonl_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["kind"] for event in exported_events] == [
            "beans_added",
            "first_crack_detected",
            "beans_dropped",
            "cooling_started",
            "cooling_stopped",
        ]
        export_summary = json.loads(export_summary_path.read_text(encoding="utf-8"))
        assert export_summary["session_id"] == session_id
        assert export_summary["phase"] == "complete"
        assert export_summary["event_count"] == 5
        assert export_summary["metrics"]["roast_elapsed_seconds"] is not None
        assert export_summary["metrics"]["development_time_seconds"] is not None

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
        emergency_payload = emergency_stop_result.structuredContent["event"]["payload"]
        assert emergency_payload["reason"] == "test-path"
        assert emergency_payload["driver"] == "mock"
        assert emergency_payload["driver_safety_method"] == "emergency_stop"
        assert emergency_payload["driver_safety_method_called"] is True

        faulted_state_result = cast(
            Any,
            await _call_with_timeout(
                session.call_tool("get_roast_state", {"session_id": second_session_id})
            ),
        )
        assert faulted_state_result.structuredContent["active"] is False
        assert faulted_state_result.structuredContent["phase"] == "fault"
        faulted_payload = faulted_state_result.structuredContent["events"][-1]["payload"]
        assert faulted_payload["reason"] == "test-path"
        assert faulted_payload["driver"] == "mock"
        assert faulted_payload["driver_safety_method_called"] is True

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


def test_stdio_server_rejects_invalid_phase_transition_calls(tmp_path: Path) -> None:
    asyncio.run(_assert_invalid_phase_transitions(tmp_path))


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


async def _assert_invalid_phase_transitions(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "first_crack:\n  allow_manual_override: true\n",
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

        first_crack_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_first_crack", {})),
        )
        assert first_crack_result.isError is True
        assert first_crack_result.content is not None
        assert "allowed phases: roasting" in first_crack_result.content[0].text

        drop_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("drop_beans", {})),
        )
        assert drop_result.isError is True
        assert drop_result.content is not None
        assert "roasting, development" in drop_result.content[0].text

        await _call_with_timeout(session.call_tool("mark_beans_added", {}))
        await _call_with_timeout(session.call_tool("drop_beans", {}))

        late_first_crack_result = cast(
            Any,
            await _call_with_timeout(session.call_tool("mark_first_crack", {})),
        )
        assert late_first_crack_result.isError is True
        assert late_first_crack_result.content is not None
        assert "allowed phases: roasting" in late_first_crack_result.content[0].text


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


def test_build_server_context_wraps_unknown_driver_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: missing-driver\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="missing-driver"):
        build_server_context(config_path=config_path)


def test_build_server_context_passes_hottop_serial_config_to_driver(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "roaster:",
                "  driver: hottop_kn8828b_2k_plus",
                "  port: /dev/test-hottop",
                "  baudrate: 57600",
                "  command_interval_seconds: 0.2",
            ]
        ),
        encoding="utf-8",
    )

    server_context = build_server_context(config_path=config_path)
    driver_state = server_context.roaster_driver.read_state()

    assert driver_state.driver == "hottop_kn8828b_2k_plus"
    assert driver_state.raw_vendor_data["port"] == "/dev/test-hottop"
    assert driver_state.raw_vendor_data["baudrate"] == 57_600
    assert driver_state.raw_vendor_data["command_interval_seconds"] == 0.2


def test_driver_emergency_stop_failure_returns_fail_closed_payload() -> None:
    server_context = build_server_context()
    object.__setattr__(server_context, "roaster_driver", _FailingSafetyDriver())

    payload = run_driver_emergency_stop(server_context, reason="unit-test")

    assert payload["driver"] == "mock"
    assert payload["driver_safety_method"] == "emergency_stop"
    assert payload["driver_safety_method_called"] is False
    assert payload["driver_error"] == "RuntimeError: driver offline"
    assert payload["heat_level_percent"] == 0
    assert payload["fan_level_percent"] == 100
    assert payload["cooling_on"] is True


class _FailingSafetyDriver:
    """Test driver that simulates an emergency-stop I/O failure."""

    def emergency_stop(self, *, reason: str) -> object:
        """Raise a deterministic driver failure."""
        _ = reason
        raise RuntimeError("driver offline")


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
