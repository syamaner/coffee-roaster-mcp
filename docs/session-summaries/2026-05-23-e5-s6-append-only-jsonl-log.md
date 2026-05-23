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

- `./.venv/bin/python -m pytest`: 325 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

GitHub validation after commit `4bdf412`:

- `Build Package`: passed
- `Checks`: passed
- `CodeRabbit`: passed with review skipped on the latest check run
- PR #123 remained open, mergeable, and based on `main`

## Usage Snapshot

Operator-provided context snapshot after PR #123 review and fix turn:

- Context window: `31% left (182K used / 258K)`
- 5h limit: `92% left`, resets `02:17 on 24 May`
- Weekly limit: `98% left`, resets `21:17 on 30 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `04:41 on 24 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `23:41 on 30 May`
- Warning: `limits may be stale - run /status again shortly`

## Review Comparison

PR #123 review state was checked after the initial E5-S6 implementation commit
`f9946ca` and after the review-fix commit `4bdf412`.

Codex review on `f9946ca` reported four actionable findings:

- Event log writes happened after in-memory event/phase mutation, so an I/O
  error could commit session state while losing the JSONL row.
- Telemetry writes happened after appending to the rolling telemetry buffer, so
  a failed JSONL append could advance metrics state without durable log state.
- Invalid `logging.sample_interval_seconds` could surface as raw `ValueError`
  from `RoastSessionStore` construction instead of the config layer's
  `ConfigError`.
- Reserved driver drop/cooling paths could leave a pending command reservation
  if a JSONL event append failed while completing the reservation.

CodeRabbit's first pass on `f9946ca` overlapped with Codex on the core atomicity
problem and added two separate review findings:

- The restart prompt used the machine-specific path
  `/Users/sertanyamaner/git/coffee-roaster-mcp`; this was made portable as
  "the local clone of `syamaner/coffee-roaster-mcp`".
- `_copy_session_for_read(...)` did not preserve
  `last_logged_telemetry_monotonic_seconds`, so snapshots lost the new telemetry
  log cursor.

The accepted fix in `4bdf412` addressed the shared and unique findings:

- Event rows are staged and appended to JSONL before timeline and phase mutation.
- Telemetry rows are staged and appended before the telemetry buffer and cursor
  advance.
- Reserved driver event completions now clear pending reservations in `finally`
  blocks.
- `logging.sample_interval_seconds` now uses positive-float config validation.
- Read snapshots copy `last_logged_telemetry_monotonic_seconds`.
- Regression tests cover failed event writes, failed telemetry writes, reserved
  drop cleanup, cursor copying, and positive sample interval validation.

Review quality comparison:

- Codex was sharper on operational failure modes. It split the logging issue
  into event atomicity, telemetry atomicity, config error normalization, and
  reservation cleanup, which mapped directly to risk-bearing tests.
- CodeRabbit was broader on handoff and consistency. It caught the portable
  restart prompt and snapshot cursor omission, and its grouped atomicity comment
  covered the same root issue as two of the Codex findings.
- The overlap was useful: both reviewers independently flagged JSONL durability
  atomicity, which was the highest-risk issue in the story.
- CodeRabbit's follow-up after `4bdf412` reported only one low-value nitpick:
  the `_telemetry_log_row_if_due(...)` docstring still says "configured 1 Hz"
  even though the interval is configurable. This is documentation polish, not a
  behavior blocker, and all review threads from the first pass are marked
  resolved.

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E5-S6 should
be checked first. If it has merged, verify issue #45 is closed, check out `main`,
run `git pull --ff-only origin main`, then begin E5-S7 from updated main on the
appropriate `feature/46-...` branch after reading the registry, active epic,
this summary, and the GitHub issue for E5-S7. Keep E5-S7 scoped to CSV roast log
export unless its issue explicitly requires more, and preserve the append-only
JSONL runtime writer plus existing metrics/session/runtime boundaries.
