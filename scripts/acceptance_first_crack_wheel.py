"""Validate clean-wheel first-crack inference without Torch or Transformers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
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
        await _call(session.call_tool("mark_beans_added", {}))
        state = await _wait_for_detection(session, session_id)
        export = await _call(session.call_tool("export_roast_log", {"session_id": session_id}))
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
    confirmation = _confirmation_evidence(
        Path(cast(str, export.structuredContent["jsonl_path"])),
        session_id,
        state,
        summary,
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "confirmation": confirmation["confirmation"],
                "onset": confirmation["onset"],
                "confidence": confirmation["confidence"],
                "runtime_metrics": expected_metrics,
                "comparison_values_not_gated": {
                    "recorded_detected_seconds_after_t0": 10.017558290999885,
                    "recorded_confidence": 0.7762153826546956,
                    "observed_onset_after_t0": confirmation["observed_onset_after_t0"],
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
    canonical_work_dir = work_dir.resolve()
    jsonl_is_contained = jsonl.resolve().is_relative_to(canonical_work_dir)
    summary_is_contained = summary.resolve().is_relative_to(canonical_work_dir)
    if not jsonl_is_contained or not summary_is_contained:
        _fail("export escaped temporary acceptance directory")
    return cast(dict[str, Any], json.loads(summary.read_text(encoding="utf-8")))


def _confirmation_evidence(
    jsonl_path: Path,
    session_id: str,
    state: Mapping[str, object],
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Validate and report session-based first-crack confirmation evidence."""
    events = _session_event_rows(jsonl_path, session_id)
    beans_added, beans_added_count = _single_session_event(events, "beans_added")
    first_crack, first_crack_count = _single_session_event(events, "first_crack_detected")

    beans_added_time = _finite_number(beans_added, "monotonic_seconds", "beans_added")
    exported_onset = _finite_number(first_crack, "monotonic_seconds", "first_crack_detected")
    payload = _mapping_field(first_crack, "payload", "first_crack_detected")
    absolute_onset = _finite_number(
        payload, "detected_at_monotonic_seconds", "first_crack_detected payload"
    )
    absolute_confirmation = _finite_number(
        payload, "confirmed_at_monotonic_seconds", "first_crack_detected payload"
    )
    state_onset = _finite_number(state, "first_crack_monotonic_seconds", "final state")
    model = _mapping_field(summary, "first_crack_model", "summary")
    confidence = _finite_number(model, "confidence", "summary first_crack_model")
    payload_confidence = _optional_finite_number(
        payload, "confidence", "first_crack_detected payload"
    )

    onset_to_confirmation = absolute_confirmation - absolute_onset
    confirmation_session_time = exported_onset + onset_to_confirmation
    onset_after_t0 = exported_onset - beans_added_time
    confirmation_after_t0 = confirmation_session_time - beans_added_time
    for name, value in {
        "onset_to_confirmation": onset_to_confirmation,
        "confirmation_session_time": confirmation_session_time,
        "onset_after_t0": onset_after_t0,
        "confirmation_after_t0": confirmation_after_t0,
    }.items():
        if not math.isfinite(value):
            _fail(f"non-finite derived {name}")
    if onset_to_confirmation < 0.0 or absolute_onset > absolute_confirmation:
        _fail("first-crack onset is after confirmation")
    if onset_after_t0 < 0.0:
        _fail("first-crack onset is before beans added")
    if exported_onset != state_onset:
        _fail("exported first-crack onset differs from final state")
    if not 3.82710390663442 <= confirmation_after_t0 <= 21.0:
        _fail(f"confirmation after beans added failed inclusive bounds: {confirmation_after_t0}")
    if confirmation_after_t0 >= 20.017:
        _fail(f"confirmation after beans added failed strict maximum: {confirmation_after_t0}")
    if confidence < 0.6:
        _fail(f"confirming confidence was below threshold: {confidence}")

    return {
        "confirmation": {
            "beans_added_monotonic_seconds": beans_added_time,
            "confirmation_session_monotonic_seconds": confirmation_session_time,
            "detected_at_monotonic_seconds": absolute_onset,
            "confirmed_at_monotonic_seconds": absolute_confirmation,
            "onset_to_confirmation_seconds": onset_to_confirmation,
            "confirmation_seconds_after_beans_added": confirmation_after_t0,
            "minimum_seconds_after_beans_added": 3.82710390663442,
            "maximum_seconds_after_beans_added": 21.0,
            "strict_maximum_seconds_after_beans_added": 20.017,
        },
        "onset": {
            "beans_added_event_count": beans_added_count,
            "first_crack_detected_event_count": first_crack_count,
            "exported_monotonic_seconds": exported_onset,
            "state_monotonic_seconds": state_onset,
            "onset_not_after_confirmation": absolute_onset <= absolute_confirmation,
            "export_state_consistent": exported_onset == state_onset,
            "onset_is_not_latency_gate": True,
        },
        "confidence": {
            "confirming_confidence": confidence,
            "minimum": 0.6,
            "payload_confidence": payload_confidence,
        },
        "observed_onset_after_t0": onset_after_t0,
    }


def _session_event_rows(jsonl_path: Path, session_id: str) -> list[dict[str, object]]:
    """Return event rows for the requested session from an exported JSONL file."""
    rows: list[dict[str, object]] = []
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            _fail(f"malformed JSON at line {line_number}: {error.msg}")
        if not isinstance(row, dict):
            _fail(f"JSONL row at line {line_number} must be an object")
        if row.get("session_id") == session_id and row.get("type") == "event":
            rows.append(cast(dict[str, object], row))
    return rows


def _single_session_event(
    events: list[dict[str, object]], kind: str
) -> tuple[dict[str, object], int]:
    """Return exactly one requested event and its session-scoped count."""
    matching = [event for event in events if event.get("kind") == kind]
    if len(matching) != 1:
        _fail(f"expected exactly one {kind} event for current session, found {len(matching)}")
    return matching[0], len(matching)


def _mapping_field(mapping: Mapping[str, object], field: str, context: str) -> Mapping[str, object]:
    """Return a required object field with a clear acceptance failure."""
    value = mapping.get(field)
    if not isinstance(value, dict):
        _fail(f"missing or invalid {context}.{field}")
    return cast(Mapping[str, object], value)


def _finite_number(mapping: Mapping[str, object], field: str, context: str) -> float:
    """Return a required finite numeric field without accepting booleans."""
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"missing or non-numeric {context}.{field}")
    numeric = float(value)
    if not math.isfinite(numeric):
        _fail(f"non-finite {context}.{field}")
    return numeric


def _optional_finite_number(
    mapping: Mapping[str, object], field: str, context: str
) -> float | None:
    """Return an optional finite numeric field without accepting invalid values."""
    if field not in mapping or mapping[field] is None:
        return None
    return _finite_number(mapping, field, context)


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
