# E6-S6 MCP Registry Publishing Verification

This summary captures the E6-S6 implementation, validation state, and restart
context.

## Scope

Story: `E6-S6` / issue `#54`, run MCP Registry publishing verification spike.

Branch: `feature/54-mcp-registry-publishing-verification`

The implementation stayed inside the E6-S6 boundary:

- verify `server.json` against the current MCP Registry preview schema
- verify the PyPI README ownership marker for
  `io.github.syamaner/coffee-roaster-mcp`
- inspect and test the pinned `mcp-publisher` v1.7.9 flow non-destructively
- update the release workflow to validate `server.json` before authentication
  and publish
- document the live-publish decision point, prerequisites, expected outcome,
  and Registry preview risk

No live PyPI upload, live MCP Registry publish, TestPyPI rehearsal, hardware
validation, model training/export/sync, real microphone validation, or broad
release validation was performed.

## Verification Results

Official MCP Registry docs checked on 2026-05-24 still describe the Registry as
preview, use schema URI
`https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`,
support PyPI package entries with `registryType: pypi`, and verify PyPI package
ownership by looking for an `mcp-name: $SERVER_NAME` string in the package
README.

`server.json` currently declares:

- schema URI: `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`
- name: `io.github.syamaner/coffee-roaster-mcp`
- title: `RoastPilot`
- package registry type: `pypi`
- package identifier: `coffee-roaster-mcp`
- version: `0.1.0`
- runtime hint: `uvx`
- transport: `stdio`

README contains exactly one verification marker:
`<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.

Non-destructive live-service checks:

- downloaded the official `2025-12-11` JSON schema and validated
  `server.json` locally
- downloaded `mcp-publisher` v1.7.9 Linux amd64 and verified SHA-256
  `ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac`
- downloaded the same version's Darwin arm64 build to inspect runnable CLI
  behavior locally
- ran `./mcp-publisher validate server.json` against the preview Registry API:
  passed
- ran `./mcp-publisher login github-oidc` locally: failed as expected because
  `ACTIONS_ID_TOKEN_REQUEST_TOKEN` is only available inside GitHub Actions with
  `id-token: write`
- checked `https://pypi.org/pypi/coffee-roaster-mcp/json`: `Not Found`
- checked
  `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`:
  no current listing

## Implementation Summary

`.github/workflows/release.yml` now runs
`./mcp-publisher validate server.json` in the `publish-mcp-registry` job after
installing the verified publisher binary and before `login github-oidc`.

The final live Registry mutation command is now documented and tested as:

```bash
./mcp-publisher publish server.json
```

`docs/release.md` now includes an MCP Registry verification section with:

- schema and metadata checks
- PyPI README marker check
- publisher checksum check
- non-destructive validate command
- live publish prerequisites
- expected Registry search result
- preview Registry risk and retry guidance

`tests/test_release_workflow.py` pins the validation step and release runbook
text.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E6-S6` complete and sets
  the active story to `E6-S7`.
- `docs/state/registry.md` says the next story is `E6-S7: document install and
  hardware setup`.
- Follow-up GitHub issue #135 now tracks `E6-S8: Execute live PyPI and MCP
  Registry publish` in Epic 6 for the controlled live publish after PyPI
  publication and verification-marker checks.

## Decision Point

Stop before executing the first destructive command:

```bash
./mcp-publisher publish server.json
```

Prerequisites before running it:

- production PyPI contains the matching `coffee-roaster-mcp` package version
- the published PyPI long description includes
  `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`
- the GitHub `release` environment has been approved
- `./mcp-publisher validate server.json` has passed

Expected outcome: the MCP Registry accepts the metadata and its search API
returns a listing for `io.github.syamaner/coffee-roaster-mcp` with PyPI package
identifier `coffee-roaster-mcp` and stdio transport.

Risk: the Registry is still preview. Schema validation, package verification,
listing behavior, or stored data can change or reset before general
availability. A failure after PyPI publish leaves PyPI live without Registry
discoverability and should be retried only after checking Registry status and
confirming no partial version entry was created.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_release_workflow.py tests/test_server_json.py tests/test_readme.py`:
  12 passed

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

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E6-S6 should
be checked first. If it has merged, verify issue #54 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S7 from updated
main on the appropriate `feature/55-...` branch after reading the registry,
active epic, this summary, and the GitHub issue for E6-S7. Keep E6-S7 scoped to
install and hardware setup documentation unless the issue explicitly expands
the work. E6-S8 is now tracked separately as issue #135 for the later live PyPI
and MCP Registry publish.
