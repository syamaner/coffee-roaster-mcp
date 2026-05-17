# E4-S9 Integrate First Crack With Session Timeline Session

## Scope

This session resumed after `PR #100` for `E4-S8` was squashed and merged, and
issue `#97` was closed. Work started from updated `main` on branch
`feature/39-integrate-first-crack-with-session-timeline` for issue `#39`,
`E4-S9: Integrate first crack with session timeline`.

The story goal was to map confirmed detector output into the authoritative
one-session timeline exactly once, using the existing E4-S1 through E4-S8
released-artifact resolver, validation, audio-source, audio-pipeline, and
detector-adapter boundaries.

The work preserved the Epic 2 one-session store boundary, MCP semantics,
mock-safe defaults, coverage workflow, Epic 3 Hottop safety/validation boundary,
and optional/gated real microphone validation. It did not add model training,
ONNX export, Hugging Face sync, local directory sync behavior, broad coverage
hardening, detector startup, audio capture startup, or live Hottop control
changes.

## Context Usage

Session usage snapshot supplied by the operator after the E4-S9 review loop:

- Context window: `18% left (214K used / 258K)`
- 5h limit: `90% left`, resets `22:35`
- Weekly limit: `96% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `02:11 on 18 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `21:11 on 24 May`

Quality versus context consumption note:

- The PR review loop consumed substantial context, but the Codex review comments
  were high quality. Each comment identified a real integration edge case in the
  first-crack timeline boundary rather than style churn.
- The comments improved correctness in five important places: preserving the
  detector timestamp as authoritative timing, accepting adapter-inferred default
  window-end timestamps without breaking the common backend path, ignoring
  pre-beans false positives, and restricting future-timestamp tolerance to
  inferred timestamps only, and ignoring late detector confirmations after the
  session leaves active `roasting`.
- The fixes stayed inside E4-S9 scope. They did not expand into detector
  startup, audio capture startup, ONNX runtime inference, MCP tool wiring, or
  broad coverage hardening.

## Pre-Story Verification

Before starting E4-S9:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the E4-S8 merge
  to `b0f56f4`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s8-add-microphone-and-wav-audio-input-adapters.md`,
  and GitHub issue `#39`.
- Confirmed issue `#39` required mocked detector output to create exactly one
  `first_crack_detected` event and required manual override behavior to keep
  following config.

## Implementation

Updated:

- `src/coffee_roaster_mcp/detector.py`
- `src/coffee_roaster_mcp/session.py`
- `tests/test_detector.py`
- `tests/test_first_crack_integration.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior added:

- Added `integrate_first_crack_window_with_session(...)` as the explicit E4-S9
  detector-to-session integration helper.
- The helper is gated to `first_crack.mode: audio`; disabled and manual modes do
  not call the detector adapter or mutate the session timeline.
- Confirmed detector output is recorded through `RoastSessionStore` as an
  authoritative `first_crack_detected` event.
- Detector metadata is stored in the event payload, including source, detected
  monotonic timestamp, precision, revision, repo id, selected ONNX artifact,
  feature-extractor artifact, window sequence number, and optional confidence.
- Repeated detector confirmations return the existing singleton
  `first_crack_detected` event without appending duplicate timeline rows.
- Automatic detection remains independent of manual override permission, so
  `allow_manual_override: false` only disables the manual MCP override path.

## Review Fixes

Codex review `4306194507` found an actionable timing issue:

- The first implementation stored `detection_event.detected_at_monotonic_seconds`
  only in payload metadata, while the authoritative `RoastEvent` timestamp came
  from the later integration time.
- This would skew `session.first_crack_monotonic_seconds` and downstream
  development metrics when detector inference or queue processing was delayed.

Fix applied:

- Added `RoastSessionStore.record_first_crack_detection_snapshot(...)`.
- Automatic detector integration now records the authoritative first-crack event
  at the detector-provided monotonic timestamp.
- Added regression coverage proving `compute_roast_metrics(...)` uses the
  detector timestamp rather than the later integration time.

Codex review `4306204446` found a default timestamp edge case:

- The detector adapter falls back to `window.started_at_monotonic_seconds +
  window.duration_seconds` when a backend omits an explicit timestamp.
- Because audio windows are emitted around capture time, that inferred window-end
  timestamp can be slightly ahead of the integration clock and was rejected as a
  future timestamp.

Fix applied:

- Added a bounded future-timestamp tolerance for inferred window-end timestamps.
- `integrate_first_crack_window_with_session(...)` passes the active
  `window.duration_seconds` as that tolerance.
- Added regression coverage for the common backend path where no explicit
  detector timestamp is provided.

Codex review `4306216288` found an early false-positive issue:

- Confirmed detector output before `beans_added` could raise a lifecycle error
  from the session store and break the detection loop.

Fix applied:

- `integrate_first_crack_window_with_session(...)` now ignores detector output
  before beans are added and does not call the backend in that state.
- Added regression coverage proving a confirmed pre-roast detector output leaves
  the timeline empty and keeps the session in `pre_roast`.

Codex review `4306239859` found that tolerance was too broad:

- The future-timestamp tolerance also applied to explicit backend timestamps,
  which could hide backend clock or timestamp bugs by clamping them.

Fix applied:

- Added `detected_at_inferred` to `FirstCrackDetectionEvent`.
- Future tolerance is now applied only when the adapter inferred the timestamp
  from the window end.
- Explicit future timestamps from detector backends fail fast.
- Added regression coverage proving explicit future detector timestamps are
  rejected.

Codex review `4306250152` found a late lifecycle issue:

- The integration guard only checked `beans_added_at_utc`, so confirmed detector
  output could still reach the session store after the session had moved to
  `dropped`, `cooling`, `complete`, `fault`, or stopped states when first crack
  had not been recorded.
- In those states, `first_crack_detected` is not a valid transition and the
  store would raise `SessionLifecycleError`, potentially crashing a long-running
  detector loop on late confirmations.

Fix applied:

- `integrate_first_crack_window_with_session(...)` now processes detector output
  only while the session is active and still in `roasting`.
- Late confirmations after first crack, manual first crack, drop, cooling,
  completion, fault, or stop are ignored before calling the backend or store.
- Added regression coverage proving confirmed detector output after bean drop is
  ignored without mutating the timeline or invoking the backend.

## Pull Request

Opened `PR #101`: <https://github.com/syamaner/coffee-roaster-mcp/pull/101>

PR branch:

- `feature/39-integrate-first-crack-with-session-timeline`

Commits on the branch before this summary:

- `28bfc8ab7867b516ae4fddbc17332a4f7b8843b5` -
  `feat: integrate first crack with session timeline`
- `2e902191dadfb45a779cc714521706c95090ecd8` -
  `fix: preserve detected first crack timestamp`
- `118bfe7bbb304c6bf0d819fcdd77a87c18ecdb59` -
  `fix: allow inferred detector window timestamp`
- `b0fdd1bd7a5d4bfbafc32a0268ff54314856ace2` -
  `fix: ignore detector confirmations before beans`
- `afbd65ea67d4fbc7221441279751f4cfaff43da2` -
  `fix: restrict inferred timestamp tolerance`
- `7a26c04816f0e655027abb686c95e80cb17b225a` -
  `docs: add e4 s9 session summary`
- latest local commit -
  `fix: ignore detector output outside roasting`

PR status when this summary was written:

- state: open
- draft: false
- mergeable: true
- head: latest local commit after the post-roasting lifecycle fix
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed
- Thread-aware refresh after the latest fix showed the newest review thread
  outdated; prior actionable threads were resolved.
- A later Codex review on `afbd65ea67` found the post-roasting lifecycle gap;
  the latest local commit addresses it and is ready for CI after push.

Issue `#39` remains open and should close when PR #101 is merged through
`Closes #39`.

## Validation

Initial E4-S9 implementation:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py`:
  `13 passed`
- Ran `./.venv/bin/python -m pytest`: `234 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #101 passed.

After preserving detector-provided authoritative timing:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
  `51 passed`
- Ran `./.venv/bin/python -m pytest`: `234 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #101 passed.

After accepting inferred window-end timestamps:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
  `52 passed`
- Ran `./.venv/bin/python -m pytest`: `235 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #101 passed.

After ignoring pre-beans detector confirmations:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
  `53 passed`
- Ran `./.venv/bin/python -m pytest`: `236 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #101 passed.

After restricting tolerance to inferred timestamps:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
  `54 passed`
- Ran `./.venv/bin/python -m pytest`: `237 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #101 passed.

After ignoring detector output outside active `roasting`:

- Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
  `55 passed`
- Ran `./.venv/bin/python -m pytest`: `238 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

## Handoff Notes

After PR #101 merges:

1. Sync `main`.
2. Verify PR #101 is merged and issue #39 is closed.
3. Read `AGENTS.md`, `docs/state/registry.md`,
   `docs/state/epics/coffee-roaster-mcp-v0.1.md`, this summary, and GitHub
   issue `#40` if E4-S10 remains tracked there.
4. Begin E4-S10 from updated `main` on the issue-specific branch.
5. Keep E4-S10 scoped to targeted first-crack/MCP/export coverage hardening.
   Do not add model training, ONNX export, Hugging Face sync, local directory
   sync behavior, broad runtime redesign, or live Hottop control changes unless
   the E4-S10 issue explicitly requires it.
