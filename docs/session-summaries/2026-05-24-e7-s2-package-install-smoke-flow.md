# E7-S2 Package Install Smoke Flow

This summary captures the E7-S2 package install smoke validation story,
implementation scope, validation evidence, and restart context.

## Scope

Story: `E7-S2` / issue `#57`, test the package install smoke flow.

Branch: `feature/57-package-install-smoke-flow`

The work stayed inside package install smoke validation:

- build the package from the repository
- install the built wheel into a clean environment
- run installed CLI smoke checks
- verify installed default config remains mock-safe
- add focused CI and release workflow smoke coverage for built wheels
- update durable state and handoff notes

No hardware validation, Warp MCP validation, ChatGPT MCP validation, model
training/export/sync, real microphone validation, or live release publishing
was performed.

## Implementation Summary

- Updated `.github/workflows/ci.yml` so the `build-package` job now creates a
  clean virtual environment after `python -m build`, installs the built wheel,
  runs installed `coffee-roaster-mcp --help`, runs installed
  `coffee-roaster-mcp --version`, and verifies installed default config prints
  `mock disabled int8`.
- Updated `.github/workflows/release.yml` with the same built-wheel install
  smoke in the release `build-package` job before artifacts are uploaded.
- Updated `docs/state/registry.md` and
  `docs/state/epics/coffee-roaster-mcp-v0.1.md` to mark E7-S2 complete and
  route the next story to E7-S3 Warp MCP client connection validation.

## Validation

Preflight:

- PR #138 was merged.
- Issue #56 was closed.
- `main` was fast-forwarded to
  `30a1887c07d50f8a968cc155f22718d54d57ef69`.
- Branch `feature/57-package-install-smoke-flow` was created from updated
  `main`.

Commands run:

- `./.venv/bin/python -m build`: successfully built
  `coffee_roaster_mcp-0.1.0.tar.gz` and
  `coffee_roaster_mcp-0.1.0-py3-none-any.whl`.
- `python3.11 -m venv /tmp/coffee-roaster-mcp-e7-s2-wheel-smoke`: passed.
- `/tmp/coffee-roaster-mcp-e7-s2-wheel-smoke/bin/python -m pip install dist/coffee_roaster_mcp-0.1.0-py3-none-any.whl`:
  initial sandboxed run failed to resolve dependencies because network access
  was blocked.
- `/tmp/coffee-roaster-mcp-e7-s2-wheel-smoke/bin/python -m pip install dist/coffee_roaster_mcp-0.1.0-py3-none-any.whl`
  with approved network access: installed successfully.
- `/tmp/coffee-roaster-mcp-e7-s2-wheel-smoke/bin/coffee-roaster-mcp --help`:
  passed.
- `/tmp/coffee-roaster-mcp-e7-s2-wheel-smoke/bin/coffee-roaster-mcp --version`:
  `coffee-roaster-mcp 0.1.0`.
- `/tmp/coffee-roaster-mcp-e7-s2-wheel-smoke/bin/python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"`:
  `mock disabled int8`.
- `./.venv/bin/python -m pytest tests/test_package_metadata.py tests/test_package.py::test_main_prints_help`:
  3 passed.
- `./.venv/bin/python -m pytest`: 356 passed.
- `./.venv/bin/python -m ruff check .`: passed.
- `./.venv/bin/python -m ruff format --check .`: 30 files already formatted.
- `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations.
- `./.venv/bin/coffee-roaster-mcp --help`: passed.
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- GitHub Actions CI run `26368815098`: passed.
  - `Checks`: success.
  - `Build Package`: success, including the new
    `Smoke install built wheel` step.

## Risks And Notes

- The CI and release smoke installs the built wheel and resolves runtime
  dependencies from PyPI, so package-index or network outages can fail this
  validation even when the wheel itself is valid.
- This story proves built local wheel installation and installed CLI/default
  config smoke checks. It does not run Warp MCP client validation, Hottop
  hardware validation, real microphone input, or live release publishing.
- The local smoke validated the wheel path. The sdist was built successfully
  but was not separately installed in a clean environment because issue #57
  accepts a built wheel install smoke.

## Usage Snapshot

- Token usage: not provided.

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. E7-S2 is complete
on branch `feature/57-package-install-smoke-flow`: CI and release package-build
jobs now build distributions, install the built wheel into a clean environment,
run installed CLI smoke checks, and verify installed default config remains
`mock disabled int8`. After the E7-S2 PR merges and issue #57 closes, sync
`main` and route next work to E7-S3 Warp MCP client connection validation unless
the operator selects a different story. Do not run hardware validation,
ChatGPT MCP validation, model training/export/sync, real microphone validation,
or live release publishing unless the selected story explicitly requires it.
