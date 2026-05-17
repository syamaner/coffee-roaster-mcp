# E4-S6 Add Audio Capture Pipeline Session

## Scope

This session resumed after `PR #95` for `E4-S5` was squashed and merged, and
issue `#36` was closed. Work started from updated `main` on branch
`feature/37-add-audio-capture-pipeline` for issue `#37`, `E4-S6: Add audio
capture pipeline`.

The story goal was intentionally narrow: add the audio capture pipeline only,
so configured audio input can feed detector windows without blocking roaster
telemetry. The work preserved the Epic 2 one-session store boundary, MCP
semantics, mock-safe defaults, coverage workflow, Epic 3 Hottop
safety/validation boundary, and the E4-S1 through E4-S5 released-artifact
resolver/validation boundary.

The work did not add model training, ONNX export, Hugging Face sync, detector
adapter behavior, ONNX inference, local directory sync behavior, first-crack
session timeline integration, live microphone backend selection, or live Hottop
control changes.

## Context Usage

Session usage snapshot supplied by the operator after the review-fix push:

- Context window: `37% left (168K used / 258K)`
- 5h limit: `98% left`, resets `22:35`
- Weekly limit: `98% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `23:13`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `18:13 on 24 May`
- Warning: limits may be stale; run `/status` again shortly.

## Pre-Story Verification

Before starting E4-S6:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding to the E4-S5 squash
  merge commit `b4b07a76725dd9e3bc0a31a0cb8295562e11d853`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s5-validate-required-detector-artifacts.md`,
  and GitHub issue `#37`.
- Confirmed issue `#37` required configured audio input to feed detector
  windows without blocking roaster telemetry, with mocked audio pipeline tests.

## Implementation

Updated:

- `src/coffee_roaster_mcp/audio.py`
- `tests/test_audio.py`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`
- `docs/state/github-issues.md`

Behavior added:

- `AudioInput` and `AudioInputFactory` protocols define the audio-source
  boundary for later microphone and WAV implementations.
- `AudioCaptureSettings` builds validated capture settings from `AudioConfig`.
- `AudioWindow` represents a complete mono detector window.
- `AudioCapturePipeline` runs audio reads on a daemon worker thread and emits
  one-second detector windows at the configured sample rate.
- Detector-window handoff uses a bounded queue with `put_nowait`; if the
  downstream detector queue is full, windows are dropped and counted instead of
  blocking capture.
- `build_audio_capture_pipeline(...)` constructs the pipeline from
  configuration and an injected input factory.

Tests added:

- Config-driven audio source construction.
- Invalid audio capture settings.
- Mocked sample input windowing.
- Bounded queue drop behavior.
- Source error capture.
- Double-start protection.
- Finite sample validation.
- Restart state reset.
- Blocking detector consumer preservation across restart.

## Planning Update

During review discussion, the gap between the generic E4-S6 audio pipeline and
concrete audio sources was made explicit:

- Created GitHub issue `#97`: `E4-S8: Add microphone and WAV audio input
  adapters`.
- Renamed issue `#39` from `E4-S8` to `E4-S9: Integrate first crack with
  session timeline`.
- Updated Epic `#4` to list the sequence `E4-S7 -> E4-S8 -> E4-S9`.
- Updated `docs/state/github-issues.md`, `docs/state/registry.md`, and
  `docs/state/epics/coffee-roaster-mcp-v0.1.md` with the new story ordering.

The new `E4-S8` issue captures Linux/Raspberry Pi microphone behavior and WAV
replay explicitly, keeping both behind the E4-S6 `AudioInput` boundary and out
of E4-S7 detector-adapter scope.

## Review Fixes

First Codex review on `PR #96`:

- Review URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/96#pullrequestreview-4305911152>
- Finding: restarting a pipeline reused partial samples and sequence state from
  the previous run.
- Fix commit:
  `becfd8343d59919b6be1b5fe3fe3e9068d705547` -
  `fix: reset audio capture run state`
- Fix: reset run-scoped state on each `AudioCapturePipeline.start()` so partial
  samples, queued windows, sequence numbers, counters, and prior errors cannot
  leak across stopped/restarted pipeline instances.
- Added restart regression coverage.

Second Codex review on `PR #96`:

- Review URL:
  <https://github.com/syamaner/coffee-roaster-mcp/pull/96#pullrequestreview-4305947834>
- Finding: the first review fix replaced the queue object on restart, which
  could strand detector consumers already blocked on the old queue.
- Fix commit:
  `bb790c00a7767f3c0bf2b1f1f37e1cb8c86ab750` -
  `fix: keep audio queue stable on restart`
- Fix: reset now drains and reuses the existing queue instead of replacing it.
- Added blocking-consumer restart coverage proving a consumer waiting before
  restart receives the next window after capture restarts.

Both review threads were checked through thread-aware GitHub GraphQL reads and
were marked resolved in GitHub after the fixes.

## Pull Request

Opened `PR #96`: <https://github.com/syamaner/coffee-roaster-mcp/pull/96>

PR branch:

- `feature/37-add-audio-capture-pipeline`

Commits on the branch:

- `c9a6a5fb9b01b3d522049d3efb5c51b7e35418c6` -
  `feat: add audio capture pipeline`
- `6d64d8f08a7ad3e84d1f9057aaddbf704bfeac32` -
  `docs: insert audio input adapter story`
- `becfd8343d59919b6be1b5fe3fe3e9068d705547` -
  `fix: reset audio capture run state`
- `bb790c00a7767f3c0bf2b1f1f37e1cb8c86ab750` -
  `fix: keep audio queue stable on restart`

PR status at the time this summary was written:

- state: open
- draft: false
- mergeable: yes
- head: `bb790c00a7767f3c0bf2b1f1f37e1cb8c86ab750`
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed

Issue `#37` remains open and should close when PR #96 is merged through
`Closes #37`.

## Validation

Initial E4-S6 implementation:

- Ran `./.venv/bin/python -m pytest tests/test_audio.py`: `8 passed`
- Ran `./.venv/bin/python -m pytest`: `208 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After first review fix:

- Ran `./.venv/bin/python -m pytest tests/test_audio.py`: `9 passed`
- Ran `./.venv/bin/python -m pytest`: `209 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After second review fix:

- Ran `./.venv/bin/python -m pytest tests/test_audio.py`: `10 passed`
- Ran `./.venv/bin/python -m pytest`: `210 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

GitHub Actions after the final review-fix push:

- `Build Package`: passed
- `Checks`: passed

## Handoff Notes

After PR #96 merges:

1. Sync `main`.
2. Verify issue `#37` closes.
3. Begin E4-S7 from updated `main`.
4. Keep E4-S7 scoped to the detector adapter only.

Do not add concrete microphone/WAV adapters in E4-S7; those are now captured in
`E4-S8` / issue `#97`. Do not add first-crack session timeline integration in
E4-S7 or E4-S8; that remains `E4-S9` / issue `#39`.

Suggested restart prompt after PR #96 is merged:

```text
Resume in /Users/sertanyamaner/git/coffee-roaster-mcp. PR #96 for E4-S6 was squashed and merged, and issue #37 is closed. First run git checkout main and git pull --ff-only origin main. Then read AGENTS.md, docs/state/registry.md, docs/state/epics/coffee-roaster-mcp-v0.1.md, docs/session-summaries/2026-05-17-e4-s6-add-audio-capture-pipeline.md, and GitHub issue #38. Begin E4-S7 from updated main on branch feature/38-add-detector-adapter. Keep scope to the detector adapter only. Preserve the Epic 2 one-session store boundary, MCP semantics, mock-safe defaults, coverage workflow, Epic 3 Hottop safety/validation boundary, and the E4-S1 through E4-S6 released-artifact and audio-pipeline boundaries. Do not add model training, ONNX export, Hugging Face sync, concrete microphone or WAV input adapters, local directory sync behavior, first-crack session timeline integration, or live Hottop control changes unless issue #38 explicitly requires it.
```
