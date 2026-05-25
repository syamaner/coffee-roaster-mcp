# E7-S6a Sliding-Window First-Crack Validation

Date: 2026-05-25

Branch: `feature/150-align-mcp-first-crack-sliding-window-validation`

Issue: #150

## Scope

Aligned the RoastPilot MCP first-crack runtime with configurable sliding-window
detector validation before the full E7-S6 manual Warp roast.

Included:

- detector configuration for confidence threshold, min positive windows, and
  confirmation window seconds
- audio configuration for overlap and explicit hop seconds
- overlapping window emission in both realtime capture and detector-paced WAV
  replay
- recent-positive first-crack confirmation before writing exactly one
  `first_crack_detected` event
- configurable ONNX confidence threshold while preserving released-artifact
  resolution and backend boundaries
- first-crack artifact, confidence, and confirmation metadata in JSONL, CSV,
  and `summary.json`
- public MCP WAV replay validation with the committed fixture and pinned
  released INT8 Hugging Face artifacts

Excluded:

- live Hottop validation
- real microphone validation
- full end-to-end Warp manual roast validation
- model training, ONNX export, Hugging Face sync, model cards, or dataset cards
- live PyPI or MCP Registry publishing
- hardware-ready release labeling

## Prerequisite Verification

- PR #151 was merged at `2026-05-25T16:34:54Z`.
- E7-S6a / issue #150 was open and routed before E7-S6 / issue #112.
- Local `main` was clean, checked out, and fast-forwarded from `9f8a064` to
  `b7c5151` before branching.

## Implementation Notes

- Defaults remain mock-safe: roaster driver `mock`, first-crack mode
  `disabled`, and no model resolution/download unless `first_crack.mode:
  audio` is configured.
- `audio.overlap` and `audio.hop_seconds` control the detector hop. If
  `hop_seconds` is unset, hop is derived from `window_seconds * (1 -
  overlap)`.
- The detector adapter keeps recent positive windows inside
  `first_crack.confirmation_window_seconds` and records the earliest positive
  window only after `first_crack.min_positive_windows` is reached.
- The committed replay fixture is exactly `20.0` seconds long, so the
  validation profile uses `window_seconds: 10.0`, `overlap: 0.7`,
  `confidence_threshold: 0.6`, `min_positive_windows: 3`, and
  `confirmation_window_seconds: 20.0`. That produces three full overlapping
  windows before confirmation; using five positives is not possible on this
  fixture without padding or extending the source audio.

## WAV Validation

- Fixture: `tests/fixtures/audio/roastpilot-fc-replay-001.wav`
- Labels: `tests/fixtures/audio/roastpilot-fc-replay-001.labels.json`
- Manifest: `tests/fixtures/audio/roastpilot-fc-replay-001.manifest.json`
- Model repo: `syamaner/coffee-first-crack-detection`
- Revision: `b349a919c34b6130472da97c01817be404e4f629`
- Precision: `int8`
- Artifacts:
  - `onnx/int8/model_quantized.onnx`
  - `onnx/int8/preprocessor_config.json`
- Label interval after T0: `3.82710390663442-20.0` seconds
- Detected after T0: `10.017558290999885` seconds
- Previous non-overlapping result: about `20.017` seconds after T0
- Emitted windows: `3`
- Processed windows: `3`
- Dropped windows: `0`
- Confidence: `0.7762153826546956`
- Confidence threshold: `0.6`
- Min positive windows: `3`
- Confirmation window: `20.0` seconds
- Exported paths:
  - JSONL: `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-y04kfl8t/logs/roasts/2936bdf8a6044ac78bfeeb09714753ca/roast.jsonl`
  - CSV: `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-y04kfl8t/logs/roasts/2936bdf8a6044ac78bfeeb09714753ca/roast.csv`
  - Summary: `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-y04kfl8t/logs/roasts/2936bdf8a6044ac78bfeeb09714753ca/summary.json`

## Validation

- `./.venv/bin/python -m pytest tests/test_config.py tests/test_audio.py tests/test_detector.py tests/test_first_crack_runtime.py tests/test_first_crack_integration.py tests/test_exports.py`: 119 passed
- `./.venv/bin/python scripts/validate_first_crack_wav_replay.py --timeout-seconds 180`: passed
- `./.venv/bin/python -m pytest`: 394 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.2`

Full gate results are recorded in the E7-S6a validation notes in
`docs/state/epics/coffee-roaster-mcp-v0.1.md`.

## Next Routing

After the E7-S6a PR for issue #150 merges, continue to E7-S6 / issue #112: run
the supervised manual Warp roast validation with released Hugging Face ONNX
audio inference, real microphone input, and Hottop hardware.
