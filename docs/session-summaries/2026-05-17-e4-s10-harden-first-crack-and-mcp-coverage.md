# E4-S10 Harden First-Crack And MCP Coverage Session

## Scope

This session resumed after `PR #101` for `E4-S9` was squashed and merged, and
issue `#39` was closed. Work started from updated `main` on branch
`feature/99-harden-first-crack-and-mcp-coverage-before-next-epic` for issue
`#99`, `E4-S10: Harden first-crack and MCP coverage before next epic`.

The original story goal was targeted coverage hardening before Epic 5. During
review of the MCP interface, we identified that this was not only a coverage
gap: the installed Claude-local operational MCP path was not yet complete.
That produced a planning correction and a new inserted Epic 4.1 before Epic 5.

## Context Usage

Session usage snapshot supplied by the operator when this summary was requested:

- Context window: `28% left (190K used / 258K)`
- 5h limit: `87% left`, resets `22:35`
- Weekly limit: `95% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `02:57 on 18 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `21:57 on 24 May`

## Pre-Story Verification

Before starting E4-S10:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the E4-S9 merge.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s9-integrate-first-crack-with-session-timeline.md`,
  and GitHub issue `#99`.
- Created branch
  `feature/99-harden-first-crack-and-mcp-coverage-before-next-epic`.

## Implementation

Updated:

- `tests/test_exports.py`
- `tests/test_mcp_server.py`
- `pyproject.toml`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`
- `docs/state/github-issues.md`

Coverage hardening added:

- Added direct in-process FastMCP tool coverage for the registered MCP tool
  bodies. This complements the existing stdio subprocess smoke tests, which are
  useful end-to-end checks but do not contribute to local coverage for
  `mcp_server.py`.
- Covered current MCP-facing behavior for server info, runtime config, session
  start/state, heat/fan controls, beans added, manual first crack, drop,
  cooling, export, audio-mode bootstrap safety reporting, missing active session
  errors, disabled manual override errors, and unknown session lookup.
- Added snapshot export coverage proving automatic first-crack detector metadata
  is preserved in current JSONL and CSV event exports.
- Documented that `summary.json` currently carries first-crack timestamps and
  metrics, while final schema completeness is deferred to Epic 5.
- Added `fail_under = 90` to coverage configuration.

Coverage result:

- Before E4-S10 coverage hardening, local package coverage was `86%`.
- After E4-S10 coverage hardening, local package coverage is `91.73%`.
- `mcp_server.py` improved from `55%` to `97%`.
- `exports.py` improved from `43%` to `96%`.

## MCP Operational Gap Review

After E4-S10 coverage was implemented, we reviewed whether installing the MCP
server locally in Claude would allow a real operational roast:

- Claude should be able to start a roast.
- Claude should be able to adjust the configured device.
- Claude should be able to read current device and session state.
- Claude should be able to know whether first crack has happened.

The answer was: not fully yet.

Current state:

- MCP heat/fan/drop/cooling tools are covered at the existing mock/session
  boundary.
- E3 validated the Hottop driver boundary, but normal MCP heat/fan/drop/cooling
  tools are not yet wired to live configured driver commands.
- E4 built released-artifact resolution, audio input, detector adapter, and
  detector-to-session integration.
- E4 does not yet include a released-artifact ONNX detector backend or a
  session-owned audio/detector runtime loop.

Planning correction:

- Added new GitHub issue `#103`: `Epic 4.1: Operational MCP runtime for device
  control and first-crack status`.
- Added story issues:
  - `#104` `E4.1-S1: Wire MCP roast-control tools to configured driver`
  - `#105` `E4.1-S2: Expose current roaster device state through MCP`
  - `#106` `E4.1-S3: Add released-artifact ONNX first-crack detector backend`
  - `#107` `E4.1-S4: Start first-crack detection runtime with roast sessions`
  - `#108` `E4.1-S5: Add MCP operational readiness tests and docs`
- Updated `docs/state/github-issues.md`, `docs/state/registry.md`, and
  `docs/state/epics/coffee-roaster-mcp-v0.1.md` to insert Epic 4.1 before Epic
  5.

## MCP Tool Semantics Clarified

The operator asked whether `mark_beans_added`, `mark_first_crack`, and
`start_cooling` should be exposed if the runtime should mark those events
internally.

Decision captured in GitHub issues and local state:

- `mark_beans_added` remains exposed as an explicit T0 override.
- The primary future automatic T0 path is internal runtime detection when
  enabled, for example a qualifying bean-temperature drop.
- `mark_first_crack` remains exposed only as an explicit manual override when
  configuration allows it.
- The primary audio-mode first-crack path is internal detector confirmation.
- `drop_beans` is the normal agent/operator command that should trigger roaster
  drop/cooling behavior and record the relevant timeline events.
- `start_cooling` remains exposed as an advanced/manual recovery control, not
  the normal Claude roast flow.
- `get_roast_state` must expose event timestamps and status for beans added,
  first crack, bean drop, cooling started, and cooling stopped.

GitHub issues updated with these semantics:

- `#103` Epic 4.1 operational model
- `#104` E4.1-S1 control semantics
- `#105` E4.1-S2 state/timestamp/status requirements
- `#107` E4.1-S4 detector runtime/manual override semantics
- `#108` E4.1-S5 readiness tests/docs

## Pull Request

Opened `PR #102`: <https://github.com/syamaner/coffee-roaster-mcp/pull/102>

PR branch:

- `feature/99-harden-first-crack-and-mcp-coverage-before-next-epic`

Commits on the branch before this summary:

- `993ff5f` - `test: harden first crack and mcp coverage`
- `f01dd4f` - `docs: add operational mcp epic`
- `517f2bb` - `docs: clarify operational mcp tool semantics`

PR status when this summary was written:

- state: open
- draft: false
- mergeable: true
- head: `517f2bbc9ae2ace3abf9cf1af8e93d1e3d31baa2`
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed

Issue `#99` remains open and should close when PR #102 is merged through
`Closes #99`.

## Validation

Local validation run:

- Ran `./.venv/bin/python -m pytest tests/test_exports.py tests/test_mcp_server.py tests/test_first_crack_integration.py tests/test_package.py`:
  `27 passed`.
- Ran `./.venv/bin/python -m pytest`: `241 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `241 passed`, required coverage `90.0%` reached, total coverage `91.73%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

GitHub Actions validation on PR #102:

- `Build Package`: passed.
- `Checks`: passed.

## Handoff Notes

After PR #102 merges:

1. Sync `main`.
2. Verify PR #102 is merged and issue `#99` is closed.
3. Read `AGENTS.md`, `docs/state/registry.md`,
   `docs/state/epics/coffee-roaster-mcp-v0.1.md`, this summary, and GitHub
   issue `#104`.
4. Begin E4.1-S1 from updated `main` on branch
   `feature/104-wire-mcp-roast-control-tools-to-configured-driver`.
5. Keep E4.1-S1 scoped to wiring current MCP roast-control tools to the
   configured driver boundary. Preserve mock-safe defaults, one-session store
   semantics, fail-closed safety behavior, E3 Hottop validation boundaries, and
   no-live-hardware CI.
6. Do not add automatic first-crack detector startup, ONNX detector runtime,
   rolling telemetry metrics, final log schemas, model training, ONNX export,
   Hugging Face sync, or broad release validation in E4.1-S1.
