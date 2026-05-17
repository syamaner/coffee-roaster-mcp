# E4-S7 Add Detector Adapter Session

## Scope

This session resumed after `PR #96` for `E4-S6` was squashed and merged, and
issue `#37` was closed. Work started from updated `main` on branch
`feature/38-add-detector-adapter` for issue `#38`, `E4-S7: Add detector
adapter`.

The story goal was intentionally narrow: add the detector adapter only, using
the E4-S1 through E4-S6 resolver, artifact-validation, and audio-window
boundaries. The work preserved the Epic 2 one-session store boundary, MCP
semantics, mock-safe defaults, coverage workflow, Epic 3 Hottop
safety/validation boundary, and released-artifact boundary.

The work did not add model training, ONNX export, Hugging Face sync, concrete
microphone or WAV input adapters, local directory sync behavior, first-crack
session timeline integration, live Hottop control changes, detector startup, or
real ONNX runtime inference.

## Context Usage

Session usage snapshot supplied by the operator after the E4-S10 planning
update:

- Context window: `50% left (135K used / 258K)`
- 5h limit: `96% left`, resets `22:35`
- Weekly limit: `97% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `23:57`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `18:57 on 24 May`

## Pre-Story Verification

Before starting E4-S7:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the E4-S6 merge
  to `fb51b02d15e40be3ff1dcb85a06a869fedf73b0b`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s6-add-audio-capture-pipeline.md`,
  and GitHub issue `#38`.
- Confirmed issue `#38` required detector output to map to a confirmed
  first-crack event with timestamp, precision, revision, and optional
  confidence, with mocked detector adapter tests.

## Implementation

Updated:

- `src/coffee_roaster_mcp/detector.py`
- `tests/test_detector.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior added:

- `FirstCrackDetectorBackend` protocol defines the injected detector backend
  boundary.
- `FirstCrackDetectorOutput` represents raw backend output for one E4-S6
  `AudioWindow`.
- `FirstCrackDetectionEvent` represents the confirmed first-crack event
  candidate for later session-timeline integration.
- `FirstCrackDetectorAdapter.process_window(...)` ignores unconfirmed outputs
  and maps confirmed outputs to `first_crack_detected` event candidates.
- Confirmed event candidates include monotonic timestamp, configured precision,
  configured revision, repo id, resolved ONNX artifact filename, resolved
  feature-extractor filename, source audio window sequence number, and optional
  confidence.
- When a backend does not provide a detection timestamp, the adapter uses the
  end of the audio window.
- Invalid confidence and non-finite detection timestamps fail clearly.

Tests added:

- Unconfirmed detector output returns no event candidate.
- Confirmed detector output maps precision, revision, artifacts, timestamp,
  confidence, repo id, and window sequence metadata.
- Missing detector timestamp falls back to the audio-window end timestamp.
- Invalid confidence values are rejected.
- Non-finite detector timestamps are rejected.
- Non-boolean confirmation values are rejected.

## Planning Update

After reviewing coverage, the project now has a closing Epic 4 coverage story:

- Created GitHub issue `#99`: `E4-S10: Harden first-crack and MCP coverage
  before next epic`.
- Updated GitHub Epic `#4` to include `#99`.
- Updated `docs/state/github-issues.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`, and
  `docs/state/registry.md`.

E4-S10 is placed after E4-S9. It is intended to close Epic 4 with targeted
automated coverage for the assembled first-crack path, MCP-facing behavior,
current export surfaces, duplicate/no-confirmation/error cases,
disabled/manual modes, missing artifacts, and coverage gaps in `mcp_server.py`,
`exports.py`, and the Epic 4 modules.

Manual real microphone validation is explicitly optional and gated. It must be
skipped by default and must not be required for normal CI.

## Coverage Snapshot

Local coverage was checked after E4-S7:

- Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json`
- Result: `218 passed`
- Total package coverage: `87%`

Notable module coverage:

- `artifacts.py`: `94%`
- `audio.py`: `96%`
- `detector.py`: complete enough to be skipped from the missing-lines report
- `drivers.py`: `95%`
- `hottop_validation.py`: `94%`
- `session.py`: `88%`
- `config.py`: `83%`
- `mcp_server.py`: `55%`
- `exports.py`: `43%`

The main remaining coverage risks are `mcp_server.py`, `exports.py`, and some
configuration edge paths. E4-S10 was added to address these after E4-S9 makes
the assembled first-crack path available.

## Pull Request

Opened `PR #98`: <https://github.com/syamaner/coffee-roaster-mcp/pull/98>

PR branch:

- `feature/38-add-detector-adapter`

Commits on the branch before this summary:

- `ba647c090071cb1e385463dc295725bb3301b32d` -
  `feat: add first crack detector adapter`
- `3e2a5bc14840e496003b1c4e6b1acd10475083c8` -
  `docs: add e4 s10 coverage hardening story`

PR status when this summary was written:

- state: open
- draft: false
- mergeable: true
- head: `3e2a5bc14840e496003b1c4e6b1acd10475083c8`
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed

Issue `#38` remains open and should close when PR #98 is merged through
`Closes #38`.

## Validation

E4-S7 implementation:

- Ran `./.venv/bin/python -m pytest tests/test_detector.py`: `8 passed`
- Ran `./.venv/bin/python -m pytest`: `218 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After the E4-S10 docs planning update:

- Targeted Markdown `ruff check` reported no Python files, as expected.
- Targeted Markdown `ruff format --check` was not applicable because Ruff
  Markdown formatting is experimental and requires preview mode.
- GitHub Actions on PR #98 passed after the planning commit.

## Handoff Notes

After PR #98 merges:

1. Sync `main`.
2. Verify issue `#38` closes.
3. Begin E4-S8 from updated `main`.
4. Keep E4-S8 scoped to concrete microphone and WAV audio input adapters behind
   the E4-S6 `AudioInput` boundary.
5. Do not add session timeline integration in E4-S8; that remains E4-S9.
6. Do not perform broad coverage hardening in E4-S8 or E4-S9 unless required by
   those issues; E4-S10 owns the final Epic 4 coverage hardening pass.

Suggested restart prompt after PR #98 is merged:

```text
Resume in /Users/sertanyamaner/git/coffee-roaster-mcp. PR #98 for E4-S7 was squashed and merged, and issue #38 is closed. First run git checkout main and git pull --ff-only origin main. Then read AGENTS.md, docs/state/registry.md, docs/state/epics/coffee-roaster-mcp-v0.1.md, docs/session-summaries/2026-05-17-e4-s7-add-detector-adapter.md, and GitHub issue #97. Begin E4-S8 from updated main on branch feature/97-add-microphone-and-wav-audio-input-adapters. Keep scope to concrete microphone and WAV audio input adapters behind the E4-S6 AudioInput boundary. Preserve the Epic 2 one-session store boundary, MCP semantics, mock-safe defaults, coverage workflow, Epic 3 Hottop safety/validation boundary, and the E4-S1 through E4-S7 released-artifact, audio-pipeline, and detector-adapter boundaries. Do not add model training, ONNX export, Hugging Face sync, local directory sync behavior, first-crack session timeline integration, broad coverage hardening, or live Hottop control changes unless issue #97 explicitly requires it. Real microphone validation must be optional and explicitly gated so normal CI remains mock-safe.
```
