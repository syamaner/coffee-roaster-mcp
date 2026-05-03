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

The current repo state is still bootstrap. The package scaffold, config loading, local development commands, and pull-request CI are in place. The full MCP runtime and tool surface start in Epic 2.

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

The full stdio MCP server and roast-session tool flow have not landed yet. Until `E2-S1` and later Epic 2 stories are implemented, the practical local mock run is a bootstrap validation flow rather than a real MCP session.

### Mock-Safe Bootstrap Smoke

The full stdio MCP server lands in `E2-S1`. Until then, use this mock-safe bootstrap smoke to confirm the default local path stays hardware-free and model-free from a guaranteed-empty temporary directory:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

Expected output:

```text
mock disabled int8
```

This confirms the bootstrap defaults are still aligned with the mock vertical-slice plan.

## Hottop Configuration Placeholder

Hottop support is planned behind the `RoasterDriver` abstraction. It is not ready for hardware use yet.

When Hottop stories land, configuration will continue to live in `coffee-roaster-mcp.yaml`. The relevant roaster section already has the expected placeholder shape:

```yaml
roaster:
  driver: mock
  port: null
  baudrate: 115200
  temperature_unit: celsius
  command_interval_seconds: 0.3
```

For future Hottop usage, the expected change is to switch `driver` away from `mock` and set the serial port explicitly. Until the Hottop driver and validation stories are complete, keep local development on the mock driver.

Hardware safety matters here: Hottop command-loop behavior, packet handling, temperature units, drop behavior, cooling behavior, and emergency stop still require explicit implementation plus manual validation before hardware-ready use.

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
  input_device: null
  sample_rate: 16000

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
- `COFFEE_AUDIO_INPUT_DEVICE`
- `COFFEE_ROAST_LOG_DIR`
- `HF_HOME`

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
