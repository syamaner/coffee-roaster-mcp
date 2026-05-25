# RoastPilot Install And Hardware Setup

This guide covers the operator setup path for `coffee-roaster-mcp` before live
PyPI and MCP Registry publication. It is intentionally documentation-only:
do not use it as evidence that a live PyPI publish, MCP Registry publish,
hardware validation, model training, model export, model sync, or real
microphone validation has been executed.

## Mock Install

The default RoastPilot path is mock-safe:

- `roaster.driver: mock`
- `first_crack.mode: disabled`
- no Hottop hardware
- no microphone
- no Hugging Face model download
- logs written under `./logs` unless configured otherwise

For local development from a clone:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
coffee-roaster-mcp --help
coffee-roaster-mcp --version
```

For the installed package after the live PyPI release:

```bash
python -m pip install coffee-roaster-mcp
coffee-roaster-mcp --help
coffee-roaster-mcp --version
coffee-roaster-mcp serve
```

The MCP Registry metadata advertises a `uvx` runtime hint. After PyPI publish,
an MCP client can launch the package through the registry or equivalent local
stdio command that runs `coffee-roaster-mcp serve`. Before PyPI publish, use the
editable install path above.

Confirm the defaults from an empty directory:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision, c.logging.log_dir); tmp.cleanup()"
```

Expected output:

```text
mock disabled int8 logs
```

## Configuration File

RoastPilot loads `coffee-roaster-mcp.yaml` from the current working directory by
default. Use `COFFEE_ROASTER_MCP_CONFIG` to point at another file.

Start from this mock-safe configuration:

```yaml
transport:
  type: stdio

roaster:
  driver: mock
  port: null
  baudrate: 115200
  temperature_unit: celsius
  command_interval_seconds: 0.3

first_crack:
  mode: disabled
  repo_id: syamaner/coffee-first-crack-detection
  revision: null
  precision: int8
  local_model_dir: null
  onnx_threads: 2
  confidence_threshold: 0.9
  min_positive_windows: 1
  confirmation_window_seconds: 20.0
  allow_manual_override: true

audio:
  source: microphone
  input_device: null
  sample_rate: 16000
  wav_path: null
  replay_mode: realtime
  window_seconds: 1.0
  overlap: 0.0
  hop_seconds: null

logging:
  log_dir: ./logs
  sample_interval_seconds: 5.0
  export_formats:
    - jsonl
    - csv
    - summary

session:
  auto_t0_detection_enabled: false
  auto_t0_drop_threshold_c: 25.0
  ror_window_seconds: 60
  ror_min_sample_seconds: 10
```

Supported environment overrides:

- `COFFEE_ROASTER_MCP_CONFIG`
- `COFFEE_ROASTER_DRIVER`
- `COFFEE_ROASTER_PORT`
- `COFFEE_ROASTER_TEMP_UNIT`
- `COFFEE_FIRST_CRACK_MODE`
- `COFFEE_FIRST_CRACK_REPO_ID`
- `COFFEE_FIRST_CRACK_REVISION`
- `COFFEE_FIRST_CRACK_PRECISION`
- `COFFEE_FIRST_CRACK_LOCAL_MODEL_DIR`
- `COFFEE_FIRST_CRACK_ONNX_THREADS`
- `COFFEE_FIRST_CRACK_CONFIDENCE_THRESHOLD`
- `COFFEE_FIRST_CRACK_MIN_POSITIVE_WINDOWS`
- `COFFEE_FIRST_CRACK_CONFIRMATION_WINDOW_SECONDS`
- `COFFEE_AUDIO_SOURCE`
- `COFFEE_AUDIO_INPUT_DEVICE`
- `COFFEE_AUDIO_SAMPLE_RATE`
- `COFFEE_AUDIO_WAV_PATH`
- `COFFEE_AUDIO_REPLAY_MODE`
- `COFFEE_AUDIO_WINDOW_SECONDS`
- `COFFEE_AUDIO_OVERLAP`
- `COFFEE_AUDIO_HOP_SECONDS`
- `COFFEE_ROAST_LOG_DIR`
- `COFFEE_AUTO_T0_DROP_THRESHOLD_C`
- `HF_HOME`

`HF_HOME` is consumed by Hugging Face tooling directly. It is not copied into
the RoastPilot config object.

## Hottop Configuration

Keep normal setup on the mock driver until you intentionally operate connected
Hottop hardware. To configure the supported Hottop driver, set:

```yaml
roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/cu.usbserial-XXXX
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3
```

Port discovery is platform-specific:

- macOS USB serial adapters usually appear under `/dev/cu.*`.
- Linux and Raspberry Pi USB serial adapters commonly appear under
  `/dev/ttyUSB*` or `/dev/ttyACM*`.
- Confirm the device appears after plugging in the adapter and that no other
  process has the port open.

Hottop operation is hardware-facing. Only use this config with a supervised
roaster, a known stop plan, and an operator who understands heat, fan, drop,
cooling, emergency stop, and physical power-off expectations.

The guarded driver validation harness is:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-non-destructive.json \
  --i-understand-this-controls-hardware
```

The irreversible drop and emergency-stop checks are opt-in and remain outside
this setup story:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-full.json \
  --i-understand-this-controls-hardware \
  --include-drop \
  --include-emergency-stop
```

Do not commit generated validation JSON, raw serial captures, roast logs, raw
audio recordings, or local environment files unless a later validation story
explicitly calls for a sanitized artifact. The narrow current audio exception is
the derived E7-S5a labelled WAV replay fixture under `tests/fixtures/audio/`;
do not expand that exception to raw recordings or broad datasets.

## Hugging Face Model Configuration

This repository consumes released first-crack artifacts only. Model training,
ONNX export, Hugging Face sync, model cards, and dataset cards stay in
`coffee-first-crack-detection`.

The safe default is disabled:

```yaml
first_crack:
  mode: disabled
  repo_id: syamaner/coffee-first-crack-detection
  revision: null
  precision: int8
  local_model_dir: null
  onnx_threads: 2
  confidence_threshold: 0.9
  min_positive_windows: 1
  confirmation_window_seconds: 20.0
  allow_manual_override: true
```

To enable the released Hugging Face ONNX detector deliberately:

```yaml
first_crack:
  mode: audio
  repo_id: syamaner/coffee-first-crack-detection
  revision: <pinned-hub-revision>
  precision: int8
  local_model_dir: null
  onnx_threads: 2
  allow_manual_override: true

audio:
  source: microphone
  input_device: null
  sample_rate: 16000
  wav_path: null
  replay_mode: realtime
  window_seconds: 1.0
  overlap: 0.0
  hop_seconds: null
```

`precision: int8` selects `onnx/int8/model_quantized.onnx`.
`precision: fp32` selects `onnx/fp32/model.onnx`. Both precisions also require
the matching `onnx/{precision}/preprocessor_config.json` artifact.

For recorded replay instead of a microphone:

```yaml
audio:
  source: wav
  input_device: null
  sample_rate: 16000
  wav_path: /path/to/replay.wav
  replay_mode: detector_paced
  window_seconds: 10.0
  overlap: 0.7
```

The WAV sample rate must match `audio.sample_rate`. `replay_mode: realtime`
keeps the normal background capture behavior. `replay_mode: detector_paced` is
for local labelled-fixture validation: it reads the next complete WAV window
only when the detector drains it, so replay can run faster than wall-clock audio
without normal detector queue drops. Use `audio.window_seconds` to match the
released detector's expected window duration for the validation source, and use
`audio.overlap` or `audio.hop_seconds` when validating sliding-window detector
behavior. Real microphone validation is gated manual work and is not required
for normal CI or
this story.

The E7-S5a labelled replay check is opt-in/local because it uses the released
ONNX detector artifacts. From a development checkout with dependencies
installed, run:

```bash
./.venv/bin/python scripts/validate_first_crack_wav_replay.py
```

The script starts the stdio MCP server with the mock roaster, pinned released
INT8 Hugging Face artifacts, `audio.source: wav`, and
`audio.replay_mode: detector_paced`; then it uses public MCP tools to start a
session, mark T0, poll until first crack is detected, validate the detected
time against the fixture labels, and export `roast.jsonl`, `roast.csv`, and
`summary.json`.

## Offline Model Path

Set `first_crack.local_model_dir` to consume an already downloaded model tree
without Hugging Face network access:

```yaml
first_crack:
  mode: audio
  repo_id: syamaner/coffee-first-crack-detection
  revision: <recorded-local-artifact-revision>
  precision: int8
  local_model_dir: /opt/roastpilot/models/coffee-first-crack-detection
  onnx_threads: 2
  allow_manual_override: true
```

The local directory must preserve the released repository-relative paths:

```text
/opt/roastpilot/models/coffee-first-crack-detection/
  onnx/
    int8/
      model_quantized.onnx
      preprocessor_config.json
    fp32/
      model.onnx
      preprocessor_config.json
```

Configure only the precision you intend to run. If the selected ONNX model or
preprocessor config is missing, audio-mode startup reports first-crack status as
`unavailable` rather than falling back to a different artifact.

## Log Output Paths

`logging.log_dir` controls the root log directory and defaults to `./logs`.
Use `COFFEE_ROAST_LOG_DIR` to override it without changing YAML.

Runtime and snapshot logs are written under:

```text
{logging.log_dir}/roasts/{session_id}/
```

Current files are:

- `roast.jsonl`: append-only event rows plus sampled telemetry rows during the
  roast
- `roast.csv`: exported telemetry and event rows
- `summary.json`: exported session summary, metrics, configured roaster driver,
  and first-crack model metadata

Telemetry rows are sampled at `logging.sample_interval_seconds`, defaulting to
5 seconds. Event rows are written immediately when session events are recorded.

Do not commit generated files under `logs/`. For release or hardware evidence,
record sanitized paths and checksums in the relevant validation issue or session
summary instead of committing local roast output.
