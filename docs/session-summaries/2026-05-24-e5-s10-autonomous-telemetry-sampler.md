# E5-S10 Autonomous Telemetry Sampler

This summary captures the E5-S10 implementation, validation state, and restart
context.

## Scope

Story: `E5-S10` / issue `#127`, add autonomous telemetry sampler.

Branch: `feature/127-autonomous-telemetry-sampler`

The implementation stayed inside the E5-S10 boundary:

- start a session-owned telemetry sampler from `start_roast_session`
- poll the configured roaster driver at `logging.sample_interval_seconds`
- keep the default sample interval at 5 seconds
- append sampled state through the existing `RoastSessionStore` telemetry path
- keep `get_roast_state` as an opportunistic telemetry refresh path
- stop sampler workers on owner-session completion/fault and MCP shutdown
- fail closed with a diagnosable fault event on sampler driver-read failure
- preserve append-only JSONL runtime logging, CSV export schema, summary schema,
  one-session store ownership, configured-driver state/control wiring,
  automatic T0 behavior, session-owned first-crack runtime behavior, mock-safe
  CI, Hottop validation boundaries, and first-crack artifact/audio boundaries

No model training, ONNX export, Hugging Face sync, real microphone validation,
live Hottop validation, end-to-end agent roast validation, broad release
validation, package metadata, PyPI publishing, or MCP Registry work was added.

## Implementation Summary

`src/coffee_roaster_mcp/mcp_server.py` now owns a `_TelemetrySampler` background
worker in `ServerContext`. `start_roast_session` starts the sampler for the new
session, and MCP lifespan shutdown stops it before first-crack runtime shutdown.
Cooling completion and emergency stop also stop the owning sampler explicitly.

The sampler waits one configured interval before its first autonomous read, then
polls `RoasterDriver.read_state()` on the configured cadence. Successful reads
append telemetry through `RoastSessionStore.record_active_telemetry_sample(...)`
and then run the existing automatic T0 and first-crack runtime processing paths
for the same active session. This keeps runtime mutation behind the existing
store boundary and avoids making client polling the only way telemetry, derived
metrics, JSONL logging, automatic T0, or first-crack processing can advance.

`get_roast_state` still performs the existing explicit state read and telemetry
append, so MCP calls can refresh telemetry opportunistically. Driver read
failures inside the sampler fail closed through the existing emergency-stop
safety payload path, record a fault event with the read failure in the reason,
stop first-crack processing for the session, and let the sampler worker exit.

`tests/test_package.py` now covers:

- no-client-poll append-only telemetry logging through the stdio MCP flow
- opportunistic telemetry refresh from `get_roast_state`
- sampler shutdown stopping background reads
- sampler driver-read failure faulting the active session and exiting

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E5-S10` complete and sets
  the active story to `E6-S1`.
- `docs/state/registry.md` says the next story is `E6-S1: add PyPI package
  metadata`.
- `docs/state/github-issues.md` adds issue `#127` to the Epic 5 story index.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_config.py tests/test_session.py tests/test_package.py`:
  108 passed
- `./.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_package.py`:
  43 passed

Full validation:

- `./.venv/bin/python -m pytest`: 341 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Usage Snapshot

Operator-provided cumulative session usage:

- Tokens used so far in this session: `552K`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E5-S10 should
be checked first. If it has merged, verify issue #127 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S1 from updated
main on the appropriate `feature/49-...` branch after reading the registry,
active epic, this summary, and GitHub issue #49. Keep E6-S1 scoped to package
metadata only; do not add PyPI publishing, MCP Registry work, live hardware
validation, model training/export/sync, real microphone validation, or broad
release validation unless issue #49 explicitly requires it.
