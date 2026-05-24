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
  `coffee-roaster-mcp --version`, and verifies installed default config matches
  `mock disabled int8`.
- Updated `.github/workflows/release.yml` with the same built-wheel smoke in
  the release `build-package` job before artifacts are uploaded.
- Addressed PR review feedback by extracting the duplicated smoke logic into
  `.github/scripts/smoke_install_built_wheel.py`, which fails non-zero if the
  installed default config differs from `mock disabled int8`.
- Updated `docs/state/registry.md` and
  `docs/state/epics/coffee-roaster-mcp-v0.1.md` to mark E7-S2 complete and
  route the next story to E7-S3 Warp MCP client connection validation.

## Review Comparison

- CodeRabbit and Codex both identified the same substantive issue: the original
  workflow command printed `mock disabled int8` but did not assert it, so CI
  would still pass if the installed defaults regressed.
- CodeRabbit also raised two maintainability points: the config check was too
  long as a one-liner, and the smoke-install logic was duplicated across CI and
  release workflows.
- The fix addresses both reviewers by using one reusable Python smoke script
  from both workflows and making the installed default-config smoke an explicit
  assertion.

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
- PR review fix validation:
  - Thread-aware PR review fetch confirmed three unresolved actionable threads:
    two CodeRabbit threads and one Codex thread.
  - `./.venv/bin/python .github/scripts/smoke_install_built_wheel.py --venv-path /tmp/coffee-roaster-mcp-e7-s2-review-wheel-smoke`:
    initial sandboxed run failed to resolve dependencies because network access
    was blocked.
  - `./.venv/bin/python .github/scripts/smoke_install_built_wheel.py --venv-path /tmp/coffee-roaster-mcp-e7-s2-review-wheel-smoke`
    with approved network access: passed and printed `mock disabled int8`.
  - `./.venv/bin/python -m pytest tests/test_package_metadata.py tests/test_package.py::test_main_prints_help`:
    3 passed.
  - `./.venv/bin/python -m pytest`: 356 passed.
  - `./.venv/bin/python -m ruff check .`: passed after removing unused imports
    from the new helper script.
  - `./.venv/bin/python -m ruff format --check .`: 31 files already formatted.
  - `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations.
  - `git diff --check`: passed.
- Second CodeRabbit review fix validation:
  - New review `4353238457` identified that installed `--help` and `--version`
    checks asserted process success but not output content.
  - Updated `.github/scripts/smoke_install_built_wheel.py` to capture CLI
    output, assert help output contains expected help text, and assert version
    output matches `coffee-roaster-mcp X.Y.Z`.
  - `./.venv/bin/python .github/scripts/smoke_install_built_wheel.py --venv-path /tmp/coffee-roaster-mcp-e7-s2-output-review-wheel-smoke`
    with approved network access: passed, including asserted help output,
    version output, and default config output.
  - `./.venv/bin/python -m pytest tests/test_package_metadata.py tests/test_package.py::test_main_prints_help`:
    3 passed.
  - `./.venv/bin/python -m ruff check .`: passed.
  - `./.venv/bin/python -m ruff format --check .`: 31 files already formatted.
  - `./.venv/bin/python -m py_compile .github/scripts/smoke_install_built_wheel.py`:
    passed.

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

- Before review fixes: `121K used`.
- After review fixes: `280K used`.

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
