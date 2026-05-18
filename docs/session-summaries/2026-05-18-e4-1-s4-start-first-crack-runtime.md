# E4.1-S4 First-Crack Runtime Session

## Scope

This session resumed after `PR #114` for `E4.1-S3` was squashed and merged,
and issue `#106` was closed. Work started from updated `main` on branch
`feature/107-start-first-crack-detection-runtime-with-roast-sessions` for issue
`#107`, `E4.1-S4: Start first-crack detection runtime with roast sessions`.

The story goal was to start and stop the configured first-crack detection
runtime with roast sessions while preserving the mock default, Epic 2
one-session store boundary, MCP semantics, fail-closed safety behavior,
coverage workflow, Epic 3 Hottop validation boundary, E4.1-S1 configured-driver
control wiring, E4.1-S2 `get_roast_state` device/status response shape,
E4.1-S3 released-artifact ONNX backend boundary, and E4.1-S6 automatic T0 story
boundary.

## Context Usage

Final context snapshot supplied by the operator after review fixes:

- Context window: `35% left (173K used / 258K)`
- 5h limit: `95% left`, resets `01:10 on 19 May`
- Weekly limit: `93% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `02:20 on 19 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `21:20 on 25 May`
- Warning: limits may be stale; run `/status` again shortly.

## Pre-Story Verification

Before starting E4.1-S4:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the merged
  E4.1-S3 changes.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/github-issues.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-18-e4-1-s3-released-onnx-detector-backend.md`,
  and GitHub issue `#107`.
- Created branch
  `feature/107-start-first-crack-detection-runtime-with-roast-sessions`.

## Implementation

Updated:

- `src/coffee_roaster_mcp/first_crack_runtime.py`
- `src/coffee_roaster_mcp/mcp_server.py`
- `tests/test_first_crack_runtime.py`
- `tests/test_mcp_server.py`
- `README.md`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior implemented:

- Added `FirstCrackSessionRuntime` as the session-owned coordinator for audio
  capture and detector processing.
- In `first_crack.mode: audio`, `start_roast_session` prepares the configured
  audio capture pipeline and the released-artifact ONNX detector adapter.
- Disabled and manual modes do not start audio capture or detector runtime.
- Queued detector windows are processed only for the owning active session after
  authoritative T0 moves the session into `roasting`.
- Confirmed detector output records `first_crack_detected` exactly once through
  the existing detector adapter and `RoastSessionStore` integration path.
- Runtime artifact, audio-capture, and detector failures surface through
  `get_roast_state.first_crack_status` as `unavailable` or `faulted` without
  crashing normal session control.
- Runtime stop is wired to confirmed first crack, explicit `mark_first_crack`
  override, `drop_beans`, `stop_cooling`, `emergency_stop`, and MCP process
  shutdown.

Out of scope kept out:

- Automatic T0 implementation.
- Rolling telemetry metrics and final log schemas.
- Model training, ONNX export, Hugging Face sync, real microphone validation,
  live Hottop validation, or broad release validation.

## Review Fixes

`PR #115` received two automated review comments in
<https://github.com/syamaner/coffee-roaster-mcp/pull/115#pullrequestreview-4313625992>.
Both were actionable and were fixed in commit `eea7135`.

Fixes made:

- `get_roast_state` now resolves the session and reads the configured driver
  state before processing queued first-crack windows. If driver `read_state()`
  fails, the failed state query does not mutate the session timeline.
- `mark_beans_added` now returns the original `beans_added` event together with
  a refreshed phase and event count after any immediate detector side effects.
  If an already queued detector window confirms first crack during the same
  command, the response reflects the post-detection `development` phase and
  updated event count.

Regression coverage added:

- `test_get_roast_state_reads_driver_before_detector_side_effects`
- `test_mark_beans_added_returns_snapshot_after_immediate_detector_confirmation`

## Validation

Initial implementation validation:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_runtime.py tests/test_mcp_server.py tests/test_first_crack_integration.py`:
  `29 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `278 passed`, required coverage `90.0%` reached, total coverage `90.20%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.

Post-review validation after commit `eea7135`:

- Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_first_crack_runtime.py`:
  `22 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `280 passed`, required coverage `90.0%` reached, total coverage `90.15%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.
- GitHub CI for `PR #115` passed `Checks` and `Build Package` after both the
  initial implementation commit and the review-fix commit.

## Pull Request Status

`PR #115` is open at
<https://github.com/syamaner/coffee-roaster-mcp/pull/115>. At summary time:

- PR state: open.
- Merge state: mergeable.
- Branch:
  `feature/107-start-first-crack-detection-runtime-with-roast-sessions`.
- Commits before this summary:
  - `c52a5d7 feat: start first-crack runtime with sessions`
  - `eea7135 fix: keep first-crack side effects consistent`

## Handoff

Durable state now points to `E4.1-S5`, issue `#108`, for MCP operational
readiness tests and docs. Continue to preserve normal CI as mock-safe: no
Hottop hardware, microphone, model download, real ONNX file, or network should
be required.
