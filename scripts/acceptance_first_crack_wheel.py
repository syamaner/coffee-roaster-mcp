"""Validate clean-wheel first-crack inference without Torch or Transformers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import venv
import zipfile
from collections.abc import Mapping
from email import message_from_bytes
from pathlib import Path
from typing import Any, NoReturn, cast

MODEL_REPOSITORY = "syamaner/coffee-first-crack-detection"
MODEL_REVISION = "b349a919c34b6130472da97c01817be404e4f629"
FIXTURE_PATH = "tests/fixtures/audio/roastpilot-fc-replay-001.wav"
FIXTURE_SHA256 = "923c61a456b04797c1302ed78984ab7d9b148d7dc21d3825b225b8a6043aa9fc"
MODEL_ARTIFACTS = {
    "onnx/int8/model_quantized.onnx": (
        89_862_238,
        "022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df",
    ),
    "onnx/int8/preprocessor_config.json": (
        297,
        "8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231",
    ),
}
_BANNED = {"torch", "torchaudio", "transformers"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]+")
_CHILD_ENVIRONMENT_KEYS = {
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


def main(argv: list[str] | None = None) -> int:
    """Run the parent clean-wheel flow or one installed-only child operation."""
    args = _parser().parse_args(argv)
    if args.resolve_artifacts:
        return _resolve_artifacts(args)
    if args.installed_replay:
        return asyncio.run(_installed_replay(args))
    return _clean_wheel_acceptance(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-model-dir", type=Path, default=None)
    parser.add_argument("--resolve-artifacts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--installed-replay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model-root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def _clean_wheel_acceptance(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="coffee-roaster-mcp-wheel-") as temporary:
        temporary_root = Path(temporary)
        wheel = _build_one_wheel(root, temporary_root)
        wheel_banned_requirements = _assert_no_banned_requirements(
            _wheel_metadata(wheel).get_all("Requires-Dist") or []
        )
        venv_root = temporary_root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
        executable = _venv_python(venv_root)
        _run([executable, "-m", "pip", "install", str(wheel)], root)
        _run([executable, "-m", "pip", "check"], root)
        dependencies = _assert_installed_dependency_isolation(executable, root)
        resolved = _resolve_with_installed_wheel(
            executable,
            root,
            args.local_model_dir,
            _sanitized_child_environment(temporary_root / "huggingface-cache"),
        )
        model_root = _stage_model_tree(resolved, temporary_root)
        replay = _run(
            [
                executable,
                str(Path(__file__).resolve()),
                "--installed-replay",
                "--model-root",
                str(model_root),
                "--work-dir",
                str(temporary_root / "replay"),
            ],
            root,
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "wheel": wheel.name,
                    "metadata_checked_before_install": True,
                    "dependency_isolation": {
                        "wheel_metadata_banned_requirements": wheel_banned_requirements,
                        "installed_state": dependencies,
                    },
                    "replay": json.loads(replay.stdout),
                    "model_repository": MODEL_REPOSITORY,
                    "model_revision": MODEL_REVISION,
                },
                sort_keys=True,
            )
        )
    return 0


def _build_one_wheel(root: Path, temporary_root: Path) -> Path:
    output_directory = temporary_root / "wheel"
    output_directory.mkdir()
    _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(output_directory)], root)
    wheels = sorted(output_directory.glob("*.whl"))
    if len(wheels) != 1:
        _fail(f"expected exactly one fresh wheel, found {len(wheels)}")
    return wheels[0]


def _wheel_metadata(wheel: Path) -> Any:
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(members) != 1:
            _fail(f"expected exactly one wheel METADATA member, found {len(members)}")
        return message_from_bytes(archive.read(members[0]))


def _assert_no_banned_requirements(requirements: list[str]) -> list[str]:
    banned = sorted(item for item in requirements if _requirement_name(item) in _BANNED)
    if banned:
        _fail(f"requirements reintroduced banned packages: {banned}")
    return banned


def _requirement_name(requirement: str) -> str:
    match = _NAME.match(requirement.strip())
    return "" if match is None else match.group().lower().replace("_", "-").replace(".", "-")


def _venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _assert_installed_dependency_isolation(executable: Path, root: Path) -> dict[str, object]:
    code = """
import importlib.metadata as metadata
import importlib.util
import json
import re

banned = {'torch', 'torchaudio', 'transformers'}
distributions = [((item.metadata['Name'] or '').lower(), item.requires or [])
                 for item in metadata.distributions()]
installed = sorted(name for name, _ in distributions if name in banned)
specs = {name: importlib.util.find_spec(name) is not None for name in sorted(banned)}
print(json.dumps({'banned_distributions': installed,
                  'import_specs': specs}, sort_keys=True))
"""
    completed = _run([executable, "-c", code], root)
    state = cast(dict[str, object], json.loads(completed.stdout))
    banned_distributions = cast(list[str], state["banned_distributions"])
    import_specs = cast(dict[str, bool], state["import_specs"])
    evidence = _installed_dependency_evidence(banned_distributions, import_specs)
    if evidence["banned_distributions"] or any(evidence["import_specs"].values()):
        _fail(f"installed state reintroduced banned packages: {evidence}")
    return evidence


def _installed_dependency_evidence(
    banned_distributions: list[str], import_specs: Mapping[str, bool]
) -> dict[str, object]:
    """Return dependency-isolation evidence from active installed state only."""
    return {
        "banned_distributions": sorted(banned_distributions),
        "import_specs": {name: bool(import_specs.get(name, False)) for name in sorted(_BANNED)},
    }


def _resolve_with_installed_wheel(
    executable: Path,
    root: Path,
    local_model_dir: Path | None,
    environment: Mapping[str, str],
) -> dict[str, str]:
    command: list[Path | str] = [executable, Path(__file__).resolve(), "--resolve-artifacts"]
    if local_model_dir is not None:
        command.extend(("--local-model-dir", local_model_dir.resolve()))
    return cast(dict[str, str], json.loads(_run(command, root, environment=environment).stdout))


def _resolve_artifacts(args: argparse.Namespace) -> int:
    """Resolve and verify artifacts using the installed production resolver only."""
    from coffee_roaster_mcp.artifacts import resolve_first_crack_detector_artifacts
    from coffee_roaster_mcp.config import FirstCrackConfig

    artifacts = resolve_first_crack_detector_artifacts(
        FirstCrackConfig(
            mode="audio",
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            precision="int8",
            local_model_dir=args.local_model_dir,
        )
    )
    paths = {
        "onnx/int8/model_quantized.onnx": artifacts.onnx_model.local_path,
        "onnx/int8/preprocessor_config.json": artifacts.feature_extractor_config.local_path,
    }
    _verify_artifacts(paths)
    print(json.dumps({name: str(path) for name, path in paths.items()}, sort_keys=True))
    return 0


def _stage_model_tree(resolved: dict[str, str], temporary_root: Path) -> Path:
    model_root = temporary_root / "model"
    for filename in MODEL_ARTIFACTS:
        destination = model_root / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(resolved[filename]), destination)
    _verify_artifacts({filename: model_root / filename for filename in MODEL_ARTIFACTS})
    return model_root


def _verify_artifacts(paths: Mapping[str, Path]) -> None:
    """Verify each immutable artifact before an ONNX session can be constructed."""
    for filename, (expected_size, expected_digest) in MODEL_ARTIFACTS.items():
        path = paths[filename]
        if path.stat().st_size != expected_size:
            _fail(f"unexpected size for {filename}: {path}")
        if _sha256(path) != expected_digest:
            _fail(f"unexpected SHA-256 for {filename}: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _installed_replay(args: argparse.Namespace) -> int:
    """Run detector-paced replay through the installed MCP server process."""
    if args.model_root is None or args.work_dir is None:
        _fail("installed replay requires --model-root and --work-dir")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[1]
    fixture = root / FIXTURE_PATH
    if _sha256(fixture) != FIXTURE_SHA256:
        _fail(f"unexpected replay fixture SHA-256: {fixture}")
    _verify_artifacts({filename: args.model_root / filename for filename in MODEL_ARTIFACTS})
    args.work_dir.mkdir(parents=True, exist_ok=True)
    config = args.work_dir / "coffee-roaster-mcp.yaml"
    _write_replay_config(config, args.model_root, args.work_dir / "logs", fixture)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coffee_roaster_mcp.cli", "serve", "--config", str(config)],
        env=_sanitized_child_environment(args.work_dir / "huggingface-cache"),
        cwd=args.work_dir,
    )
    started = time.monotonic()
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await _call(session.initialize())
        started_session = await _call(session.call_tool("start_roast_session", {}))
        session_id = cast(str, started_session.structuredContent["session"]["session_id"])
        beans_added = await _call(session.call_tool("mark_beans_added", {}))
        beans_added_time = float(beans_added.structuredContent["event"]["monotonic_seconds"])
        state = await _wait_for_detection(session, session_id)
        export = await _call(session.call_tool("export_roast_log", {"session_id": session_id}))
    detected = float(state["first_crack_monotonic_seconds"]) - beans_added_time
    if not 3.82710390663442 <= detected <= 21.0 or detected >= 20.017:
        _fail(f"first-crack time failed replay bounds: {detected}")
    metrics = cast(dict[str, Any], state["first_crack_status"])
    expected_metrics = {
        "emitted_window_count": 3,
        "processed_window_count": 3,
        "dropped_window_count": 0,
    }
    if {key: metrics[key] for key in expected_metrics} != expected_metrics:
        _fail(f"unexpected replay metrics: {metrics}")
    summary = _assert_export(export.structuredContent, args.work_dir)
    model = cast(dict[str, Any], summary["first_crack_model"])
    if (model["repo_id"], model["revision"], model["precision"]) != (
        MODEL_REPOSITORY,
        MODEL_REVISION,
        "int8",
    ):
        _fail(f"exported model metadata differs from the immutable pin: {model}")
    if float(model["confidence"]) < 0.6:
        _fail(f"confirming confidence was below threshold: {model}")
    print(
        json.dumps(
            {
                "status": "passed",
                "detected_seconds_after_t0": detected,
                "runtime_metrics": expected_metrics,
                "comparison_values_not_gated": {
                    "recorded_detected_seconds_after_t0": 10.017558290999885,
                    "recorded_confidence": 0.7762153826546956,
                },
                "wall_seconds_elapsed": time.monotonic() - started,
            },
            sort_keys=True,
        )
    )
    return 0


async def _wait_for_detection(session: Any, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        result = await _call(session.call_tool("get_roast_state", {"session_id": session_id}))
        state = cast(dict[str, Any], result.structuredContent)
        status = cast(dict[str, Any], state["first_crack_status"])
        if status["status"] == "detected":
            return state
        if status["status"] in {"faulted", "unavailable"}:
            _fail(f"first-crack runtime failed: {status}")
        if (
            not status["audio_running"]
            and status["emitted_window_count"] > 0
            and status["processed_window_count"] >= status["emitted_window_count"]
        ):
            _fail(f"replay exhausted without detection: {status}")
        await asyncio.sleep(0.05)
    _fail("first-crack detection timed out")


def _assert_export(export: Mapping[str, object], work_dir: Path) -> dict[str, Any]:
    jsonl = Path(cast(str, export["jsonl_path"]))
    summary = Path(cast(str, export["summary_path"]))
    if not jsonl.is_relative_to(work_dir) or not summary.is_relative_to(work_dir):
        _fail("export escaped temporary acceptance directory")
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    if (
        sum(row.get("kind") == "first_crack_detected" for row in rows if row.get("type") == "event")
        != 1
    ):
        _fail("expected exactly one first_crack_detected export event")
    return cast(dict[str, Any], json.loads(summary.read_text(encoding="utf-8")))


async def _call(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=30.0)


def _write_replay_config(config: Path, model_root: Path, log_dir: Path, fixture: Path) -> None:
    config.write_text(
        "\n".join(
            [
                "roaster:",
                "  driver: mock",
                "first_crack:",
                "  mode: audio",
                f"  repo_id: {MODEL_REPOSITORY}",
                f"  revision: {MODEL_REVISION}",
                "  precision: int8",
                f"  local_model_dir: {json.dumps(str(model_root))}",
                "  onnx_threads: 2",
                "  confidence_threshold: 0.6",
                "  min_positive_windows: 3",
                "  confirmation_window_seconds: 20.0",
                "  allow_manual_override: true",
                "audio:",
                "  source: wav",
                "  input_device: null",
                "  sample_rate: 16000",
                f"  wav_path: {json.dumps(str(fixture))}",
                "  replay_mode: detector_paced",
                "  window_seconds: 10.0",
                "  overlap: 0.7",
                "logging:",
                f"  log_dir: {json.dumps(str(log_dir))}",
                "  sample_interval_seconds: 5.0",
                "session:",
                "  auto_t0_detection_enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _sanitized_child_environment(huggingface_cache: Path) -> dict[str, str]:
    """Return the minimal acceptance-owned environment for child processes."""
    huggingface_cache.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value for key, value in os.environ.items() if key in _CHILD_ENVIRONMENT_KEYS
    }
    environment["HF_HOME"] = str(huggingface_cache)
    environment["HF_HUB_CACHE"] = str(huggingface_cache / "hub")
    return environment


def _run(
    command: list[Path | str], root: Path, *, environment: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    rendered_command = [str(item) for item in command]
    try:
        return subprocess.run(
            rendered_command,
            cwd=root,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        _fail(
            "command failed "
            f"(exit status {error.returncode}): {shlex.join(rendered_command)}\n"
            f"stdout:\n{_bounded_output(error.stdout)}\n"
            f"stderr:\n{_bounded_output(error.stderr)}"
        )


def _bounded_output(output: str | None, *, limit: int = 4_000) -> str:
    """Return enough subprocess output to diagnose failures without unbounded logs."""
    if output is None:
        return ""
    if len(output) <= limit:
        return output
    return f"{output[:limit]}\n... output truncated ..."


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
