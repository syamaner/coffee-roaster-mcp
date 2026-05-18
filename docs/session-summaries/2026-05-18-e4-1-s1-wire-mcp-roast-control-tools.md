# E4.1-S1 Wire MCP Roast-Control Tools Session

## Scope

This session resumed after `PR #102` for `E4-S10` was squashed and merged, and
issue `#99` was closed. Work started from updated `main` on branch
`feature/104-wire-mcp-roast-control-tools-to-configured-driver` for issue
`#104`, `E4.1-S1: Wire MCP roast-control tools to configured driver`.

The story goal is to wire current MCP roast-control tools to the configured
`RoasterDriver` boundary while preserving the mock default, Epic 2 one-session
store boundary, MCP semantics, fail-closed safety behavior, coverage workflow,
and Epic 3 Hottop validation boundary.

## Context Usage

Initial session usage snapshot supplied by the operator when this summary was
first requested:

- Context window: `39% left (161K used / 258K)`
- 5h limit: `97% left`, resets `20:10`
- Weekly limit: `95% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `23:06`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `18:06 on 25 May`
- Warning from status output: limits may be stale; run `/status` again shortly.

Latest session usage snapshot supplied by the operator after the second review
batch was fixed and before fixing the third review batch:

- Context window: `18% left (214K used / 258K)`
- 5h limit: `96% left`, resets `20:10`
- Weekly limit: `94% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `00:21 on 19 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `19:21 on 25 May`
- Warning from status output: limits may be stale; run `/status` again shortly.

## Pre-Story Verification

Before starting E4.1-S1:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the E4-S10
  merge.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s10-harden-first-crack-and-mcp-coverage.md`,
  and GitHub issue `#104`.
- Created branch
  `feature/104-wire-mcp-roast-control-tools-to-configured-driver`.

## Implementation

Updated:

- `src/coffee_roaster_mcp/mcp_server.py`
- `src/coffee_roaster_mcp/session.py`
- `src/coffee_roaster_mcp/drivers.py`
- `tests/test_mcp_server.py`
- `tests/test_package.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior implemented:

- `start_roast_session` now calls the configured driver `connect()` before
  creating a session, preserving the mock default and requiring explicit Hottop
  configuration for live hardware.
- MCP `set_heat`, `set_fan`, `drop_beans`, `start_cooling`, and `stop_cooling`
  now call the configured `RoasterDriver` boundary and mirror the returned
  normalized control state into the authoritative session snapshot.
- The mock driver `drop_beans` path now matches the normal operational
  drop/cooling transition: heat off, fan `100%`, cooling on.
- `drop_beans` remains the normal agent/operator path for drop plus cooling
  transition. `start_cooling` remains an explicit advanced/manual recovery
  control and is not the normal Claude roast flow.
- Invalid drop, cooling-start, and cooling-stop phase calls were initially
  guarded before driver commands.

## Pull Request

Opened `PR #109`: <https://github.com/syamaner/coffee-roaster-mcp/pull/109>

PR branch:

- `feature/104-wire-mcp-roast-control-tools-to-configured-driver`

Earlier commit on the branch when this summary was first requested:

- `e0c063f` - `feat: wire mcp controls to roaster driver`

Latest pushed commit before the third review batch is fixed:

- `3f17efb` - `feat: wire mcp controls to roaster driver`

PR status before the latest unresolved review items were fixed:

- state: open
- draft: false
- mergeable: true
- GitHub Actions after commit `e0c063f`:
  - `Build Package`: passed
  - `Checks`: passed

Issue `#104` remains open and should close when PR #109 is merged through
`Closes #104`.

## Validation

Local validation before the latest unresolved review items:

- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `245 passed`, required coverage `90.0%` reached, total coverage `91.24%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

## Review Cycle So Far

First Codex review on PR #109:

- Review URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/109#pullrequestreview-4312218975>
- Reviewed commit: `c43f05f`.
- Two P1 comments were actionable:
  - Duplicate `drop_beans` calls could resend the physical drop/cooling command.
  - Drop phase validation could race a concurrent session mutation before the
    driver command.
- The fix moved duplicate-drop detection and drop phase validation into a
  store-owned drop method. After pushing commit `e0c063f`, both review threads
  became resolved and outdated on GitHub.

Second Codex review on PR #109:

- Review URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/109#pullrequestreview-4312498491>
- Reviewed commit: `e0c063f`.
- Three unresolved comments were inspected, judged relevant, and fixed:
  - P1: `set_heat` and the other driver-backed controls still use a
    check-then-act pattern where another tool could stop or fault the session
    between `_require_active_session()` and the driver command.
  - P1: `record_driver_drop_snapshot()` now runs `driver_drop()` while holding
    the session-store lock, which can block emergency stop if hardware I/O hangs.
  - P2: repeated `start_cooling` calls resend the driver command even though
    the cooling event is already recorded.

The review-fix direction was not to run all driver I/O under the session lock.
Instead, the session store now owns non-emergency driver command reservations.
Tools reserve the active session under the store lock, run hardware I/O outside
the lock, then complete only if the same reservation is still active.

Review fixes added:

- `set_heat`, `set_fan`, `drop_beans`, `start_cooling`, and `stop_cooling` now
  reserve non-emergency driver commands before calling the driver.
- `drop_beans` duplicate retries return the existing `beans_dropped` event and
  do not call the driver again.
- `start_cooling` duplicate retries return the existing `cooling_started` event
  and do not call the driver again.
- `drop_beans` driver I/O no longer runs while holding the session-store lock,
  so emergency stop can acquire the store lock while a drop command is blocked.
- `emergency_stop` cancels pending non-emergency driver command reservations
  before running the fail-closed driver safety call.
- If a non-emergency driver command finishes after its reservation was canceled,
  the code reapplies driver emergency stop and surfaces a lifecycle error
  instead of updating the authoritative session state.

Validation after these review fixes:

- Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_package.py::test_stdio_server_supports_basic_mock_roast_tool_flow tests/test_session.py`:
  `47 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `247 passed`, required coverage `90.0%` reached, total coverage `90.28%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

After pushing these fixes, the three review threads from review `4312498491`
became outdated on GitHub.

Third Codex review on PR #109:

- Review trigger/comment URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/109#issuecomment-4480605392>
- Review URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/109#pullrequestreview-4312706674>
- Reviewed commit: `3f17efb`.
- Three fresh unresolved comments were inspected and judged relevant:
  - P2: `start_roast_session` still has a check-then-act sequence
    (`get_active_session()`, then driver `connect()`, then
    `start_session_snapshot()`), so concurrent starts can both invoke
    `connect()` before one fails to own the session.
  - P2: `stop_cooling` passes pre-command `session.cooling_on` instead of
    `driver_state.cooling_on` when completing the reserved command. This can
    let MCP state claim cooling stopped even if the driver reports cooling still
    active.
  - P1: stale-command fail-closed handling currently calls global driver
    emergency stop without scoping it to the same active session. If session A
    is stopped/faulted, session B starts, and A's stale command completes, this
    can interfere with B without recording a B fault.

These latest three review items have not been fixed yet. They are the next
work items before PR #109 should merge.

## Handoff Notes

Continue on branch
`feature/104-wire-mcp-roast-control-tools-to-configured-driver`.

Before making more code changes:

1. Re-read the latest thread-aware review state for PR #109.
2. Focus on the three unresolved review threads from review `4312706674`.
3. Keep any follow-up scoped to E4.1-S1: configured-driver MCP control wiring and
   safety/idempotency around those controls.
4. Do not add automatic first-crack detector startup, released-artifact ONNX
   detector runtime, auto-T0 detection, rolling telemetry metrics, final log
   schemas, model training, ONNX export, Hugging Face sync, real microphone
   validation, or broad release validation.
5. If additional review comments appear, rerun the normal validation suite and
   update PR #109 plus durable state if validation counts or behavior notes
   change.
