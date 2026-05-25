"""Run opt-in MCP first-crack validation against the committed WAV fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL_REVISION = "b349a919c34b6130472da97c01817be404e4f629"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"
FIXTURE_STEM = "roastpilot-fc-replay-001"


def main(argv: list[str] | None = None) -> int:
    """Validate labelled WAV replay through public MCP tools."""
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_validation(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Opt-in local validation for mock-roaster MCP first-crack detection "
            "using the committed labelled WAV fixture and released INT8 artifacts."
        )
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Optional working directory for config and exported logs.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
        help="Maximum time to wait for MCP detection.",
    )
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=1.0,
        help="Allowed detection tolerance after the labelled interval start.",
    )
    return parser


async def _run_validation(args: argparse.Namespace) -> int:
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="roastpilot-fc-replay-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    config_path = work_dir / "coffee-roaster-mcp.yaml"
    log_dir = work_dir / "logs"
    labels_path = FIXTURE_DIR / f"{FIXTURE_STEM}.labels.json"
    manifest_path = FIXTURE_DIR / f"{FIXTURE_STEM}.manifest.json"
    labels = cast(dict[str, Any], json.loads(labels_path.read_text(encoding="utf-8")))
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    label = cast(dict[str, Any], labels["annotations"][0])
    label_start = float(label["start_time"])
    label_end = float(label["end_time"])
    _write_config(config_path, log_dir=log_dir)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve", "--config", str(config_path)],
        env=_server_env(),
        cwd=REPO_ROOT,
    )
    started_at = time.monotonic()
    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await _call(session.initialize())
        start_result = await _call(session.call_tool("start_roast_session", {}))
        session_id = start_result.structuredContent["session"]["session_id"]
        beans_added = await _call(session.call_tool("mark_beans_added", {}))
        beans_added_seconds = float(beans_added.structuredContent["event"]["monotonic_seconds"])

        state_content: dict[str, Any] | None = None
        deadline = time.monotonic() + float(args.timeout_seconds)
        while time.monotonic() < deadline:
            state = await _call(session.call_tool("get_roast_state", {"session_id": session_id}))
            state_content = cast(dict[str, Any], state.structuredContent)
            first_crack_status = cast(dict[str, Any], state_content["first_crack_status"])
            if first_crack_status["status"] == "detected":
                break
            if first_crack_status["status"] in {"faulted", "unavailable"}:
                raise RuntimeError(f"first-crack runtime failed: {first_crack_status}")
            if (
                first_crack_status["audio_running"] is False
                and first_crack_status["emitted_window_count"] > 0
                and first_crack_status["processed_window_count"]
                >= first_crack_status["emitted_window_count"]
            ):
                raise RuntimeError(
                    f"replay exhausted without first-crack detection: {first_crack_status}"
                )
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("first crack was not detected before timeout.")

        assert state_content is not None
        first_crack_seconds = float(state_content["first_crack_monotonic_seconds"])
        detected_after_t0 = first_crack_seconds - beans_added_seconds
        if not label_start <= detected_after_t0 <= label_end + float(args.tolerance_seconds):
            raise RuntimeError(
                "detected first-crack time "
                f"{detected_after_t0:.3f}s after T0 is outside label interval "
                f"{label_start:.3f}-{label_end:.3f}s."
            )

        export = await _call(session.call_tool("export_roast_log", {"session_id": session_id}))
        elapsed_wall_seconds = time.monotonic() - started_at
        metrics = cast(dict[str, Any], state_content["first_crack_status"])
        summary = {
            "session_id": session_id,
            "fixture": str(FIXTURE_DIR / f"{FIXTURE_STEM}.wav"),
            "fixture_sha256": manifest["output_audio"]["sha256"],
            "model_revision": MODEL_REVISION,
            "label_interval_seconds_after_t0": {"start": label_start, "end": label_end},
            "detected_seconds_after_t0": detected_after_t0,
            "wall_seconds_elapsed": elapsed_wall_seconds,
            "effective_replay_speed": detected_after_t0 / elapsed_wall_seconds,
            "runtime_metrics": {
                "emitted_window_count": metrics["emitted_window_count"],
                "processed_window_count": metrics["processed_window_count"],
                "dropped_window_count": metrics["dropped_window_count"],
            },
            "exports": {
                "jsonl_path": export.structuredContent["jsonl_path"],
                "csv_path": export.structuredContent["csv_path"],
                "summary_path": export.structuredContent["summary_path"],
            },
        }
        print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


async def _call(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=30.0)


def _write_config(config_path: Path, *, log_dir: Path) -> None:
    wav_path = json.dumps(str(FIXTURE_DIR / f"{FIXTURE_STEM}.wav"))
    log_dir_path = json.dumps(str(log_dir))
    config_path.write_text(
        "\n".join(
            [
                "roaster:",
                "  driver: mock",
                "first_crack:",
                "  mode: audio",
                "  repo_id: syamaner/coffee-first-crack-detection",
                f"  revision: {MODEL_REVISION}",
                "  precision: int8",
                "  local_model_dir: null",
                "  onnx_threads: 2",
                "  allow_manual_override: true",
                "audio:",
                "  source: wav",
                "  input_device: null",
                "  sample_rate: 16000",
                f"  wav_path: {wav_path}",
                "  replay_mode: detector_paced",
                "  window_seconds: 10.0",
                "logging:",
                f"  log_dir: {log_dir_path}",
                "  sample_interval_seconds: 5.0",
                "session:",
                "  auto_t0_detection_enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _server_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("COFFEE_"):
            del env[key]
    src_path = str(REPO_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path
        if existing_pythonpath is None
        else os.pathsep.join((src_path, existing_pythonpath))
    )
    return env


if __name__ == "__main__":
    raise SystemExit(main())
