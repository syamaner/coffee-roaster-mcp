# E6-S5 Release Workflow

This summary captures the E6-S5 implementation, validation state, and restart
context.

## Scope

Story: `E6-S5` / issue `#53`, add release workflow.

Branch: `feature/53-release-workflow`

The implementation stayed inside the E6-S5 boundary:

- add a guarded GitHub Actions release workflow
- run tests, lint, format, typecheck, CLI smoke checks, metadata validation, and
  package build before any publish step
- support a manual release dry run that proves checks and package artifacts
  without uploading to production PyPI
- publish to PyPI through Trusted Publishing on tag releases after `release`
  environment approval
- publish MCP Registry metadata with `mcp-publisher` GitHub OIDC only after the
  PyPI publish job succeeds
- document operator prerequisites for PyPI, Trusted Publishing, release
  environment approvals, protected tags, TestPyPI status, and token fallback

No live PyPI upload, live MCP Registry publish, TestPyPI rehearsal, hardware
validation, model training/export/sync, real microphone validation, or broad
release validation was performed.

## Implementation Summary

`.github/workflows/release.yml` now defines the release path:

- `workflow_dispatch` with `dry_run: true` builds and validates without upload.
- `push` tags matching `v*` run the live release path.
- `checks` runs pytest, ruff, format check, pyright, and CLI smoke checks.
- `validate-release-metadata` requires a tag such as `v0.1.0` to match
  `coffee_roaster_mcp.__version__` and both `server.json` version fields.
- `build-package` builds and uploads wheel/source distribution artifacts.
- `release-dry-run` confirms wheel and source distribution artifacts exist and
  does not run upload actions.
- `publish-pypi` uses the `release` GitHub environment, `id-token: write`, and
  a commit-pinned `pypa/gh-action-pypi-publish` action.
- `publish-mcp-registry` depends on `publish-pypi`, uses the same `release`
  environment and OIDC permission, installs the pinned `mcp-publisher` v1.7.9
  Linux amd64 asset after SHA-256 verification, authenticates with
  `github-oidc`, and publishes `server.json`.

`docs/release.md` documents the operator prerequisites before live publishing:

- PyPI account and project ownership/reservation for `coffee-roaster-mcp`
- PyPI 2FA and recovery-code handling
- Trusted Publishing configuration for repository `syamaner/coffee-roaster-mcp`,
  workflow file `release.yml`, environment `release`, and job `publish-pypi`
- GitHub `release` environment approvals
- protected `v*` tag rules
- TestPyPI not enabled in this workflow
- token fallback with exact environment secret name `PYPI_API_TOKEN`

`tests/test_release_workflow.py` pins the release workflow and runbook contract:

- tag and manual dry-run triggers
- build/test before publish ordering
- PyPI publish before MCP Registry publish ordering
- `release` environment and `id-token: write` permissions for live publish jobs
- commit-pinned GitHub Actions refs and disabled checkout credential persistence
- PyPI Trusted Publishing action
- pinned and SHA-256 verified `mcp-publisher` install
- MCP Registry `mcp-publisher login github-oidc` and `publish --file=server.json`
- required operator prerequisite runbook text

## Review Follow-Up

Pre-review context token usage snapshot: `386K used`

Post-review context token usage snapshot: `495K used`

Final context token usage snapshot after all review follow-ups: `769K used`

CodeRabbit and Codex review comments raised overlapping supply-chain hardening
concerns:

- Add `persist-credentials: false` to every checkout step.
- Pin mutable GitHub Actions refs to immutable commit SHAs.
- Stop downloading `mcp-publisher` from `releases/latest`.
- Verify the downloaded `mcp-publisher` asset before executing it.
- Update workflow tests so the PyPI publish action expectation requires an
  immutable 40-character SHA ref.

The review concerns were valid and addressed locally:

- All `actions/checkout`, `actions/setup-python`, `actions/upload-artifact`,
  `actions/download-artifact`, and `pypa/gh-action-pypi-publish` references are
  now pinned to commit SHAs.
- Every checkout step sets `persist-credentials: false`.
- `mcp-publisher` is pinned to `v1.7.9` and the Linux amd64 release asset is
  checked against SHA-256
  `ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac` before
  extraction.
- `tests/test_release_workflow.py` now verifies pinned action refs, checkout
  credential hardening, and the pinned/checksum-verified publisher install.
- Follow-up CodeRabbit metadata-validation comments were also addressed:
  missing `__version__` and missing or empty `server.json.packages` now fail
  with explicit release-operator error messages instead of generic Python
  exceptions. The first package entry is also validated before reading its
  `version` field.
- The final CodeRabbit review thread on PR #133 asked for an explicit
  `packages[0]` shape check. Commit `73b227b` addressed it by requiring the
  first package entry to be an object with a string `version` field before the
  workflow reads `package["version"]`; CodeRabbit marked the thread resolved.
- GitHub checks on commit `73b227b` passed for `Build Package` and `Checks`.
  CodeRabbit was still processing when the final token usage snapshot was
  recorded.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E6-S5` complete and sets
  the active story to `E6-S6`.
- `docs/state/registry.md` says the next story is `E6-S6: run the MCP Registry
  publishing verification spike`.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_release_workflow.py`: 7 passed

Full validation:

- `./.venv/bin/python -m pytest`: 355 passed
- `./.venv/bin/python -m build`: built
  `coffee_roaster_mcp-0.1.0.tar.gz` and
  `coffee_roaster_mcp-0.1.0-py3-none-any.whl`
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E6-S5 should
be checked first. If it has merged, verify issue #53 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S6 from updated
main on the appropriate `feature/54-...` branch after reading the registry,
active epic, this summary, and the GitHub issue for E6-S6. Keep E6-S6 scoped to
the MCP Registry publishing verification spike unless the issue explicitly
expands the work.
