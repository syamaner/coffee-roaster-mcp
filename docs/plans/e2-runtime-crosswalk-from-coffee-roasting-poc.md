# Epic 2 Runtime Crosswalk From `coffee-roasting` POC

## Purpose

This note maps the old `coffee-roasting` proof of concept to the new `coffee-roaster-mcp` Epic 2 implementation work.

Use it to recover proven runtime behavior from the old codebase without carrying forward the old two-server plus orchestration architecture.

## Source Of Truth

For this repository:

- The target architecture remains `docs/plans/coffee-roaster-mcp-v0.1-overall-plan.md`
- The active implementation state remains `docs/state/epics/coffee-roaster-mcp-v0.1.md`

The old `coffee-roasting` repository is a behavioral reference only.

## Old POC Boundaries

The old proof of concept split responsibilities across:

- `src/mcp_servers/roaster_control`
- `src/mcp_servers/first_crack_detection`
- orchestration and remote access layers around them

That split is exactly what the new repo is removing.

Carry forward:

- roast-session semantics
- roast metrics semantics
- basic stdio MCP server bootstrap patterns
- proven tool behavior where it matches the new single-server plan

Do not carry forward:

- two MCP server ownership
- cross-server synchronization
- Auth0 middleware
- SSE or HTTP transport
- `n8n` orchestration assumptions
- observability or deployment glue that only existed for the old remote setup

## Most Useful Old Modules

### Strong behavioral references

- `coffee-roasting/src/mcp_servers/roaster_control/roast_tracker.py`
- `coffee-roasting/src/mcp_servers/roaster_control/session_manager.py`
- `coffee-roasting/src/mcp_servers/roaster_control/models.py`

These are the best references for:

- T0 and roast timing semantics
- first crack and drop event handling
- development time and RoR behavior
- session state shape

### Useful bootstrap references

- `coffee-roasting/src/mcp_servers/roaster_control/mcp_server.py`
- `coffee-roasting/src/mcp_servers/roaster_control/server.py`
- `coffee-roasting/src/mcp_servers/first_crack_detection/server.py`

These are useful for:

- stdio MCP startup shape
- tool registration shape
- initialization and clean shutdown patterns

### Reference only, not to be ported directly

- `coffee-roasting/src/mcp_servers/*/sse_server.py`
- `coffee-roasting/src/mcp_servers/auth0_middleware.py`
- `coffee-roasting/src/mcp_servers/shared/auth0_middleware.py`
- `coffee-roasting/docs/03-phase-3/*`
- old agent-platform and `n8n` setup docs

## Crosswalk By Epic 2 Story

### `E2-S1` Implement stdio MCP server entrypoint

Primary old references:

- `roaster_control/mcp_server.py`
- `roaster_control/server.py`
- `first_crack_detection/server.py`

What to carry forward:

- use stdio transport
- keep startup logic explicit and testable
- keep logging off stdout and stderr where it can interfere with MCP transport
- make initialization and shutdown deterministic

What to change for the new repo:

- one server only
- no separate detector server
- no HTTP or SSE transport
- no Auth0 or remote access middleware
- no old `USE_MOCK_HARDWARE` or `ROASTER_MOCK_MODE` bootstrap flags unless the new config model requires them

Recommended scope for `E2-S1`:

- prove local stdio startup
- expose only a minimal bootstrap-safe tool list
- do not pull full roast control behavior into the entrypoint story

### `E2-S2` Implement `RoastSession` lifecycle

Primary old references:

- `roaster_control/session_manager.py`
- `roaster_control/models.py`

What to carry forward:

- one active runtime owner for session state
- explicit session start and stop lifecycle
- clear latest-state access pattern

What to change for the new repo:

- replace thread-oriented session manager naming with `RoastSession` ownership
- move from old hardware-first orchestration toward session-first orchestration
- avoid hard-coding the old roaster-control tool boundaries into the session object

### `E2-S3` Implement core event timeline

Primary old references:

- `roaster_control/roast_tracker.py`
- old tool names and event behavior in `roaster_control/server.py`

What to carry forward:

- idempotent recording of first crack and drop
- event timestamps as explicit state, not inferred ad hoc later

What to change for the new repo:

- use the new event names from the plan
- make the event timeline the single shared record across roast control and first-crack integration

### `E2-S4` Implement core MCP tools

Primary old references:

- `roaster_control/server.py`
- `docs/MCP_TOOLS_REFERENCE.md`

What to carry forward:

- tool validation discipline
- clear status-returning tool behavior
- conservative control commands

What to change for the new repo:

- collapse old roaster-control and first-crack tool interactions into one server surface
- use the new tool names from the current plan, not the old two-server names
- keep the tool surface aligned with the single authoritative session

### `E2-S5` Implement phase transitions

Primary old references:

- `roaster_control/roast_tracker.py`
- old roast workflow docs and examples

What to carry forward:

- roast progression semantics around beans added, first crack, and drop

What to change for the new repo:

- phase must be explicit state in the session core
- phase should not depend on multiple MCP servers staying synchronized

### `E2-S6` Implement emergency stop and fault recording

Primary old references:

- old roaster-control command behavior
- old hardware-control safety intent

What to carry forward:

- fail closed
- keep stop behavior operator-visible

What to change for the new repo:

- record fault or emergency-stop state in the single event timeline
- make the active driver contract responsible for the safety call

### `E2-S7` Complete thin vertical slice spike

Primary old references:

- old mock roast workflow examples in `roaster_control/README.md`
- old tool flow in `docs/MCP_TOOLS_REFERENCE.md`

What to carry forward:

- start to drop workflow expectations
- state polling and summary expectations

What to change for the new repo:

- one process only
- no detector-server coordination
- no orchestration dependency

## Immediate Guidance For `E2-S1`

Before writing code:

1. Use the old `roaster_control/mcp_server.py` and `server.py` only for stdio startup and tool-registration patterns.
2. Treat `roast_tracker.py` as the main behavioral reference for later session semantics, not as an entrypoint dependency to wire in immediately.
3. Keep `E2-S1` transport-focused. Do not prematurely implement the whole roast lifecycle inside the entrypoint story.
4. Prefer the new repo config model and package layout even when old code solved a similar problem differently.

## Decision Summary

The old POC is useful, but only in slices:

- entrypoint and MCP registration patterns for `E2-S1`
- session and roast-metric semantics for later Epic 2 stories
- Hottop and remote orchestration code only as historical context

The new repo should look like one coherent MCP runtime, not like two old servers pasted into one package.
