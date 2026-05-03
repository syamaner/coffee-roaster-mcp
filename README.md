# RoastPilot

RoastPilot is a spec-driven MCP server for autonomous coffee roasting.

The package name is `coffee-roaster-mcp`. PyPI publishing is planned for the v0.1 release; this repository currently contains the local package scaffold and project plan.

RoastPilot will provide one local MCP runtime for roaster control, telemetry, first-crack detection integration, roast metrics, and log export.

The project is currently in bootstrap. See `docs/plans/coffee-roaster-mcp-v0.1-overall-plan.md` and `docs/state/registry.md` for the active plan and state.

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
