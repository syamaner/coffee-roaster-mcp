# E3-S5 PR 83 Hottop Command Loop Review Cycle

## Scope

This session resumed after PR `#82` for `E3-S4` was merged and issue `#26` was closed. Work started from updated `main` on branch `feature/27-hottop-command-loop` for issue `#27`, `E3-S5: Implement Hottop command loop`.

The story scope was intentionally narrow:

- implement the Hottop command-loop scheduler
- keep Hottop packet construction, status parsing, checksum behavior, heat/fan/drop/cooling commands, and temperature-unit handling out of scope for later Epic 3 stories
- preserve the one-session store boundary and MCP semantics from Epic 2
- preserve mock-safe defaults
- preserve the E3-S4 serial lifecycle invariants
- use the old `coffee-roasting` repo only as behavioral reference, not as an architecture template

## Context Usage

Non-PII Codex status snapshot provided near the end of the story:

- Collaboration mode: Default
- Context window: 11% left, 232K used / 258K

Context consumption was high because this story included initial implementation, full validation, PR creation, and six Copilot review-fix rounds on concurrency, shutdown, write diagnostics, and test determinism. The most expensive parts were repeatedly fetching thread-aware PR review state, reasoning about race windows in the command-loop shutdown path, updating regression tests, rerunning local validation, pushing follow-up commits, and waiting for GitHub CI after each push.

## Implementation Summary

Initial implementation commit:

- `70fade9` - `feat: add hottop command loop streaming`

The initial E3-S5 implementation added:

- `SerialTransport.write(data: bytes)`
- injectable `HottopCommandFrameProvider`
- command-loop ticks at the configured command interval
- serial writes when the injected provider returns a frame
- safe default provider returning `None`, so no unverified Hottop hardware bytes are sent before E3-S6
- raw diagnostics for loop iterations, send attempts, successful writes, last write size, and write errors
- deterministic tests for injected-frame streaming, safe no-frame behavior, write failures, and disconnect behavior
- durable state updates marking `E3-S5` complete and `E3-S6` next

The initial PR body included `Closes #27`.

## Copilot Review Cycle

Copilot reviewed PR `#83` six times and produced 13 actionable comments. All were addressed with targeted follow-up commits and validation. The review loop took six review-fix turns after the initial implementation turn.

### Turn 1: Type contract, diagnostics semantics, and disconnect/write race

Copilot raised 3 issues:

- `SerialTransport.write()` returned `object`, but pyserial and test transports return an integer byte count.
- `_send_command_frame()` could write after `disconnect()` changed connection state because it snapshotted state before writing.
- `command_loop_iterations` and `command_send_attempts` were incremented together, making diagnostics misleading.

Response:

- `1237b7b` - `fix: harden hottop command loop review issues`
- changed `SerialTransport.write()` to return `int`
- added `command_frame_poll_count`
- made `command_send_attempts` count actual frame-send attempts rather than every tick
- added write-path coordination with `_command_write_lock`
- added regression coverage for disconnect preventing post-stop writes

Value:

- Medium to high. This review improved type safety and clarified diagnostics before packet bytes land. The disconnect/write issue was the most important because it affects hardware-control lifecycle safety.

### Turn 2: Partial writes and flaky test assertion

Copilot raised 2 issues:

- partial serial writes were counted as successful writes
- a streaming test compared write counts while the background loop was still running

Response:

- `45be405` - `fix: handle hottop command loop partial writes`
- partial writes now increment error diagnostics and do not increment successful write count
- test assertions now stop the command loop before comparing write counts
- added regression coverage for partial writes

Value:

- Medium. Partial writes are plausible with serial timeouts and would make hardware debugging misleading. The test issue was lower severity but important for stable CI.

### Turn 3: Blocked writes, hook failures, and stale write-size diagnostics

Copilot raised 3 issues:

- `disconnect()` could hang behind `_command_write_lock` if `serial_transport.write()` blocked indefinitely
- `_command_loop_iteration_hook` could raise and kill the command thread while leaving the driver marked connected
- write exceptions could leave stale `last_command_write_size` diagnostics from a previous successful write

Response:

- `cba0da8` - `fix: harden hottop command loop shutdown`
- added serial `write_timeout`
- changed disconnect so stop state is recorded without waiting behind a blocked write
- added close behavior for blocked writes
- wrapped hook execution and fail-closed on hook exceptions by marking disconnected, setting stop, closing serial, and recording an error
- reset `last_command_write_size` to `0` on write exceptions
- added tests for blocked writes, hook failures, and stale diagnostic reset

Value:

- High. The blocked-write and fail-closed behavior directly affect lifecycle safety once real serial hardware is involved.

### Turn 4: Remaining post-stop write race

Copilot raised 1 issue:

- avoiding the write lock during disconnect removed the hang but reopened a race where a frame could still be written after disconnect began.

Response:

- `5070576` - `fix: serialize hottop command loop close`
- `7382949` - `fix: serialize hottop stop requests`
- added non-blocking `_command_write_lock` coordination around normal serial close
- serialized stop-state transition with the write path when possible, without waiting indefinitely behind an already-blocked write
- preserved non-hanging behavior if a write is already blocked
- added regression coverage for the pending-frame disconnect window

Value:

- High. This was a useful second-order review that caught an unintended tradeoff introduced by the previous fix. The first fix serialized normal close, and the follow-up commit tightened stop-state transition after the thread remained actionable.

### Turn 5: Stop-request visibility and timeout coupling

Copilot raised 2 issues:

- there was still a race because stop state could be set after the command thread had passed checks but before `write()`
- `write_timeout` was coupled to `join_timeout_seconds`, mixing serial write behavior with lifecycle join behavior

Response:

- `3b8b52e` - `fix: decouple hottop write timeout`
- added a dedicated cadence-derived write timeout
- added `_disconnect_requested`, set immediately at disconnect start
- `_send_command_frame()` now checks `_disconnect_requested` inside `_command_write_lock` before writing
- kept non-blocking disconnect behavior for already-blocked writes

Value:

- High. This closed the final visible post-stop write race and separated two independent timing concerns.

### Turn 6: Final disconnect/write serialization and unused test helper

Copilot raised 2 issues:

- a remaining disconnect/write race existed because the command loop could hold `_command_write_lock`, pass the stop checks, then write after `disconnect()` had begun but before disconnect could publish stop flags
- `FakeSerialTransport.wait_for_writes()` and its target-state fields were unused after the earlier test cleanup

Response:

- tightened `disconnect()` by acquiring `_command_write_lock` with the dedicated write-timeout before publishing `_disconnect_requested`, `_connected = False`, and `_stop_event`
- kept the bounded lock acquisition so disconnect still does not wait indefinitely behind a blocked serial write
- added a final immediate `_disconnect_requested` / `_stop_event` check inside `_command_write_lock` immediately before `serial_transport.write(frame)`
- removed the unused fake-transport write-target fields and `wait_for_writes()` helper

Value:

- High for the disconnect/write race because Hottop command bytes must not be sent after stop has been requested. Low for the unused helper cleanup, but it kept the tests easier to reason about after the earlier deterministic-test refactor.

## Final Behavior After Review

After the full review cycle, the Hottop command loop has these invariants:

- default frame provider returns no bytes, preserving hardware-safe behavior before packet construction
- command frames can be injected for deterministic lifecycle tests
- serial writes report bytes written and partial writes are treated as errors
- diagnostics distinguish frame polls, send attempts, successful writes, last write size, and errors
- disconnect publishes stop intent while coordinated with the command write path when the write path is not already blocked
- normal stop/close is serialized with the write path
- blocked writes do not hang disconnect indefinitely
- hook failures fail closed and close serial
- stale write-size diagnostics are cleared after write exceptions

## Validation

Final local validation after the last review-fix commit:

- `./.venv/bin/python -m pytest tests/test_drivers.py`: 52 passed
- `./.venv/bin/python -m pytest`: 116 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors

GitHub CI before the sixth local follow-up had passed. The sixth follow-up was locally validated and then pushed for PR CI rerun:

- `Build Package`: passed
- `Checks`: passed

## Durable State

Updated files during the story:

- `src/coffee_roaster_mcp/drivers.py`
- `tests/test_drivers.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Durable state now marks:

- `E3-S5` complete
- next story as `E3-S6: Implement Hottop packet build/parse`

## Current Handoff

PR `#83` is open and mergeable on branch `feature/27-hottop-command-loop`.

Before starting the next story after merge:

1. Confirm PR `#83` is merged and issue `#27` is closed.
2. Run `git checkout main`.
3. Run `git pull --ff-only origin main`.
4. Read `AGENTS.md`, `docs/state/registry.md`, `docs/state/epics/coffee-roaster-mcp-v0.1.md`, and GitHub issue `#28`.
5. Start `E3-S6` from updated `main`, focused only on Hottop packet build/parse.
