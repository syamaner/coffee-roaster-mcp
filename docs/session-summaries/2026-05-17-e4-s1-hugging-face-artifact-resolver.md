# E4-S1 Hugging Face Artifact Resolver Session

## Scope

This session resumed after `PR #90` for `E3-S9` was merged, issue `#31` was
closed, and issue `#62` was closed. Work started from updated `main` on branch
`feature/32-hugging-face-artifact-resolver` for issue `#32`, `E4-S1: Add
Hugging Face artifact resolver`.

The story goal was intentionally narrow: resolve released Hugging Face model
artifacts from the configured first-crack repository and revision. The work did
not add model training, ONNX export, Hugging Face sync, detector startup,
precision-specific ONNX selection, local offline directory handling, or MCP
session integration.

## Context Usage

Session usage snapshot supplied by the operator near the end of the story:

- Context window: `65% left (97.5K used / 258K)`
- 5h limit: `95% left`, resets `17:12`
- Weekly limit: `99% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `20:03`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `15:03 on 24 May`

This was a moderate-context story. The implementation itself was small, but the
session included post-merge verification for Epic 3, branch setup from updated
`main`, E4 durable-state updates, PR creation, and one automated review-fix
round.

## Pre-Story Verification

Before starting E4-S1:

- Verified `PR #90` was merged into `main`.
- Verified issue `#31` was closed as completed.
- Verified issue `#62` was closed as completed.
- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to commit
  `5a5796d321941bc90575a36b1ecf3d0740b986f6`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`, and GitHub issue `#32`.

## Implementation

Added:

- `src/coffee_roaster_mcp/artifacts.py`
- `tests/test_artifacts.py`

The resolver:

- accepts a repository-relative artifact filename
- uses `FirstCrackConfig.repo_id`
- uses `FirstCrackConfig.revision`
- calls Hugging Face Hub through a small injectable downloader boundary
- returns the local Hugging Face cache path plus repo, revision, and filename
  metadata
- wraps download failures with artifact, repo, and revision context
- rejects empty, absolute, parent-traversal, and Windows-style backslash paths
  before calling the downloader

Added runtime dependency:

- `huggingface_hub>=0.23,<1`

The Hub import is lazy so mocked resolver tests do not require network access or
a real download.

## Documentation And State

Updated durable project state:

- `docs/state/registry.md` now marks E4-S1 complete and points next at E4-S2.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E4-S1` complete,
  records the resolver decision, and adds validation notes.

## Pull Request

Opened `PR #91`: <https://github.com/syamaner/coffee-roaster-mcp/pull/91>

PR branch:

- `feature/32-hugging-face-artifact-resolver`

Commits:

- `84797b851f2b06199cf526bbd7296fb71f5919f0` -
  `feat: add hugging face artifact resolver`
- `cd95f95238b077dab3a954f955d2c6c9dcbce6a5` -
  `fix: reject backslash artifact paths`

PR status after the review fix:

- state: open
- draft: false
- mergeable: yes
- GitHub Actions `Checks`: passed
- GitHub Actions `Build Package`: passed

Issue `#32` was commented with what changed and how it was tested.

## Review Response

Codex review `4305540514` found one actionable issue:

- `_validate_hub_filename` rejected POSIX `..` traversal but allowed
  Windows-style backslash paths such as `..\model.onnx` and
  `onnx\..\model.onnx`.

Fix:

- `_validate_hub_filename` now rejects any backslash before path parsing.
- Regression tests cover both Windows-style examples from the review.

No Copilot code review findings were available because Copilot reported a review
error.

## Validation

Before opening PR #91:

- Ran `./.venv/bin/python -m pytest`: `184 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After the review fix:

- Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: `11 passed`
- Ran `./.venv/bin/python -m ruff check src/coffee_roaster_mcp/artifacts.py tests/test_artifacts.py`: passed
- Ran `./.venv/bin/python -m ruff format --check src/coffee_roaster_mcp/artifacts.py tests/test_artifacts.py`: passed
- Ran `./.venv/bin/python -m pyright src/coffee_roaster_mcp/artifacts.py tests/test_artifacts.py`: `0 errors`
- Ran `./.venv/bin/python -m pytest`: `186 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

GitHub Actions after pushing the review fix:

- `Checks`: passed
- `Build Package`: passed

## Handoff Notes

The branch is clean and pushed at
`cd95f95238b077dab3a954f955d2c6c9dcbce6a5`.

After PR #91 merges:

1. Sync `main`.
2. Verify issue `#32` closes.
3. Begin E4-S2 from updated `main`.
4. Keep E4-S2 scoped to default INT8 ONNX selection, using the resolver from
   this story.

Do not add model training, ONNX export, Hugging Face sync, local offline
directory support, detector startup, or MCP session integration as part of
E4-S2 unless the story scope is explicitly changed.
