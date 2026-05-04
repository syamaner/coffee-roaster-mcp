# Code Review Summary: E2-S3 And E2-S4

Date: 2026-05-04

Repository: `syamaner/coffee-roaster-mcp`

## Scope

This summary captures the code review findings and fixes for:

- PR `#72` - `Add roast event timeline`
- PR `#73` - `Add core MCP tools`

The goal is to preserve the review history in a compact durable form, especially the behavior and lifecycle corrections that materially changed the runtime design.

## PR #72: E2-S3 Review Summary

Story:

- `E2-S3`
- issue `#18`

PR outcome:

- merged

### Main review themes

1. stopped-session telemetry mutation
2. first-fault timestamp stability

### Important review findings and fixes

Stopped-session telemetry mutation:

- Review found `append_telemetry(...)` still allowed writes after a session stopped.
- Fix:
  - moved telemetry writes behind the same active-session guard used for event writes
  - added regression coverage that appending telemetry to a stopped session raises `SessionLifecycleError`

First-fault timestamp stability:

- Review found the code needed explicit coverage that multiple `fault` rows do not overwrite the authoritative first-fault timestamp fields.
- Fix:
  - added unit coverage that multiple `fault` events remain in the timeline
  - `faulted_at_utc` and `faulted_monotonic_seconds` stay anchored to the first fault

### Review-driven commits for PR #72

1. `e795536` - `test: harden event timeline mutation rules`

### Final E2-S3 quality gate

- `pytest`: 34 passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `pyright`: 0 errors

## PR #73: E2-S4 Review Summary

Story:

- `E2-S4`
- issue `#19`

PR state at summary time:

- open
- review fixes pushed

### Main review themes

1. cooling lifecycle correctness
2. completed-session finalization
3. manual first-crack override enforcement
4. emergency-stop lifecycle correctness
5. fail-closed control behavior after fault
6. snapshot serialization under store locking
7. read-only export behavior
8. control-type validation

### Important review findings and fixes

Cooling completion left the roast active:

- Review found `stop_cooling()` moved the phase to `complete` without actually stopping the session.
- Fix:
  - `stop_cooling()` now records `stopped_at_utc` and `monotonic_stop`
  - completed roasts are no longer left active
  - later `start_roast_session()` calls succeed in the same process

Cooling before bean drop:

- Review found cooling could start before drop, producing impossible roast timelines.
- Fix:
  - `start_cooling()` now rejects calls before `beans_dropped`
  - `stop_cooling()` also rejects invalid out-of-order calls

Manual first-crack override ignored config:

- Review found `mark_first_crack` ignored `first_crack.allow_manual_override`
- Fix:
  - the MCP tool now returns an error result when manual override is disabled
  - MCP-level config-driven test coverage added

Emergency stop left the session active:

- Review found `emergency_stop` recorded a fault but left the session active, which blocked later roasts
- Fix:
  - emergency stop is now store-owned
  - it forces fail-closed mock state and stops the session with phase `fault`
  - a later roast can start cleanly

Reheating after fault:

- Review found `set_heat` still allowed non-zero heat after a fault
- Fix:
  - heat cannot be increased after a fault
  - faulted sessions are stopped, so normal mutation attempts fail through the active-session guard

Phase changes after fault:

- Review found later non-fault events could overwrite `phase` after a `fault`
- Fix:
  - fault is terminal for later non-fault event writes

Snapshot serialization outside the store lock:

- Review found MCP responses were iterating mutable `RoastSession` state outside the documented mutation boundary
- Fix:
  - added store-owned deep-copied session snapshots
  - MCP tools now serialize from snapshot copies instead of from live mutable objects

Export tool side effects:

- Review found `export_roast_log` claimed to be a manifest-only tool but still created directories
- Fix:
  - removed the `mkdir(...)` side effect
  - the tool now only returns the planned paths

Control-type validation:

- Review found `_validate_control_percent()` accepted values like `bool` and `float`
- Fix:
  - explicit runtime rejection for non-`int` values and `bool`
  - added unit coverage

### Review-driven commits for PR #73

1. `e19f867` - `fix: harden mock tool lifecycle`

### Final E2-S4 quality gate

- `pytest`: 43 passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `pyright`: 0 errors

## Cross-Story Lessons

Important implementation lessons from these review cycles:

- completing a phase is not enough; session active/stopped state must also be finalized explicitly
- phase preconditions matter even in a mock-only runtime, otherwise later stories inherit impossible timelines
- configuration flags like manual override need MCP-surface enforcement, not just documentation
- emergency stop should be a terminal lifecycle action, not just an event append
- once the repo documents the store as the concurrency boundary, serialization must honor that contract too
- read-only tools should stay free of hidden filesystem side effects

## Suggested Future Use

When starting `E2-S5` or later runtime stories, re-read this file before changing:

- phase transitions
- terminal fault behavior
- emergency-stop semantics
- snapshot/state serialization
- export behavior

Those areas already had real review churn and are the most likely places for regressions if later stories shortcut the current boundaries.
