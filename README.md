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

RoastPilot now provides a local stdio MCP server entrypoint with a mock-safe
roast-session tool surface. The default configuration lets an MCP client start
a roast, adjust controls, read current device and session state, record explicit
override events, drop beans into cooling, and export snapshot logs without
roaster hardware, microphone input, model files, or network access.

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

### Operational MCP Flow

The mock-safe Claude/operator flow is:

1. Call `start_roast_session` to create the one active roast session and connect
   the configured driver.
2. Call `set_heat` and `set_fan` as operational decisions require. These tools
   go through the configured `RoasterDriver` boundary; the default mock driver
   stays local and deterministic.
3. Call `get_roast_state` to read both the authoritative session state and the
   current configured-device state. The response includes driver id, connected
   status, bean/environment temperatures when available, heat/fan levels,
   cooling state, safe raw diagnostics, T0 status, first-crack status, and
   lifecycle timestamps for beans added, first crack, bean drop, cooling
   started, and cooling stopped.
4. Use `drop_beans` as the normal drop command. For the mock path and the
   Hottop compound drop path, this records `beans_dropped`, records
   `cooling_started` when the driver reports cooling active, turns heat off,
   sets fan to `100%`, and enters the cooling phase.
5. Use `stop_cooling` when cooling is complete. `start_cooling` remains
   available as an explicit advanced/manual recovery tool, not as the normal
   roast flow after `drop_beans`.

`mark_beans_added` and `mark_first_crack` are explicit override tools. They are
kept available for operator recovery and controlled manual runs. The primary
automatic runtime paths are internal: automatic T0 detection can record
`beans_added` when `session.auto_t0_detection_enabled` is enabled, and
audio-mode first-crack confirmation is owned by the session-owned first-crack
runtime when `first_crack.mode: audio` is deliberately configured.

Automatic T0 is disabled by default. When enabled, `get_roast_state` reads the
configured driver, tracks the max preheat bean temperature before T0, and
records `beans_added` when the current bean temperature drops from that max by
`session.auto_t0_drop_threshold_c`. `get_roast_state.t0_status` exposes the
configured threshold, tracked charge temperature, current drop, and detected
bean temperature when automatic T0 records the event.

`get_roast_state.first_crack_status.status` is one of:

- `disabled`: first-crack detection is disabled and no first-crack event exists.
- `manual`: manual first-crack mode is configured and the override tool is
  available.
- `pending`: audio detection is configured and waiting for a confirmed event.
- `detected`: the authoritative session timeline has a first-crack event.
- `faulted`: the detector runtime or session has faulted.
- `unavailable`: configuration, artifacts, audio capture, or manual-override
  settings make first-crack detection unavailable.

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

Hardware safety matters here: command-loop cadence, packet handling,
temperature units, drop behavior, cooling behavior, emergency stop, and cleanup
must be validated on a supervised roaster before the Hottop path is treated as
release-ready. The current MCP roast-session tools call the configured driver
boundary, so keep normal development on the mock driver unless a guarded Hottop
validation run is explicitly intended.

Optional live Hottop MCP validation is gated manual work. Run it only with a
supervised roaster, an explicit `hottop_kn8828b_2k_plus` config, a known serial
port, and a clear stop plan. Expected evidence for a pass is: the MCP client can
start one session, set heat and fan, read connected device state with plausible
temperatures, call `drop_beans` to trigger drop plus cooling, read
`beans_dropped` and `cooling_started` timestamps from `get_roast_state`, stop
cooling when the roaster reports cooling off, and preserve any failure as a
fault event. Any serial, telemetry, command-loop, or safety uncertainty should
be treated as a failed validation and should not be required by normal CI.

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
- `COFFEE_AUDIO_SOURCE`
- `COFFEE_AUDIO_INPUT_DEVICE`
- `COFFEE_AUDIO_SAMPLE_RATE`
- `COFFEE_AUDIO_WAV_PATH`
- `COFFEE_ROAST_LOG_DIR`
- `COFFEE_AUTO_T0_DROP_THRESHOLD_C`
- `HF_HOME`

`audio.source` can be `microphone` or `wav`. Microphone capture uses a
PortAudio-backed `sounddevice` stream and keeps the configured device identifier
behind the audio-input boundary for macOS, Linux, and Raspberry Pi hosts. WAV
replay uses PCM `.wav` files, converts channels to the same mono float sample
contract as microphone capture, and requires the file sample rate to match
`audio.sample_rate`.

For microphone capture, `audio.input_device: null` uses the system default input
device. To pin a specific microphone, set `audio.input_device` to a
PortAudio-resolvable device name or platform device identifier. On Linux and
Raspberry Pi, use `arecord -l` and `arecord -L` to inspect ALSA devices; values
such as `plughw:1,0` are often more forgiving than raw `hw:1,0` because ALSA can
perform format conversion. On macOS, use the system sound settings or a
`sounddevice` device listing during manual validation. Real microphone checks
are optional and should be run only when `first_crack.mode: audio` and
`audio.source: microphone` are deliberately configured.

Optional real microphone validation is gated manual work. Before running it,
configure released Hugging Face ONNX artifacts or a validated
`first_crack.local_model_dir`, select the intended microphone, and confirm the
MCP process can start without artifact or audio-capture errors. Expected
evidence for a pass is: `get_roast_state.first_crack_status` moves from
`pending` to `detected` during a supervised roast or controlled replay, the
recorded `first_crack_detected` event includes detector metadata, and normal
roast controls continue to work. Missing artifacts, unavailable audio devices,
and detector failures should surface as `unavailable` or `faulted` status and
remain outside normal CI.

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

That default keeps local setup free from Hugging Face network access until
audio mode is deliberately configured.
When `first_crack.mode: audio` is deliberately configured, RoastPilot consumes
the released ONNX artifacts with ONNX Runtime and the released AST preprocessor
config with `transformers.ASTFeatureExtractor`; model training, export, and Hub
publishing remain outside this repository.

In audio mode, starting a roast session prepares the configured audio capture
pipeline and released-artifact detector runtime. Detector windows are processed
only after T0 is recorded and the active session is in `roasting`. Confirmed
detector output records `first_crack_detected` once through the authoritative
session timeline. The runtime stops when first crack is recorded automatically
or through the explicit manual override, and also stops on drop, cooling
completion, emergency stop, and process shutdown. Missing artifacts,
unavailable audio capture, and detector errors are surfaced through
`get_roast_state.first_crack_status` as `unavailable` or `faulted` rather than
crashing normal roast controls. Disabled and manual first-crack modes do not
start audio capture or detector runtime.

## Log Export

RoastPilot currently supports snapshot export through `export_roast_log` for
the active in-process session.

Current export files:

- `roast.jsonl` with append-only event and sampled telemetry rows during the
  roast
- `roast.csv` with telemetry and event rows using the planned CSV columns for
  timestamps, elapsed seconds, phase, temperatures, controls, event flags,
  development percent, RoR/delta metrics, and first-crack model metadata
- `summary.json` with current session-level metadata and timestamp-derived
  metrics
- output under `logs/roasts/{session_id}/`

Final `summary.json` schema work and broad release validation land in later
stories.
