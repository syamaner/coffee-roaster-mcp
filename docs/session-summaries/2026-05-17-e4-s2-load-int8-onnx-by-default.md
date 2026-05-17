# E4-S2 Load INT8 ONNX By Default Session

## Scope

This session resumed after `PR #91` for `E4-S1` was squashed and merged, and
issue `#32` was closed. Work started from updated `main` on branch
`feature/33-load-int8-onnx-by-default` for issue `#33`, `E4-S2: Load INT8 ONNX
by default`.

The story goal was intentionally narrow: select the released INT8 ONNX model
artifact by default using the E4-S1 Hugging Face artifact resolver. The work did
not add model training, ONNX export, Hugging Face sync, detector startup, audio
capture, local offline directory handling, artifact validation, or MCP session
integration.

## Context Usage

Session usage snapshot supplied by the operator near the end of the story:

- Context window: `71% left (83.9K used / 258K)`
- 5h limit: `94% left`, resets `17:12`
- Weekly limit: `99% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `20:14`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `15:14 on 24 May`
- Warning: `limits may be stale - run /status again shortly`

This was a low-code-change story. Most of the session was spent on branch setup
from updated `main`, reading the required durable state and issue context,
scoped implementation, validation, state updates, PR creation, and CI
confirmation.

## Pre-Story Verification

Before starting E4-S2:

- Verified from the operator handoff that `PR #91` was squashed and merged and
  issue `#32` was closed.
- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to the E4-S1 squash
  merge commit `a4f54bd7139549b33176f079c6059c17edcb9c83`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s1-hugging-face-artifact-resolver.md`,
  and GitHub issue `#33`.

## Implementation

Updated:

- `src/coffee_roaster_mcp/artifacts.py`
- `tests/test_artifacts.py`

Added:

- `INT8_ONNX_MODEL_FILENAME = "onnx/int8/model_quantized.onnx"`
- `resolve_first_crack_onnx_model(...)`

The selector:

- uses `FirstCrackConfig.precision`
- resolves `onnx/int8/model_quantized.onnx` for the default `int8` precision
- also resolves the same artifact for explicit `precision="int8"`
- delegates repository, revision, filename validation, and Hub download behavior
  to `resolve_hugging_face_artifact(...)`
- keeps `fp32` selection deferred to `E4-S3` and fails before downloader use for
  that precision

## Documentation And State

Updated durable project state:

- `docs/state/registry.md` now marks E4-S2 complete and points next at E4-S3.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E4-S2` complete,
  records the INT8 selector decision, and adds validation notes.

## Pull Request

Opened `PR #92`: <https://github.com/syamaner/coffee-roaster-mcp/pull/92>

PR branch:

- `feature/33-load-int8-onnx-by-default`

Commit before this session-summary update:

- `922ba6df7f576ea624a8b6b46d67d7e8cd7dfc7b` -
  `feat: select int8 onnx artifact`

PR status before this session-summary update:

- state: open
- draft: false
- mergeable: yes
- GitHub Actions `Checks`: passed
- GitHub Actions `Build Package`: passed

Issue `#33` was commented with what changed and how it was tested.

## Validation

Before opening PR #92:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `14 passed`
- Ran `./.venv/bin/python -m pytest`: `189 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

GitHub Actions after opening PR #92:

- `Checks`: passed
- `Build Package`: passed

## Handoff Notes

After PR #92 merges:

1. Sync `main`.
2. Verify issue `#33` closes.
3. Begin E4-S3 from updated `main`.
4. Keep E4-S3 scoped to selecting `onnx/fp32/model.onnx` for configured
   `precision: fp32` using the same resolver boundary.

Do not add model training, ONNX export, Hugging Face sync, detector startup,
audio capture, local offline directory support, artifact validation, or MCP
session integration as part of E4-S3 unless the story scope explicitly changes.
