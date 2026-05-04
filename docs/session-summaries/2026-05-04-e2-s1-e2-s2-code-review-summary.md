# Code Review Summary: E2-S1 And E2-S2

Date: 2026-05-04

Repository: `syamaner/coffee-roaster-mcp`

## Scope

This summary captures the code review findings and fixes for:

- PR `#70` - `Implement stdio MCP server entrypoint`
- PR `#71` - `Implement RoastSession lifecycle`

The goal is to preserve the review history in a compact durable form, especially the behavioral issues that changed code shape rather than just tests.

## PR #70: E2-S1 Review Summary

Story:

- `E2-S1`
- issue `#16`

PR outcome:

- merged

### Main review themes

1. MCP startup test determinism
2. CLI no-arg behavior
3. bootstrap-safe semantics
4. runtime-vs-config transport accuracy
5. timeout and environment hardening in MCP smoke tests
6. blanket type-suppression cleanup

### Important review findings and fixes

Deterministic stdio smoke startup:

- Review found the stdio test inherited local `COFFEE_*` overrides and repo-root config state.
- Fix:
  - temp working directory
  - scrubbed `COFFEE_*` env vars
  - later narrowed to a minimal selected subprocess environment

No-arg CLI behavior:

- Review found `coffee-roaster-mcp` with no subcommand silently returned success without output.
- Fix:
  - `main([])` now prints help and returns `0`
  - tests updated to assert the help output

Bootstrap-safe manual mode:

- Review found `bootstrap_safe` incorrectly returned `False` for mock + `first_crack.mode=manual`
- Fix:
  - `manual` now counts as bootstrap-safe
  - MCP-level smoke coverage added for public `get_server_info`

Transport reporting:

- Review found `get_server_info.transport` reported config transport rather than actual runtime transport
- Fix:
  - runtime transport moved into `ServerContext`
  - public response now reports real runtime transport

MCP smoke test hang risk:

- Review found no timeout around `initialize`, `list_tools`, and `call_tool`
- Fix:
  - wrapped these awaits in `asyncio.wait_for(...)`

Pyright suppression scope:

- Review found blanket file-level suppressions in runtime and test modules
- Fix:
  - removed module-wide suppressions
  - kept targeted ignores only for `@mcp.tool()` decorator interaction
  - used narrow casts at MCP client interaction points in tests

### Review-driven commits for PR #70

1. `893abb0` - `test: harden stdio startup smoke`
2. `497fe5d` - `test: tighten mcp startup coverage`
3. `f1670f5` - `test: narrow pyright suppressions`

### Final E2-S1 quality gate

- `pytest`: 20 passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `pyright`: 0 errors

## PR #71: E2-S2 Review Summary

Story:

- `E2-S2`
- issue `#17`

PR state at summary time:

- open
- mergeable

### Main review themes

1. invalid retention-parameter handling
2. log-root propagation from config
3. thread-safety and mutation-boundary clarity
4. stop-session contract accuracy
5. latest-vs-active session ownership naming
6. lifecycle tests that lock in restart behavior

### Important review findings and fixes

Negative telemetry buffer limit:

- Review found `telemetry_buffer_limit < 0` could crash during trimming.
- Fix:
  - `RoastSessionStore.__init__` rejects negative values with `ValueError`

Configured log root propagation:

- Review found `RoastSessionStore` was seeded with a hardcoded default, ignoring `logging.log_dir`
- Fix:
  - added `build_server_context(...)`
  - session store now uses `config.logging.log_dir / "roasts"`
  - test coverage added

Thread-safety contract:

- Review found the mutable `RoastSession` object could be mutated outside store locking
- Fix:
  - documented `RoastSession` as mutable and not independently thread-safe
  - documented the store as the authoritative mutation boundary
  - later moved telemetry writes behind the store API

Negative `max_samples` in telemetry append:

- Review found per-call retention limits could be invalid
- Intermediate fix:
  - rejected negative `max_samples`
- Final structural fix:
  - removed public caller-controlled telemetry retention from the session object
  - replaced it with `RoastSessionStore.append_telemetry(...)`

Repeated `stop_session()` behavior:

- Review found repeated stop calls still returned the latest stopped session, conflicting with method contract
- Fix:
  - `stop_session()` now returns `None` when there is no active session left
  - coverage added

Misleading internal field name:

- Review found `_active_session` was misleading because it still pointed at the latest stopped session after stop
- Fix:
  - renamed internal field to `_latest_session`
  - aligned related checks and doc semantics

Test clarity and lifecycle restart:

- Review found one test name overstated what was cleared on stop
- Review also asked for restart-after-stop coverage
- Fix:
  - renamed the test to match actual behavior
  - added restart-after-stop coverage

### Review-driven commits for PR #71

1. `603cbdf` - `test: harden session lifecycle edges`
2. `a8084e2` - `test: tighten session store lifecycle`
3. `a0869c7` - `refactor: clarify latest session ownership`

### Final E2-S2 quality gate

- `pytest`: 29 passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `pyright`: 0 errors

## Cross-Story Lessons

Important implementation lessons from these review cycles:

- public introspection fields must reflect runtime truth, not just configuration intent
- bootstrap-safe behavior needs explicit test coverage at the MCP response level
- store-owned mutation boundaries matter early, even before concurrency-heavy tool flows exist
- internal naming must match lifecycle semantics, especially when “active” and “latest” diverge
- broad static-analysis suppressions are easy to add and expensive to clean up later; targeted ignores are worth the extra effort

## Suggested Future Use

When starting `E2-S3` or later runtime stories, re-read this file before changing:

- session lifecycle semantics
- telemetry write paths
- stop behavior
- MCP startup smoke tests

Those areas already had real review churn and are the most likely to regress if later stories shortcut the current boundaries.
