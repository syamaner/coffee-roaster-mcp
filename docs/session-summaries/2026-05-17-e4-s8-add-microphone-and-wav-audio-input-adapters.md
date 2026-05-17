# E4-S8 Add Microphone And WAV Audio Input Adapters Session

## Scope

This session resumed after `PR #98` for `E4-S7` was squashed and merged, and
issue `#38` was closed. Work started from updated `main` on branch
`feature/97-add-microphone-and-wav-audio-input-adapters` for issue `#97`,
`E4-S8: Add microphone and WAV audio input adapters`.

The story goal was to add concrete microphone and recorded WAV audio input
adapters behind the E4-S6 `AudioInput` boundary. The work preserved the Epic 2
one-session store boundary, MCP semantics, mock-safe defaults, coverage
workflow, Epic 3 Hottop safety/validation boundary, and the E4-S1 through E4-S7
released-artifact, audio-pipeline, and detector-adapter boundaries.

The work did not add detector inference, ONNX export, model training, Hugging
Face sync, local directory sync behavior, first-crack session timeline
integration, broad coverage hardening, or live Hottop control changes.

## Context Usage

Session usage snapshot supplied by the operator after the E4-S8 review fix:

- Context window: `51% left (133K used / 258K)`
- 5h limit: `95% left`, resets `22:35`
- Weekly limit: `97% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `00:18 on 18 May`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `19:18 on 24 May`

## Pre-Story Verification

Before starting E4-S8:

- Ran `git checkout main`.
- Ran `git pull --ff-only origin main`, fast-forwarding through the E4-S7 merge
  to `aa42939`.
- Read `AGENTS.md`, `docs/state/registry.md`,
  `docs/state/epics/coffee-roaster-mcp-v0.1.md`,
  `docs/session-summaries/2026-05-17-e4-s7-add-detector-adapter.md`, and GitHub
  issue `#97`.
- Confirmed issue `#97` required microphone and recorded WAV source selection,
  WAV replay through the same mono float sample contract, mocked microphone
  tests, and Linux/Raspberry Pi suitability without real audio hardware in CI.

## Implementation

Updated:

- `src/coffee_roaster_mcp/config.py`
- `src/coffee_roaster_mcp/audio.py`
- `tests/test_config.py`
- `tests/test_audio.py`
- `pyproject.toml`
- `README.md`
- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

Behavior added:

- `AudioConfig` now includes `source: microphone|wav` and `wav_path`.
- Environment overrides now include `COFFEE_AUDIO_SOURCE`,
  `COFFEE_AUDIO_SAMPLE_RATE`, and `COFFEE_AUDIO_WAV_PATH`.
- `build_audio_capture_pipeline(...)` can use a default
  `build_configured_audio_input(...)` factory while preserving injected factories
  for tests.
- `WavAudioInput` reads PCM WAV files through Python stdlib `wave`, supports
  8/16/24/32-bit PCM sample widths, converts multi-channel WAV frames to mono,
  emits normalized float samples, and fails clearly when the file sample rate
  differs from configured `audio.sample_rate`.
- `MicrophoneAudioInput` opens a lazy PortAudio-backed
  `sounddevice.RawInputStream` with configured device and sample rate, reads
  mono float32 samples, and maps backend open/read/overflow failures to
  `AudioCaptureError`.
- `sounddevice>=0.5,<1` was added as a declared runtime dependency.
- README now documents `audio.source`, WAV replay behavior, microphone device
  selection, Linux/Raspberry Pi `arecord -l` / `arecord -L`, and why
  `plughw:...` identifiers are often more forgiving than raw `hw:...` devices.

Tests added:

- Config defaults and YAML/env overrides cover audio source, sample rate, and
  WAV path.
- Invalid audio source values fail with config context.
- Generated PCM WAV fixtures prove WAV source construction, mono sample output,
  stereo-to-mono conversion, EOF behavior, and sample-rate mismatch failures.
- Mocked microphone backend tests prove the configured device/sample-rate values
  are passed to the PortAudio stream without touching real audio hardware.
- Microphone overflow is reported as `AudioCaptureError`.
- WAV and microphone sources feed detector windows through the same
  `AudioCapturePipeline` contract.

## Review Fix

Codex review on `PR #100` found one actionable issue:

- If `sounddevice.RawInputStream(...)` succeeded but `start()` failed, the
  partially initialized stream was not closed before raising `AudioCaptureError`.

Fix applied:

- `MicrophoneAudioInput` now tracks the created stream during startup and closes
  it in the failure path before re-raising.
- Added a mocked regression test proving a stream is closed when `start()` raises.

## Pull Request

Opened `PR #100`: <https://github.com/syamaner/coffee-roaster-mcp/pull/100>

PR branch:

- `feature/97-add-microphone-and-wav-audio-input-adapters`

Commits on the branch before this summary:

- `1597bff663596c0e46ba5ddc0aeb5347e71d48f1` -
  `feat: add microphone and wav audio inputs`
- `deb1e44c92584efca184dd8b37eab06a86e5106b` -
  `docs: document microphone selection`
- `8c8e0f9e84cbb2470414c536db6d7fe44d765f97` -
  `fix: close microphone stream on startup failure`

PR status when this summary was written:

- state: open
- draft: false
- mergeable: true
- head: `8c8e0f9e84cbb2470414c536db6d7fe44d765f97`
- GitHub Actions `Build Package`: passed
- GitHub Actions `Checks`: passed

Issue `#97` remains open and should close when PR #100 is merged through
`Closes #97`.

## Validation

Initial E4-S8 implementation:

- Ran `./.venv/bin/python -m pytest tests/test_audio.py tests/test_config.py`:
  `30 passed`
- Ran `./.venv/bin/python -m pytest`: `226 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`

After microphone selection documentation:

- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed

After review fix:

- Ran `./.venv/bin/python -m pytest tests/test_audio.py`: `18 passed`
- Ran `./.venv/bin/python -m pytest`: `227 passed`
- Ran `./.venv/bin/python -m ruff check .`: passed
- Ran `./.venv/bin/python -m ruff format --check .`: passed
- Ran `./.venv/bin/python -m pyright`: `0 errors`
- GitHub Actions on PR #100 passed after the review-fix commit.

## Handoff Notes

After PR #100 merges:

1. Sync `main`.
2. Verify issue `#97` closes.
3. Begin E4-S9 from updated `main`.
4. Keep E4-S9 scoped to integrating confirmed detector output into the
   authoritative session timeline.
5. Do not add broader coverage hardening in E4-S9; E4-S10 owns the final Epic 4
   coverage pass.
6. Preserve mock-safe defaults and avoid real microphone requirements in CI.

Suggested restart prompt after PR #100 is merged:

```text
Resume in /Users/sertanyamaner/git/coffee-roaster-mcp. PR #100 for E4-S8 was squashed and merged, and issue #97 is closed. First run git checkout main and git pull --ff-only origin main. Then read AGENTS.md, docs/state/registry.md, docs/state/epics/coffee-roaster-mcp-v0.1.md, docs/session-summaries/2026-05-17-e4-s8-add-microphone-and-wav-audio-input-adapters.md, and GitHub issue #39. Begin E4-S9 from updated main on branch feature/39-integrate-first-crack-with-session-timeline. Keep scope to integrating confirmed detector output into the authoritative session timeline exactly once, using the existing E4-S1 through E4-S8 resolver, validation, audio-source, audio-pipeline, and detector-adapter boundaries. Preserve the Epic 2 one-session store boundary, MCP semantics, mock-safe defaults, coverage workflow, Epic 3 Hottop safety/validation boundary, and optional/gated real microphone validation. Do not add model training, ONNX export, Hugging Face sync, local directory sync behavior, broad coverage hardening, or live Hottop control changes unless issue #39 explicitly requires it.
```
