# E4.1-S6 Automatic T0 Runtime Path Session

## Scope

This session resumed after `PR #116` for `E4.1-S5` was squashed and
merged, and issue `#108` was closed. Work started from updated `main` on
branch `feature/111-add-automatic-t0-runtime-path` for issue `#111`,
`E4.1-S6: Add automatic T0 runtime path`.

The story goal was to add the internal automatic T0 path so an agent-driven
roast can record authoritative `beans_added` without using `mark_beans_added`
as the primary runtime path. Scope stayed bounded to configured-driver
temperature reads, the one-session `RoastSessionStore` mutation boundary,
mock-safe CI, and MCP state diagnostics.

## Context Usage

Latest context snapshot supplied by the operator after implementation and PR
review fixes:

- Context window: `20% left (209K used / 258K)`
- 5h limit: `90% left`, resets `01:10 on 19 May`
- Weekly limit: `92% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `03:33 on 19 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `22:33 on 25 May`

## Pre-Story Verification

Before starting E4.1-S6:

- Verified `PR #116` was merged and issue `#108` was closed.
- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through merged
  E4.1-S5 changes.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/github-issues.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-18-e4-1-s5-mcp-operational-readiness.md`,
  and GitHub issue `#111`.
- Created branch `feature/111-add-automatic-t0-runtime-path`.

## Implementation

Initial implementation updated:

- `src/coffee_roaster_mcp/config.py`
- `src/coffee_roaster_mcp/session.py`
- `src/coffee_roaster_mcp/mcp_server.py`
- `tests/test_config.py`
- `tests/test_session.py`
- `tests/test_mcp_server.py`
- `tests/test_package.py`
- `README.md`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior added:

- Added `session.auto_t0_drop_threshold_c`, defaulting to `25.0`, while
  keeping `session.auto_t0_detection_enabled` disabled by default.
- Added `RoastSessionStore.process_auto_t0_reading_snapshot(...)` to track the
  max preheat/charge bean temperature before T0 and record `beans_added` when
  current bean temperature drops from that max by the configured threshold.
- Preserved charge temperature, detected bean temperature, drop, threshold, and
  `auto_t0` source in the automatic `beans_added` event payload.
- Wired `get_roast_state` to process automatic T0 only after a successful
  `RoasterDriver.read_state()` call and before first-crack runtime window
  processing.
- Added `get_roast_state.t0_status` with automatic T0 enabled/disabled status,
  pending/detected state, charge temperature, current drop, threshold, and
  detected bean-temperature diagnostics.
- Preserved `mark_beans_added` as an explicit idempotent override.

Out of scope kept out:

- Rolling telemetry metrics and final log schemas.
- Model training, ONNX export, Hugging Face sync.
- Real microphone validation, live Hottop validation, end-to-end agent roast
  validation, or broad release validation.

## Review Fixes

Four actionable PR review comments were addressed:

- `7876ae2` - `fix: align auto t0 drop diagnostics with threshold`
  - Removed rounding from the stored `auto_t0_current_drop_c` diagnostic so a
    true drop such as `24.9996` against a `25.0` threshold does not display as
    `25.0` while automatic T0 remains pending.
  - Added session-store and MCP regressions for the near-threshold pending
    case.
- `261916d` - `fix: skip auto t0 on disconnected driver state`
  - Gated automatic T0 processing on `device_state.connected`, so disconnected
    or stale driver readings cannot record `beans_added`.
  - Added an MCP regression proving a disconnected driver with a large stale
    temperature drop leaves the session in `pre_roast` with no events.
- `b181710` - `fix: harden auto t0 audio and threshold handling`
  - Rejected non-finite automatic T0 thresholds (`nan`, `inf`, `-inf`) during
    config load, including YAML and `COFFEE_AUTO_T0_DROP_THRESHOLD_C`.
  - Added first-crack runtime support for discarding queued audio windows at a
    runtime boundary.
  - Wired `get_roast_state` to discard queued pre-T0 detector windows when
    automatic T0 records `beans_added`, before normal first-crack detector
    processing resumes.
  - Added config, runtime, and MCP regressions for the non-finite threshold and
    queued pre-T0 audio-window cases.

## Validation

Initial implementation validation:

- Ran `./.venv/bin/python -m pytest tests/test_config.py tests/test_session.py tests/test_mcp_server.py`:
  `76 passed`.
- Ran `./.venv/bin/python -m pytest tests/test_package.py`: `15 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `289 passed`, required coverage `90.0%` reached, total coverage `90.06%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

After the near-threshold review fix:

- Ran `./.venv/bin/python -m pytest tests/test_session.py tests/test_mcp_server.py`:
  `64 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `291 passed`, required coverage `90.0%` reached, total coverage `90.06%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

After the disconnected-driver review fix:

- Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py`: `22 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `292 passed`, required coverage `90.0%` reached, total coverage `90.06%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

After the non-finite threshold and queued pre-T0 audio-window review fixes:

- Ran `./.venv/bin/python -m pytest tests/test_config.py tests/test_first_crack_runtime.py tests/test_mcp_server.py`:
  `48 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `298 passed`, required coverage `90.0%` reached, total coverage `90.06%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

GitHub CI for `PR #117` on head
`b18171099e7171bb32fdea290af06f1d05e3024c` passed:

- `Build Package`: passed.
- `Checks`: passed.

## Pull Request Status

`PR #117` is open at
<https://github.com/syamaner/coffee-roaster-mcp/pull/117>. At summary time:

- PR state: open.
- Merge state: mergeable.
- Branch: `feature/111-add-automatic-t0-runtime-path`.
- Latest commit:
  - `b181710` - `fix: harden auto t0 audio and threshold handling`

## Handoff

Durable state now points to `E5-S1`, issue `#40`, for the rolling telemetry
buffer. Continue to preserve normal CI as mock-safe: no Hottop hardware,
microphone, model download, real ONNX file, or network should be required.
