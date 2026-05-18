# E4.1-S3 Released ONNX Detector Backend Session

## Scope

This session resumed after `PR #113` for the full-roast readiness roadmap
update was merged, `PR #110` for `E4.1-S2` was merged, issue `#105` was
closed, and issues `#111` and `#112` existed. Work started from updated `main`
on branch `feature/106-add-released-artifact-onnx-first-crack-detector-backend`
for issue `#106`, `E4.1-S3: Add released-artifact ONNX first-crack detector
backend`.

The story goal was to add the released-artifact ONNX first-crack detector
backend while preserving the mock default, Epic 2 one-session store boundary,
MCP semantics, fail-closed safety behavior, coverage workflow, Epic 3 Hottop
validation boundary, E4.1-S1 configured-driver control wiring, E4.1-S2
`get_roast_state` device/status response shape, and the E4.1-S6 automatic T0
story boundary.

## Context Usage

Final context snapshot supplied by the operator after confirming no PR feedback:

- Context window: `45% left (147K used / 258K)`
- 5h limit: `98% left`, resets `01:13 on 19 May`
- Weekly limit: `93% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `01:23 on 19 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `20:23 on 25 May`
- Warning: limits may be stale; run `/status` again shortly.

## Pre-Story Verification

Before starting E4.1-S3:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the roadmap
  update.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/github-issues.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-18-e4-1-s2-expose-roaster-device-state.md`,
  and GitHub issue `#106`.
- Created branch
  `feature/106-add-released-artifact-onnx-first-crack-detector-backend`.

## Implementation

Updated:

- `src/coffee_roaster_mcp/detector.py`
- `tests/test_detector.py`
- `pyproject.toml`
- `README.md`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior implemented:

- Added `OnnxFirstCrackDetectorBackend` for released first-crack ONNX artifacts.
- Added `build_released_onnx_first_crack_detector_backend(...)` for already
  resolved detector artifacts.
- Added `build_released_onnx_first_crack_detector_adapter(...)` so
  `first_crack.mode: audio` resolves configured INT8/FP32 artifacts through the
  existing Hugging Face/local resolver boundary before backend construction.
- The backend loads the precision-specific `preprocessor_config.json`, builds a
  local `transformers.ASTFeatureExtractor`, creates an ONNX Runtime CPU session
  for the resolved ONNX model, and converts logits into the existing detector
  output confidence path.
- Runtime dependencies are lazy at backend construction time, so mock/default
  config does not import or start ONNX Runtime or Transformers.
- Added declared runtime dependencies for `onnxruntime` and `transformers`.

Fail-fast behavior added:

- Rejects non-audio mode for the released ONNX backend.
- Rejects invalid `first_crack.onnx_threads`.
- Fails clearly for missing, malformed, non-object, or invalid
  `preprocessor_config.json`.
- Fails clearly when ONNX Runtime or `ASTFeatureExtractor` is unavailable.
- Rejects sample-rate mismatches between the audio window and preprocessor
  config.
- Rejects feature-extractor output without `input_values`, ONNX models without
  inputs, empty outputs, empty logits, and non-numeric logits.

Out of scope kept out:

- Session-owned detector startup or automatic first-crack detector lifecycle.
- Audio capture lifecycle wiring.
- Automatic T0 implementation.
- Rolling telemetry metrics and final log schemas.
- Model training, ONNX export, Hugging Face sync, real microphone validation,
  live Hottop validation, or broad release validation.

## Validation

- Ran `./.venv/bin/python -m pytest tests/test_detector.py tests/test_artifacts.py`:
  `43 passed`.
- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
  `272 passed`, required coverage `90.0%` reached, total coverage `90.45%`.
- Ran `./.venv/bin/python -m ruff check .`: passed.
- Ran `./.venv/bin/python -m ruff format --check .`: passed.
- Ran `./.venv/bin/python -m pyright`: `0 errors`.
- GitHub CI for `PR #114` passed `Checks` and `Build Package`.

## Pull Request Status

`PR #114` is open at
<https://github.com/syamaner/coffee-roaster-mcp/pull/114>. At summary time:

- PR state: open.
- Merge state: clean and mergeable.
- Review comments: none.
- Reviews: none.
- Branch:
  `feature/106-add-released-artifact-onnx-first-crack-detector-backend`.
- Commits before this summary:
  - `ea5aed1 feat: add released onnx detector backend`

## Handoff

Durable state now points to `E4.1-S4`, issue `#107`, for starting the
first-crack detection runtime with roast sessions. Continue to preserve normal
CI as mock-safe: no Hottop hardware, microphone, model download, or network
should be required.
