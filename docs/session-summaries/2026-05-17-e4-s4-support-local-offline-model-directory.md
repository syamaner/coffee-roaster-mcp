# E4-S4 Support Local Offline Model Directory Session

## Scope

This session resumed after `PR #93` for `E4-S3` was squashed and merged, and
issue `#34` was closed. Work started from updated `main` on branch
`feature/35-support-local-offline-model-directory` for issue `#35`, `E4-S4:
Support local offline model directory`.

The story goal was intentionally narrow: make `first_crack.local_model_dir`
resolve released first-crack artifacts from local storage without Hugging Face
network access, using the existing E4-S1 through E4-S3 artifact resolver and
ONNX filename selection boundary. The work did not add model training, ONNX
export, Hugging Face sync, detector startup, audio capture, broad artifact
validation, local directory sync behavior, or MCP session integration.

## Context Usage

Session usage snapshot supplied by the operator after PR creation:

- Context window: `69% left (88.5K used / 258K)`
- 5h limit: `93% left`, resets `17:12`
- Weekly limit: `98% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `21:18`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `16:18 on 24 May`

## Pre-Story Verification

Before starting E4-S4:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to the E4-S3 squash
  merge commit `ce4a3f91668aa90205813e3207c595ecf21c5cbc`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s3-load-fp32-onnx-by-config.md`,
  and GitHub issue `#35`.
- Confirmed issue `#35` required `local_model_dir` to work without Hugging Face
  network access, missing local artifacts to fail clearly, and offline local
  directory tests.

## Implementation

Updated:

- `src/coffee_roaster_mcp/artifacts.py`
- `tests/test_artifacts.py`

Behavior added:

- `resolve_hugging_face_artifact(...)` now validates the repository-relative
  artifact filename, then checks `FirstCrackConfig.local_model_dir`.
- When `local_model_dir` is configured, the resolver joins the local directory
  with the same repository-relative artifact path used for released Hub
  artifacts.
- Local resolution returns `ResolvedArtifact` without calling the Hugging Face
  downloader.
- Missing local files raise `ArtifactResolutionError` with the repository-
  relative filename, configured local directory, and computed local path.
- When `local_model_dir` is unset, existing Hugging Face Hub resolution behavior
  is unchanged.

The existing E4-S2/E4-S3 ONNX selector continues to choose:

- `onnx/int8/model_quantized.onnx` for `precision="int8"`
- `onnx/fp32/model.onnx` for `precision="fp32"`

## Tests

Added offline resolver coverage for:

- default INT8 ONNX local model selection without downloader use
- configured FP32 ONNX local model selection without downloader use
- missing local ONNX model failure before downloader use

## Documentation And State

Updated durable project state:

- `docs/state/registry.md` now marks E4-S4 complete and points next at E4-S5.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E4-S4` complete,
  records the local directory resolver decision, and adds validation notes.

## Pull Request

Opened `PR #94`: <https://github.com/syamaner/coffee-roaster-mcp/pull/94>

PR branch:

- `feature/35-support-local-offline-model-directory`

Implementation commit before this session-summary update:

- `c34048b8c8a1ff646f21b408049163fdb43f4f41` -
  `feat: resolve local first crack artifacts`

PR status before this session-summary update:

- state: open
- draft: false
- mergeable: yes
- head: `c34048b8c8a1ff646f21b408049163fdb43f4f41`
- GitHub Actions `Checks`: passed
- GitHub Actions `Build Package`: passed

Issue `#35` was commented with what changed and how it was tested.

## Validation

Before opening PR #94:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `18 passed`
- Ran `./.venv/bin/python -m pytest`: `193 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- Ran `git diff --check`: clean

GitHub Actions after the implementation push:

- `Checks`: passed
- `Build Package`: passed

## Handoff Notes

After PR #94 merges:

1. Sync `main`.
2. Verify issue `#35` closes.
3. Begin E4-S5 from updated `main`.
4. Keep E4-S5 scoped to validating required detector artifacts before detection
   starts.

Do not add model training, ONNX export, Hugging Face sync, detector startup
beyond validation prerequisites, audio capture, local directory sync behavior,
or MCP session integration as part of E4-S5 unless the story scope explicitly
changes.
