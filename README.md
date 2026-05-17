# RoastPilot

RoastPilot is a spec-driven MCP server for autonomous coffee roasting.

The package name is `coffee-roaster-mcp`. PyPI publishing is planned for the v0.1 release; this repository currently contains the local package scaffold and project plan.

RoastPilot will provide one local MCP runtime for roaster control, telemetry, first-crack detection integration, roast metrics, and log export.

The project is currently in bootstrap. See `docs/plans/coffee-roaster-mcp-v0.1-overall-plan.md` and `docs/state/registry.md` for the active plan and state.

## What RoastPilot Is

RoastPilot is the human-facing product name. `coffee-roaster-mcp` is the infrastructure and packaging name used for the repository, Python package, and future distribution.

The v0.1 direction is one local stdio MCP server that will own:

- roaster control
- roast session timing and events
- first-crack detection integration
- derived roast metrics
- roast log export

The current repo state is still bootstrap. The package scaffold, config loading, local development commands, pull-request CI, stdio MCP entrypoint, mock roast-session tool surface, and one-process mock vertical slice are in place.

## Install

For local development today:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

The future user-facing install target is the `coffee-roaster-mcp` package. PyPI publication is planned later in v0.1 after the runtime and distribution stories land.

## Local Development

### Setup

Use the commands in the [Install](#install) section, then continue with the checks below.

### Test

```bash
python -m pytest
```

### Coverage

```bash
python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=html:htmlcov --cov-report=json:coverage.json
python .github/scripts/write_coverage_summary.py coverage.json
```

Pull-request CI publishes a Markdown coverage summary in the `Checks` job summary and uploads `html-coverage-report` as a workflow artifact for file-by-file drill-down.

### Lint

```bash
python -m ruff check .
```

### Format Check

```bash
python -m ruff format --check .
```

### Typecheck

```bash
python -m pyright
```

### CLI Smoke

```bash
coffee-roaster-mcp --help
coffee-roaster-mcp --version
```

## Local Mock Run

The default local path is intentionally mock-safe:

- default roaster driver: `mock`
- default first-crack mode: `disabled`
- no roaster hardware required
- no microphone required
- no model download required

RoastPilot now provides a real local stdio MCP server entrypoint plus the first mock-safe roast-session tool surface. Later Epic 2 stories still need to refine phase transitions, emergency-stop semantics, and the real log writers.

### Start The Local MCP Server

```bash
coffee-roaster-mcp serve
```

The current MCP tool surface includes:

- `get_server_info`
- `get_runtime_config`
- `start_roast_session`
- `get_roast_state`
- `set_heat`
- `set_fan`
- `mark_beans_added`
- `mark_first_crack`
- `drop_beans`
- `start_cooling`
- `stop_cooling`
- `export_roast_log`
- `emergency_stop`

`export_roast_log` writes snapshot `roast.jsonl`, `roast.csv`, and `summary.json` files for the current in-process session. Append-only telemetry writers and final log schemas land in Epic 5.

### Mock-Safe Bootstrap Smoke

Use this mock-safe bootstrap smoke to confirm the default local path stays hardware-free and model-free from a guaranteed-empty temporary directory:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

Expected output:

```text
mock disabled int8
```

This confirms the bootstrap defaults are still aligned with the mock vertical-slice plan.

## Hottop Configuration And Validation

Hottop support lives behind the `RoasterDriver` abstraction. The current driver has lifecycle, command-loop, packet, control-state, and temperature-unit support, but it still requires guarded manual validation before any hardware-ready release label.

Configuration lives in `coffee-roaster-mcp.yaml`. Keep local development on the mock driver unless you are intentionally validating connected Hottop hardware:

```yaml
roaster:
  driver: mock
  port: null
  baudrate: 115200
  temperature_unit: celsius
  command_interval_seconds: 0.3
```

For guarded hardware validation, switch `driver` to `hottop_kn8828b_2k_plus`, set the serial port explicitly, and run the validation harness with an evidence output path:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-non-destructive.json \
  --i-understand-this-controls-hardware
```

The irreversible and safety-action checks are opt-in:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-full.json \
  --i-understand-this-controls-hardware \
  --include-drop \
  --include-emergency-stop
```

Hardware safety matters here: command-loop cadence, packet handling, temperature units, drop behavior, cooling behavior, emergency stop, and cleanup must be validated on a supervised roaster before the Hottop path is treated as release-ready. The current MCP roast-session tools preserve their existing one-session store semantics; driver-level validation does not imply MCP heat, fan, drop, or cooling tools are live-hardware control surfaces yet.

## Configuration

RoastPilot loads configuration from `coffee-roaster-mcp.yaml` in the current directory by default. If the file is absent, mock-safe defaults are used so local development does not require roaster hardware, audio hardware, or model downloads.

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
  allow_manual_override: true

audio:
  source: microphone
  input_device: null
  sample_rate: 16000
  wav_path: null

logging:
  log_dir: ./logs
  sample_interval_seconds: 1.0
  export_formats:
    - jsonl
    - csv
    - summary

session:
  auto_t0_detection_enabled: false
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
- `COFFEE_AUDIO_SOURCE`
- `COFFEE_AUDIO_INPUT_DEVICE`
- `COFFEE_AUDIO_SAMPLE_RATE`
- `COFFEE_AUDIO_WAV_PATH`
- `COFFEE_ROAST_LOG_DIR`
- `HF_HOME`

`audio.source` can be `microphone` or `wav`. Microphone capture uses a
PortAudio-backed `sounddevice` stream and keeps the configured device identifier
behind the audio-input boundary for macOS, Linux, and Raspberry Pi hosts. WAV
replay uses PCM `.wav` files, converts channels to the same mono float sample
contract as microphone capture, and requires the file sample rate to match
`audio.sample_rate`.

`HF_HOME` is consumed by Hugging Face tooling directly rather than copied into the RoastPilot config object.

## Hugging Face Model Boundary

This repository does not train, export, sync, or publish first-crack models.

The `coffee-first-crack-detection` repository remains the source of truth for:

- model training
- ONNX export
- Hugging Face artifact publishing
- model cards
- dataset cards

RoastPilot only consumes released artifacts from `syamaner/coffee-first-crack-detection`. The runtime boundary for this repo is inference-time configuration and model selection, not model lifecycle management.

Current first-crack defaults are kept safe for local development:

- `mode: disabled`
- `precision: int8`
- `repo_id: syamaner/coffee-first-crack-detection`

That default keeps local setup free from Hugging Face network access until first-crack runtime stories are implemented.

## Log Export

Roast log export is a planned v0.1 capability, not a completed runtime feature yet.

The intended direction is:

- append-only JSONL during roast
- CSV export for analysis
- `summary.json` for session-level metadata
- output under `logs/roasts/{session_id}/`

Those exports will be produced by the future MCP runtime once roast sessions, telemetry, and export flows are implemented in later epics. They are not available from the current bootstrap CLI yet.
