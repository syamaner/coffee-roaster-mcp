# E4-S5 Validate Required Detector Artifacts Session

## Scope

This session resumed after `PR #94` for `E4-S4` was squashed and merged, and
issue `#35` was closed. Work started from updated `main` on branch
`feature/36-validate-required-detector-artifacts` for issue `#36`, `E4-S5:
Validate required detector artifacts before detection starts`.

The story goal was intentionally narrow: validate the released first-crack
detector artifact set before audio detection begins, using the existing E4-S1
through E4-S4 resolver boundary. The work did not add model training, ONNX
export, Hugging Face sync, detector startup beyond validation prerequisites,
audio capture, local directory sync behavior, artifact content validation, or
MCP session timeline integration.

## Context Usage

Session usage snapshot supplied by the operator after the review-fix push:

- Context window: `54% left (126K used / 258K)`
- 5h limit: `100% left`, resets `22:35`
- Weekly limit: `98% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `21:32`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `16:32 on 24 May`
- Warning: limits may be stale; run `/status` again shortly.

## Pre-Story Verification

Before starting E4-S5:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to the E4-S4 squash
  merge commit `afe377f5460ec8dbbbb97aa2b2d57623d5d89c7b`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s4-support-local-offline-model-directory.md`,
  and GitHub issue `#36`.
- Confirmed issue `#36` required missing ONNX model and missing feature
  extractor files to fail clearly before audio detection begins, with missing
  artifact tests.

## Implementation

Updated:

- `src/coffee_roaster_mcp/artifacts.py`
- `tests/test_artifacts.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior added:

- `resolve_first_crack_detector_artifacts(...)` is now the narrow pre-audio
  detector artifact validation entry point.
- The detector artifact set resolves the configured ONNX model plus the
  precision-specific feature extractor preprocessor config.
- INT8 validation resolves:
  - `onnx/int8/model_quantized.onnx`
  - `onnx/int8/preprocessor_config.json`
- FP32 validation resolves:
  - `onnx/fp32/model.onnx`
  - `onnx/fp32/preprocessor_config.json`
- Missing ONNX model artifacts fail before feature extractor resolution.
- Missing feature extractor config artifacts fail with repository, revision,
  and filename context.
- Local offline directory behavior remains delegated to the existing resolver,
  so local paths still resolve without Hugging Face network access.

## Review Fix

Copilot review on `PR #95` requested explicit coverage for a missing Hugging
Face ONNX model through `resolve_first_crack_detector_artifacts(...)`, including
proof that feature-extractor resolution is not attempted after the ONNX failure.

Added:

- `test_missing_hub_detector_onnx_model_fails_before_feature_extractor`

Review-fix commit:

- `da9440aa9ef29d670bdb812b016474733ab2c8b3` -
  `test: cover missing hub detector onnx`

The Copilot review thread was still marked unresolved in GitHub after the fix;
it was not manually resolved or replied to.

## Pull Request

Opened `PR #95`: <https://github.com/syamaner/coffee-roaster-mcp/pull/95>

PR branch:

- `feature/36-validate-required-detector-artifacts`

Implementation commit:

- `89e5c710838aad0a807e06b27a295b1f6033180b` -
  `feat: validate first crack detector artifacts`

Review-fix commit:

- `da9440aa9ef29d670bdb812b016474733ab2c8b3` -
  `test: cover missing hub detector onnx`

PR status before this context-summary update:

- state: open
- draft: false
- mergeable: yes
- head: `da9440aa9ef29d670bdb812b016474733ab2c8b3`
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed

Issue `#36` was commented with what changed and how it was tested.

## Validation

Before opening PR #95:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `24 passed`
- Ran `./.venv/bin/python -m pytest`: `199 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After the review-fix commit:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `25 passed`
- Ran `./.venv/bin/python -m pytest`: `200 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

GitHub Actions after the review-fix push:

- `Build Package`: passed
- `Checks`: passed

## Handoff Notes

After PR #95 merges:

1. Sync `main`.
2. Verify issue `#36` closes.
3. Begin E4-S6 from updated `main`.
4. Keep E4-S6 scoped to the audio capture pipeline.

Do not add model training, ONNX export, Hugging Face sync, detector adapter
behavior beyond what E4-S6 explicitly requires, local directory sync behavior,
or MCP session timeline integration as part of E4-S6 unless the story scope
explicitly changes.
