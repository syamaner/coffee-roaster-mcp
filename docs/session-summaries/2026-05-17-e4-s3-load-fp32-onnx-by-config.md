# E4-S3 Load FP32 ONNX By Config Session

## Scope

This session resumed after `PR #92` for `E4-S2` was squashed and merged, and
issue `#33` was closed. Work started from updated `main` on branch
`feature/34-load-fp32-onnx-by-config` for issue `#34`, `E4-S3: Load FP32 ONNX
by config`.

The story goal was intentionally narrow: select the released FP32 ONNX model
artifact when first-crack precision is configured as `fp32`, using the existing
E4-S1/E4-S2 Hugging Face artifact resolver boundary. The work did not add model
training, ONNX export, Hugging Face sync, detector startup, audio capture, local
offline directory handling, artifact validation, or MCP session integration.

## Context Usage

Session usage snapshot supplied by the operator after the PR review fix:

- Context window: `68% left (90.1K used / 258K)`
- 5h limit: `93% left`, resets `17:12`
- Weekly limit: `99% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `20:48`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `15:48 on 24 May`
- Warning: `limits may be stale - run /status again shortly`

## Pre-Story Verification

Before starting E4-S3:

- Verified from the operator handoff that `PR #92` was squashed and merged and
  issue `#33` was closed.
- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to the E4-S2 squash
  merge commit `4979dbb51ea328607e00dd25c9ebf8ab13658f72`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s2-load-int8-onnx-by-default.md`,
  and GitHub issue `#34`.

## Implementation

Updated:

- `src/coffee_roaster_mcp/artifacts.py`
- `tests/test_artifacts.py`

Added:

- `FP32_ONNX_MODEL_FILENAME = "onnx/fp32/model.onnx"`
- FP32 selection in `resolve_first_crack_onnx_model(...)`

The selector now:

- resolves `onnx/int8/model_quantized.onnx` for `precision="int8"`
- resolves `onnx/fp32/model.onnx` for `precision="fp32"`
- delegates repository, revision, filename validation, and Hub download behavior
  to `resolve_hugging_face_artifact(...)`
- preserves mock-safe behavior by using mocked downloaders in tests

## Review Fix

Codex reviewed `PR #93` at review `4305610617` and opened one actionable thread:

- Unsupported runtime precision values could bypass the static `ModelPrecision`
  type because `FirstCrackConfig` is a plain dataclass. Without a fallback
  branch, `resolve_first_crack_onnx_model(...)` could raise `UnboundLocalError`
  instead of the documented `ArtifactResolutionError`.

The fix:

- converts `config.precision` to a runtime string before matching
- raises `ArtifactResolutionError` for unsupported values before downloader use
- adds regression coverage using `cast(ModelPrecision, "INT8")`

After the fix, the review thread became outdated on GitHub. It was not manually
resolved.

## Documentation And State

Updated durable project state:

- `docs/state/registry.md` now marks E4-S3 complete and points next at E4-S4.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E4-S3` complete,
  records the FP32 selector decision, and adds validation notes.

## Pull Request

Opened `PR #93`: <https://github.com/syamaner/coffee-roaster-mcp/pull/93>

PR branch:

- `feature/34-load-fp32-onnx-by-config`

Commits before this session-summary update:

- `a2086da9371383a9ce362c35f6c208a1f8d2da8c` -
  `feat: select fp32 onnx artifact`
- `f5cca0cd2a94d219a74405551912e16dde7fbe70` -
  `fix: handle unsupported onnx precision`

PR status before this session-summary update:

- state: open
- draft: false
- mergeable: yes
- head: `f5cca0cd2a94d219a74405551912e16dde7fbe70`
- GitHub Actions `Checks`: passed
- GitHub Actions `Build Package`: passed

Issue `#34` was commented with what changed and how it was tested.

## Validation

Before opening PR #93:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `14 passed`
- Ran `./.venv/bin/python -m pytest`: `189 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After the PR review fix:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `15 passed`
- Ran `./.venv/bin/python -m pytest`: `190 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

GitHub Actions after the review-fix push:

- `Checks`: passed
- `Build Package`: passed

## Handoff Notes

After PR #93 merges:

1. Sync `main`.
2. Verify issue `#34` closes.
3. Begin E4-S4 from updated `main`.
4. Keep E4-S4 scoped to local offline model directory support.

Do not add model training, ONNX export, Hugging Face sync, detector startup,
audio capture, artifact validation, or MCP session integration as part of E4-S4
unless the story scope explicitly changes.
