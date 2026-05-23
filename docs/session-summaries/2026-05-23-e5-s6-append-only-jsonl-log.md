# E5-S6 Append-Only JSONL Roast Log

This summary captures the E5-S6 implementation, validation state, and restart
context.

## Scope

Story: `E5-S6` / issue `#45`, write append-only JSONL roast log.

Branch: `feature/45-write-append-only-jsonl-roast-log`

The implementation stayed inside the E5-S6 boundary:

- write JSONL event rows immediately when new authoritative timeline events are
  recorded
- write JSONL telemetry rows from the existing E5-S1 polling sample path at the
  configured sample interval, defaulting to 1 Hz
- preserve the E5-S1 rolling telemetry buffer for derived metrics
- preserve E5-S2 elapsed, E5-S3 development metric, E5-S4 delta, and E5-S5 RoR
  helpers
- preserve the one-session `RoastSessionStore` mutation boundary and mock-safe
  MCP path
- keep snapshot CSV and `summary.json` behavior available without overwriting
  an existing append-only `roast.jsonl`

No final CSV schema work, final `summary.json` schema work, model training, ONNX
export, Hugging Face sync, real microphone validation, live Hottop validation,
end-to-end agent roast validation, or broad release validation was added.

## Implementation Summary

`RoastSessionStore` now owns append-only JSONL writes at the same mutation
points that already own session state:

- `record_event(...)` appends event rows for new timeline events.
- automatic first-crack detection appends the confirmed first-crack row.
- emergency fault recording appends the fault row.
- telemetry recording appends telemetry rows only when the configured interval
  has elapsed since the last written telemetry row for that session.

The MCP server passes `logging.sample_interval_seconds` into the session store,
so the default remains 1 Hz while config can tune the interval.

`export_roast_snapshot(...)` still writes `roast.csv` and `summary.json`, but it
does not overwrite an existing append-only runtime `roast.jsonl`. If a legacy
snapshot has no runtime JSONL file yet, export can still create the current
event-only JSONL fallback.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E5-S6` complete.
- `docs/state/registry.md` says the next story is `E5-S7: export CSV roast log`.

## Validation

Local validation:

- `./.venv/bin/python -m pytest`: 321 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Restart Prompt

Resume in `/Users/sertanyamaner/git/coffee-roaster-mcp`. PR for E5-S6 should be
checked first. If it has merged, verify issue #45 is closed, check out `main`,
run `git pull --ff-only origin main`, then begin E5-S7 from updated main on the
appropriate `feature/46-...` branch after reading the registry, active epic,
this summary, and the GitHub issue for E5-S7. Keep E5-S7 scoped to CSV roast log
export unless its issue explicitly requires more, and preserve the append-only
JSONL runtime writer plus existing metrics/session/runtime boundaries.
