# E7-S5a Labelled WAV First-Crack Replay

Date: 2026-05-25

Branch: `feature/141-test-mcp-first-crack-detection-labelled-wav-replay`

Issue: #141

## Scope

Test MCP first-crack detection with labelled WAV replay only.

Included:

- mock roaster
- released Hugging Face INT8 artifacts from `syamaner/coffee-first-crack-detection`
- pinned revision `b349a919c34b6130472da97c01817be404e4f629`
- one small derived labelled WAV fixture committed under `tests/fixtures/audio/`
- detector-paced WAV replay through public MCP tools

Excluded:

- live Hottop validation
- real microphone validation
- autonomous roasting
- full end-to-end agent roast validation
- model training, ONNX export, Hugging Face sync, model cards, or dataset cards
- live PyPI or MCP Registry publishing
- hardware-ready release labeling

## Prerequisite Verification

- PR #146 is merged.
- Tag `v0.1.2` points at `3c19d6a677cf40c769dc8394d2e2ac53308446b6`.
- PyPI `coffee-roaster-mcp==0.1.2` is published with wheel SHA-256
  `c548967fc239cd93786cc23287c4c55cd67dac398a61b82021bc0022bd4926db` and
  sdist SHA-256
  `25d554bef5f7477256fac63a1c277c224de116d7251f9cd34ff11fdb42a9ef77`.
- PR #147 is merged.
- E7-S4 issue #59 is closed.
- E7-S4 PR #143 is merged.
- Local `main` was clean, checked out, and `git pull --ff-only origin main`
  reported already up to date before branching.

## Fixture

Committed fixture files:

- `tests/fixtures/audio/roastpilot-fc-replay-001.wav`
- `tests/fixtures/audio/roastpilot-fc-replay-001.labels.json`
- `tests/fixtures/audio/roastpilot-fc-replay-001.manifest.json`

Source:

- checkout: `/Users/sertanyamaner/git/coffee-first-crack-detection`
- raw audio: `data/raw/mic2-panama-hortigal-estate-roast1.wav`
- labels: `data/labels/mic2-panama-hortigal-estate-roast1.json`
- evaluation split evidence:
  `data/splits/test/first_crack/mic2-panama-hortigal-estate-roast1_w0670.0.wav`
  and
  `data/splits/test/first_crack/mic2-panama-hortigal-estate-roast1_w0680.0.wav`

Generation:

- trim: `662.0-682.0` seconds from the raw recording
- output: 20.0 seconds, 16 kHz, mono, PCM16 WAV
- original label interval: `665.8271039066344-746.7350791840846` seconds
- adjusted label interval: `3.82710390663442-20.0` seconds
- WAV SHA-256:
  `923c61a456b04797c1302ed78984ab7d9b148d7dc21d3825b225b8a6043aa9fc`

The fixture is documented as the narrow E7-S5a exception to the normal
no-audio-in-git rule. Raw recordings, broad training/evaluation audio, model
artifacts, roast logs, serial captures, and local environment files remain
excluded from git.

## Implementation Notes

- Added `audio.replay_mode`, defaulting to `realtime`.
- Added `audio.window_seconds`, defaulting to `1.0`.
- Added detector-paced WAV replay for `audio.source: wav` and
  `audio.replay_mode: detector_paced`. It reads complete windows synchronously
  when the detector drains them, so replay can run faster than wall-clock audio
  without normal queue drops.
- Detector-paced replay preserves source-audio timeline semantics by allowing
  detector timestamps to advance ahead of wall-clock elapsed time for this
  explicit local replay mode.
- Exposed first-crack runtime metrics in
  `get_roast_state.first_crack_status`: audio running state, queued, emitted,
  dropped, and processed window counts.
- Patched the released AST feature extractor setup for ONNX-only runtime so the
  local `transformers` feature-extraction path works without PyTorch by using
  the existing NumPy spectrogram path directly.
- Added `scripts/validate_first_crack_wav_replay.py` as an opt-in local manual
  validation path. It starts the stdio MCP server, uses public MCP tools,
  validates detection against labels, and exports logs.

## Released-Model MCP Replay Result

Command:

```bash
./.venv/bin/python scripts/validate_first_crack_wav_replay.py
```

Result:

- model revision: `b349a919c34b6130472da97c01817be404e4f629`
- fixture SHA-256:
  `923c61a456b04797c1302ed78984ab7d9b148d7dc21d3825b225b8a6043aa9fc`
- label interval after T0: `3.82710390663442-20.0` seconds
- detected first crack after T0: `20.018370707999928` seconds
- wall-clock elapsed: `2.4680781659990316` seconds
- effective replay speed: `8.110914388279461`
- emitted windows: `2`
- processed windows: `2`
- dropped windows: `0`

Exported artifacts reviewed:

- `roast.jsonl`:
  `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-1bl4cb5u/logs/roasts/f23bdb0fb8054cf5851a7d51862e3945/roast.jsonl`
- `roast.csv`:
  `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-1bl4cb5u/logs/roasts/f23bdb0fb8054cf5851a7d51862e3945/roast.csv`
- `summary.json`:
  `/private/var/folders/1b/g1kk3f852c9_5srvd67j_r9w0000gn/T/roastpilot-fc-replay-1bl4cb5u/logs/roasts/f23bdb0fb8054cf5851a7d51862e3945/summary.json`

Review notes:

- `roast.jsonl` contains `beans_added`, `first_crack_detected`, and telemetry
  rows.
- `first_crack_detected` payload includes confidence
  `0.9984510180497517`, window sequence `1`, INT8 precision, the pinned
  revision, and released artifact filenames.
- `roast.csv` includes the `first_crack_detected` event row at elapsed
  `20.02` seconds with model repo, revision, and precision fields populated.
- `summary.json` records phase `development`, roaster driver `mock`, the
  first-crack UTC timestamp, and first-crack model metadata.

## Validation

- `./.venv/bin/python -m pytest tests/test_config.py tests/test_audio.py tests/test_audio_fixtures.py tests/test_first_crack_runtime.py`: 49 passed
- `./.venv/bin/python scripts/validate_first_crack_wav_replay.py`: passed
- `./.venv/bin/python -m pytest`: 371 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: 33 files already formatted
- `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.2`

## Review Follow-Up

- Addressed CodeRabbit review feedback by YAML-quoting replay script paths,
  stripping inherited `COFFEE_*` environment overrides from the replay script
  subprocess, using the AST extractor speech-backend availability helper before
  installing the NumPy-only patch, and scoping exposed first-crack runtime
  metrics to the serialized session id.
- Addressed Codex review feedback by normalizing stopped capture snapshots so a
  stopped runtime cannot continue reporting `audio_running: true`, and by
  blocking later non-fault phase events until wall-clock elapsed time catches up
  to a detector-paced future first-crack timestamp.
- Added regression coverage for stopped audio snapshots, cross-session runtime
  metric leakage, and future first-crack timeline ordering.
- Re-ran released-model WAV replay after the review fixes:
  detected `20.018164542000154` seconds after T0 against the
  `3.82710390663442-20.0` second label interval, emitted `2` windows, processed
  `2` windows, dropped `0` windows, and exported `roast.jsonl`, `roast.csv`,
  and `summary.json` under the local temporary replay log directory.

## Next Routing

After the E7-S5a PR for issue #141 merges, continue to E7-S5 / issue #60:
produce the v0.1 release checklist. Keep live Hottop validation, real
microphone validation, full end-to-end agent roast validation, model lifecycle
work, live publishing, and hardware-ready release labeling out of scope unless
the next issue explicitly requires them.

## Usage Snapshot

- Review follow-up start: current Codex goal token tracking is unavailable in
  this session (`remainingTokens: null`, `completionBudgetReport: null`).
