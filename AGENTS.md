# AGENTS.md - RoastPilot

Project rules and context for coding agents working in this repository.

## Rules

- Python 3.11+ with full type hints on all public functions and methods.
- Google-style docstrings for public modules, classes, functions, and methods.
- `ruff check`, `ruff format --check`, `pyright`, and `pytest` must pass before marking implementation complete once the dev environment is available.
- All runtime and dev dependencies must be declared in `pyproject.toml`. Never install ad-hoc dependencies without adding them to project metadata.
- Keep roaster hardware control conservative. Heat, fan, drop, cooling, and emergency stop behavior require explicit tests or manual validation notes.
- The default roaster driver is `mock`. Default first-crack mode is `disabled`.
- Model training, ONNX export, Hugging Face model sync, model cards, and dataset cards stay in `coffee-first-crack-detection`.
- This repository consumes released Hugging Face artifacts only.
- Do not commit model weights, audio files, roast logs, serial captures, `.env` files, or local IDE folders, except for the single small derived E7-S5a replay fixture under `tests/fixtures/audio/`.
- One PR per story, branch: `feature/{issue-number}-{slug}`.
- Before starting a task: read `docs/state/registry.md`, open the active epic file, then check the GitHub issue.

## Quick Commands

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

### Test

```bash
python -m pytest
```

### Lint And Format

```bash
python -m ruff check .
python -m ruff format --check .
```

### Typecheck

```bash
python -m pyright
```

### CLI Smoke

```bash
coffee-roaster-mcp --help
coffee-roaster-mcp --version
```

### Mock-Safe Bootstrap Smoke

`E2-S1` now provides `coffee-roaster-mcp serve` with a minimal bootstrap-safe tool list. Use this command to verify the default local path stays on the mock driver with first-crack detection disabled from a guaranteed-empty temporary directory:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

## Repo-local Workflows

- `.claude/skills/code-quality`: run before marking a story complete or opening a PR.
- `.claude/skills/mcp-dev`: use for local setup, stdio MCP startup, and scaffold-level validation while the runtime is still landing.
- `.claude/skills/mock-roast`: use for the current mock-safe bootstrap path and early stdio MCP checks before the full roast-session flow lands.
- `.claude/skills/hottop-validation`: use for guarded manual Hottop validation planning and release-readiness review.
- `.claude/skills/release-registry`: use for staged PyPI and MCP Registry release preparation without implying unimplemented release automation exists.

## Codebase Architecture

```text
src/coffee_roaster_mcp/
  __init__.py     - package version
  cli.py          - console entrypoint
  config.py       - typed configuration loading from defaults, YAML, and env vars
  mcp_server.py   - FastMCP stdio entrypoint and bootstrap-safe tools
  session.py      - authoritative roast session lifecycle, event timeline, and active-session owner
tests/
  test_package.py - package and CLI smoke coverage
  test_config.py  - config defaults, YAML, env override, and validation coverage
  test_session.py - roast session lifecycle and active-session ownership coverage
docs/state/
  registry.md     - active project state pointer
  epics/          - durable epic and story state
docs/plans/
  coffee-roaster-mcp-v0.1-overall-plan.md - implementation-grade v0.1 plan
```

## Key Design Decisions

- RoastPilot will be one local stdio MCP server for v0.1.
- One authoritative roast session will own timing, telemetry, first-crack events, metrics, logs, and roaster control.
- `beans_added_at` is T0. Auto-T0 detection is disabled by default.
- First crack is recorded once by automatic detection flow. Manual override is enabled by default and can be disabled by config.
- Roaster support goes through a `RoasterDriver` abstraction. The mock driver comes first; Hottop support requires hardware validation.
- First-crack models are consumed from `syamaner/coffee-first-crack-detection`.
- ONNX INT8 is the default runtime precision. ONNX FP32 is supported by config for validation and comparison.

## Epic State Management

Before starting a story:

1. Read `docs/state/registry.md`.
2. Open the active epic file listed in the registry.
3. Read the GitHub story issue and any comments.
4. Confirm acceptance criteria and current risks.
5. Work on a branch named `feature/{issue-number}-{slug}`.

After completing a story:

1. Run required checks.
2. Update story status in the active epic file.
3. Update Active Context and decision notes when behavior changed.
4. Add validation notes to the epic file.
5. Comment on the GitHub story issue with what changed and how it was tested.
6. Open a PR referencing the story issue.

## Hardware Safety Notes

- Hottop command-loop behavior, packet format, temperature units, drop behavior, cooling behavior, and emergency stop require explicit validation before a hardware-ready release label.
- Unsafe or uncertain hardware behavior should fail closed: heat off, record a fault event, and preserve enough state for diagnosis.
- Do not mark hardware stories complete from mock tests alone.

## Storage Rules

- Do not commit generated logs under `logs/`.
- Do not commit audio recordings, model artifacts, ONNX files, or raw serial captures. The only current audio exception is the derived, trimmed, retimestamped E7-S5a labelled WAV replay fixture under `tests/fixtures/audio/`; raw recordings and broad datasets remain excluded.
- Large or generated artifacts belong in Hugging Face Hub, release artifacts, or ignored local directories depending on the artifact type.
