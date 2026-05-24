# E6-S7 Install And Hardware Setup Docs

This summary captures the E6-S7 documentation update, validation state, and
restart context.

## Scope

Story: `E6-S7` / issue `#55`, document install and hardware setup.

Branch: `feature/55-install-and-hardware-setup-docs`

The implementation stayed inside the E6-S7 documentation boundary:

- document mock install from a local clone and the later PyPI package path
- document `coffee-roaster-mcp.yaml` and supported environment overrides
- document Hottop configuration and guarded validation commands
- document Hugging Face released-model configuration
- document offline model directory layout
- document runtime and snapshot log output paths
- cross-reference the setup guide from README and release readiness docs

No live PyPI publish, live MCP Registry publish, hardware validation, model
training/export/sync, real microphone validation, or broad release validation
was performed.

## Implementation Summary

Added `docs/install-and-hardware-setup.md` as the dedicated setup runbook for
operators and release readiness. The guide covers:

- mock-safe defaults and local editable install
- the planned installed-package command path after PyPI publication
- `coffee-roaster-mcp.yaml` defaults and environment overrides
- Hottop serial configuration, supervised-operation cautions, and guarded
  `hottop-validate` commands
- released Hugging Face ONNX detector configuration for audio mode
- offline `first_crack.local_model_dir` layout using repository-relative
  artifact paths
- log output under `{logging.log_dir}/roasts/{session_id}/`, including
  `roast.jsonl`, `roast.csv`, and `summary.json`

Updated README to link the setup guide from the install and configuration
sections, and corrected stale wording that implied append-only telemetry writers
and final log schemas had not landed.

Updated `docs/release.md` so live release operators review the setup guide
before publishing and before any hardware-ready labeling.

Added focused documentation coverage in `tests/test_readme.py` for the E6-S7
required topics and the README/release cross-references.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E6-S7` complete and
  sets the active story to `E6-S8`.
- `docs/state/registry.md` records the setup guide and says the next story is
  `E6-S8: Execute live PyPI and MCP Registry publish`.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_readme.py tests/test_release_workflow.py`:
  9 passed

Full validation:

- `./.venv/bin/python -m pytest`: 356 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Usage Snapshot

- Token usage: `261K used`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E6-S7 should
be checked first. If it has merged, verify issue #55 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S8 from updated
main on the appropriate `feature/135-...` branch after reading the registry,
active epic, this summary, and GitHub issue #135. Keep E6-S8 scoped to the
controlled live PyPI and MCP Registry publish, and do not conflate it with
hardware validation, model training/export/sync, or real microphone validation.
