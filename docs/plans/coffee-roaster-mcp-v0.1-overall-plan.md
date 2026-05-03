# Coffee Roaster MCP v0.1 Overall Plan

## Summary

Build `RoastPilot`, a standalone Python MCP server published as `coffee-roaster-mcp`, that replaces the current two-MCP plus n8n POC with one local process that owns roaster control, telemetry, microphone first-crack detection, roast timing, derived metrics, event logging, and export.

The model repo remains the source of truth for training, ONNX export, Hugging Face publishing, model cards, and dataset cards. This MCP only consumes released Hugging Face artifacts from `syamaner/coffee-first-crack-detection`.

## Repository Identity

- GitHub repository name: `coffee-roaster-mcp`.
- Python package name: `coffee-roaster-mcp`.
- Python import package: `coffee_roaster_mcp`.
- Console entrypoint: `coffee-roaster-mcp`.
- Product/display name: `RoastPilot`.
- MCP Registry name: `io.github.syamaner/coffee-roaster-mcp`.
- Default config file: `coffee-roaster-mcp.yaml`.
- Rationale: use `coffee-roaster-mcp` for boring infrastructure names where searchability and install clarity matter. Use `RoastPilot` for README, docs, demos, and blog framing where a product name reads better.

## Core Architecture

- Use one local stdio MCP server for v0.1.
- Keep agent orchestration and n8n out of scope.
- Add one authoritative `RoastSession` runtime with session id, monotonic clock, roaster driver, first-crack detector, telemetry buffer, event timeline, and append-only log writer.
- `beans_added_at` is T0.
- `first_crack_at` is recorded once from detector or manual override.
- `beans_dropped_at` ends roast timing.
- All timing, RoR, deltas, development percent, and logs are computed inside the MCP.

## MCP Tools

- `start_roast_session`: creates session, connects roaster, starts telemetry, optionally starts detector.
- `get_roast_state`: returns phase, temps, controls, events, elapsed time, development percent, RoR, and deltas.
- `set_heat`: sets heat percentage.
- `set_fan`: sets fan percentage.
- `mark_beans_added`: records T0 and emits `beans_added`.
- `mark_first_crack`: records first crack manually and emits `first_crack_detected`.
- `drop_beans`: records drop, executes driver drop action, emits `beans_dropped`.
- `start_cooling`: starts cooling and emits `cooling_started`.
- `stop_cooling`: stops cooling and emits `cooling_stopped`.
- `export_roast_log`: exports active session by default, or a provided `session_id`, returning JSONL, CSV, and summary paths.
- `emergency_stop`: turns heat off, attempts safe cooling behavior, records emergency/fault event.

## Session Metrics

- `roast_elapsed_seconds`: from `beans_added_at` to now, or to `beans_dropped_at` after drop.
- `development_time_seconds`: from `first_crack_at` to now, or to drop.
- `development_time_percent`: `development_time_seconds / roast_elapsed_seconds * 100`.
- `bean_delta_60s_c`: latest bean temp minus oldest bean temp in rolling 60s window.
- `env_delta_60s_c`: latest env temp minus oldest env temp in rolling 60s window.
- `bean_ror_c_per_min`: bean temp slope over rolling window, normalized to C/min.
- `env_ror_c_per_min`: env temp slope over rolling window, normalized to C/min.
- RoR returns null until at least 10 seconds of samples exist.
- Auto-T0 detection is disabled by default. `mark_beans_added` is authoritative.

## Config Schema

Default config file path: `./coffee-roaster-mcp.yaml`.

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
  mode: disabled  # disabled | audio | manual
  repo_id: syamaner/coffee-first-crack-detection
  revision: null  # null means latest; release builds should pin this
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

Environment overrides:

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

## Model Runtime

- Use ONNX Runtime.
- Default model: INT8 at `onnx/int8/model_quantized.onnx`.
- Optional model: FP32 at `onnx/fp32/model.onnx`.
- Resolve model artifacts through `huggingface_hub`.
- Allow `local_model_dir` for offline operation.
- Validate required ONNX model and feature extractor files before detection starts.
- Record first-crack event metadata: model repo, revision, precision, timestamp, and confidence if available.
- Do not implement model sync, training, export, or card publishing in this repo.

## Roaster Abstraction

- Define `RoasterDriver` with `connect`, `disconnect`, `read_state`, `set_heat`, `set_fan`, `drop_beans`, `start_cooling`, `stop_cooling`, `emergency_stop`, and `capabilities`.
- Ship `mock` driver for tests, demos, registry install smoke tests, and vertical-slice validation.
- Ship `hottop_kn8828b_2k_plus` driver for the current hardware.
- Normalize driver state to bean temp C, env temp C, heat %, fan %, cooling on/off, connected, and raw vendor data.
- Capabilities describe control ranges, step sizes, supported actions, sensor units, and whether continuous command streaming is required.

## Hottop Driver Requirements

- Use the existing direct serial protocol approach unless verification proves `pyhottop` is safer.
- Preserve continuous command loop behavior around 0.3s.
- Support 36-byte command/status packets and checksum validation.
- Initialize in safe state: heat off, fan controlled, cooling off unless commanded.
- Heat above zero implies drum-on where required by the Hottop protocol.
- Drop is compound: heat off, drum off, drop/solenoid active, cooling on, main fan high.
- Cooling stop clears cooling motor, solenoid/drop path, and main fan as appropriate.
- Temperature mode is explicit: `celsius`, `fahrenheit`, or `auto`.
- Ignore startup zero/unready readings until plausible telemetry arrives.
- Add a Hottop verification spike because command-loop behavior and temp units are the main hardware risks.

## Roast Logs

- Write append-only JSONL during roast.
- Export CSV and `summary.json`.
- Store files under `logs/roasts/{session_id}/`.
- Log at 1 Hz plus immediate event rows.
- Required CSV columns:
  - `timestamp_utc`
  - `elapsed_seconds`
  - `phase`
  - `bean_temp_c`
  - `env_temp_c`
  - `heat_level_percent`
  - `fan_level_percent`
  - `cooling_on`
  - `event`
  - `beans_added`
  - `first_crack_detected`
  - `beans_dropped`
  - `development_time_percent`
  - `bean_ror_c_per_min`
  - `env_ror_c_per_min`
  - `bean_delta_60s_c`
  - `env_delta_60s_c`
  - `fc_model_repo`
  - `fc_model_revision`
  - `fc_model_precision`

Example telemetry JSONL row:

```json
{"timestamp_utc":"2026-04-30T10:15:31Z","elapsed_seconds":391.2,"phase":"roasting","bean_temp_c":176.4,"env_temp_c":204.8,"heat_level_percent":60,"fan_level_percent":35,"cooling_on":false,"event":null,"development_time_percent":null,"bean_ror_c_per_min":7.2,"env_ror_c_per_min":3.1,"bean_delta_60s_c":7.2,"env_delta_60s_c":3.1}
```

Example event JSONL row:

```json
{"timestamp_utc":"2026-04-30T10:18:42Z","elapsed_seconds":582.6,"phase":"development","event":"first_crack_detected","first_crack_detected":true,"bean_temp_c":194.1,"env_temp_c":216.3,"heat_level_percent":45,"fan_level_percent":50,"cooling_on":false,"development_time_percent":0.0,"fc_model_repo":"syamaner/coffee-first-crack-detection","fc_model_revision":"pinned-release","fc_model_precision":"int8"}
```

Example CSV header:

```csv
timestamp_utc,elapsed_seconds,phase,bean_temp_c,env_temp_c,heat_level_percent,fan_level_percent,cooling_on,event,beans_added,first_crack_detected,beans_dropped,development_time_percent,bean_ror_c_per_min,env_ror_c_per_min,bean_delta_60s_c,env_delta_60s_c,fc_model_repo,fc_model_revision,fc_model_precision
```

Example `summary.json`:

```json
{
  "session_id": "20260430-101000",
  "started_at_utc": "2026-04-30T10:10:00Z",
  "beans_added_at_utc": "2026-04-30T10:12:00Z",
  "first_crack_at_utc": "2026-04-30T10:18:42Z",
  "beans_dropped_at_utc": "2026-04-30T10:20:10Z",
  "total_roast_seconds": 490.0,
  "development_time_seconds": 88.0,
  "development_time_percent": 17.96,
  "roaster_driver": "hottop_kn8828b_2k_plus",
  "first_crack_model": {
    "repo_id": "syamaner/coffee-first-crack-detection",
    "revision": "pinned-release",
    "precision": "int8"
  }
}
```

## Distribution And Registry

- Source repo target: `github.com/syamaner/coffee-roaster-mcp`.
- Publish package to PyPI as `coffee-roaster-mcp`.
- Add console entrypoint `coffee-roaster-mcp`.
- Add root `server.json` for MCP Registry.
- Register as `io.github.syamaner/coffee-roaster-mcp`.
- Use `RoastPilot` as the human-facing title/display name.
- Add README verification string: `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
- Use `mcp-publisher` in GitHub Actions after PyPI publish.
- Align git tag, package version, PyPI version, and `server.json.version`.
- Keep GHCR container optional after v0.1.
- Hugging Face hosts model/data. PyPI hosts code. MCP Registry hosts discovery metadata.

Minimal `server.json` target:

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.syamaner/coffee-roaster-mcp",
  "title": "RoastPilot",
  "description": "Control coffee roasts, detect first crack, and export roast logs.",
  "version": "0.1.0",
  "packages": [
    {
      "registryType": "pypi",
      "identifier": "coffee-roaster-mcp",
      "version": "0.1.0",
      "transport": {
        "type": "stdio"
      }
    }
  ]
}
```

## Spec-Driven Delivery Model

This project will be delivered using the same spec-driven operating model as the first-crack detection rebuild. The goal is to keep agent work bounded by explicit rules, durable project state, and story-level acceptance criteria rather than relying on chat context alone.

### AGENTS.md Rulebook

Add a root `AGENTS.md` that defines:

- Python version, typing rules, linting, formatting, and test requirements.
- Dependency rules: all runtime and dev dependencies must be declared in `pyproject.toml`.
- Hardware safety rules for Hottop control, drop, cooling, and emergency stop.
- Model boundary rules: this repo consumes Hugging Face model artifacts but does not train, export, or sync models.
- Storage rules: no model weights, audio files, roast logs, or hardware captures committed to git.
- Distribution rules for PyPI, MCP Registry, and version alignment.
- Required state-reading workflow before implementation.

Required pre-task workflow:

1. Read `docs/state/registry.md`.
2. Open the active epic file.
3. Read the GitHub issue for the story.
4. Confirm acceptance criteria and current risks.
5. Work on a branch named `feature/{issue-number}-{slug}`.

Required post-task workflow:

1. Run the required checks.
2. Update the story status in the active epic file.
3. Update active context and decision notes if behavior changed.
4. Comment on the GitHub issue with what was built and how it was tested.
5. Open a PR referencing the story issue.

### Durable Epic State

Add project state files under `docs/state/`:

- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`

`registry.md` points to the active epic and summarizes the current project phase.

The active epic file tracks:

- Epic and story list.
- Story status.
- Active story.
- Current architecture decisions.
- Current risks.
- Hardware validation notes.
- Registry publishing notes.
- Active context for the next agent session.

### Story Specification Standard

Every story starts as a GitHub issue before implementation.

Each issue includes:

- Problem statement.
- In scope.
- Out of scope.
- Public interface or config changes.
- Implementation notes.
- Acceptance criteria.
- Required tests.
- Manual validation steps, when hardware or publishing is involved.

Risky stories require a short implementation plan before code, especially:

- Hottop command loop and packet handling.
- Drop, cooling, and emergency stop behavior.
- MCP Registry publishing.
- First-crack event integration into the roast timeline.
- Log export schema changes.

### Repo-Local Skills And Runbooks

Add repo-local skills or runbooks for repeatable workflows. These should encode exact command sequences and validation steps so the agent does not improvise.

Recommended skills:

- `mcp-dev`: install dev environment, run lint/typecheck/tests, start mock MCP server, call basic MCP tools, inspect generated logs.
- `mock-roast`: start mock session, mark beans added, inject first crack, change heat/fan, drop beans, export logs, validate JSONL/CSV/summary.
- `hottop-validation`: confirm serial port, connect, read telemetry, verify temp units, test heat/fan/drop/cooling/emergency stop, record validation notes.
- `release-registry`: verify version alignment, build package, validate `server.json`, publish PyPI, install from PyPI, run mock smoke test, publish MCP Registry metadata, verify registry listing.

Do not add model training, ONNX export, or Hugging Face sync skills to this repo. Those remain in the `coffee-first-crack-detection` model repo.

### Actor Responsibilities

Human owner:

- Roast safety.
- Architecture decisions.
- MCP tool contracts.
- Roaster abstraction boundaries.
- Metrics semantics.
- First-crack behavior acceptance.
- Hardware-ready release decision.

Coding agent:

- Implementation against accepted story specs.
- Tests.
- Packaging.
- Documentation.
- Runbooks.
- Release automation.

Code review:

- Type safety.
- API misuse.
- Dependency hygiene.
- Missing error handling.
- Test gaps.
- Documentation accuracy.

Do not rely on code review to validate coffee roasting safety, Hottop hardware behavior, first-crack signal quality, or roast metric semantics. Those remain explicit human-review responsibilities.

## Release Checklist

- Confirm tests pass locally.
- Confirm mock MCP server starts through console entrypoint.
- Confirm package builds cleanly.
- Confirm `server.json.version` matches package version.
- Confirm README contains MCP verification string.
- Pin HF model revision before release.
- Publish package to TestPyPI if desired.
- Install package from TestPyPI and run mock smoke test.
- Publish package to PyPI.
- Install package from PyPI and run mock smoke test.
- Authenticate `mcp-publisher` with GitHub OIDC in CI.
- Publish MCP Registry metadata.
- Confirm registry page renders expected package and install metadata.
- Create GitHub release from tag.
- Run manual Hottop validation before labeling a release hardware-ready.

## Epics, Stories, And Acceptance Criteria

### Epic 1: Repo, Packaging, And Developer Workflow

Stories:

- Create standalone GitHub repo `syamaner/coffee-roaster-mcp`.
- Add package scaffold using `coffee-roaster-mcp` as the PyPI name and `coffee_roaster_mcp` as the Python import package.
- Add config loading from YAML and env vars.
- Add dev commands for test, lint, typecheck, and mock server.
- Add CI for tests and package build.
- Add README and local run instructions.
- Add repo-local skills/runbooks.

Acceptance criteria:

- `coffee-roaster-mcp --help` works after editable install.
- Mock server can start without roaster hardware or model download.
- CI runs tests and package build.
- Config defaults allow a local mock run with no config file.

### Epic 2: MCP Runtime And Session Core

Stories:

- Implement stdio MCP entrypoint.
- Implement session lifecycle and event timeline.
- Implement MCP tools.
- Implement phase transitions.
- Implement emergency stop and fault recording.
- Build thin vertical slice with mock roaster and injected first crack.

Acceptance criteria:

- A mock roast can start, mark beans added, mark first crack, drop beans, and export logs.
- `get_roast_state` returns consistent timestamps, phase, metrics, and latest controls.
- First crack is recorded once unless manual override is explicitly allowed.
- Emergency stop records an event and calls the driver safety method.

### Epic 3: Roaster Abstraction And Hottop Driver

Stories:

- Define `RoasterDriver` interface and capabilities.
- Implement mock driver.
- Implement Hottop connection lifecycle.
- Implement Hottop command loop.
- Implement Hottop heat, fan, drop, cooling, stop cooling, and emergency stop.
- Implement Hottop packet parse/build and temp unit handling.
- Run Hottop integration verification spike.

Acceptance criteria:

- Mock driver passes contract tests.
- Hottop packet tests cover checksum, invalid packets, and temp conversion.
- Command loop starts and stops cleanly.
- Manual Hottop checklist passes before hardware-ready release label.

### Epic 4: First-Crack Detection With HF Models

Stories:

- Add HF artifact resolver.
- Load INT8 ONNX by default.
- Load FP32 ONNX by config.
- Support `local_model_dir`.
- Add audio capture and detector adapter.
- Feed confirmed detection into session timeline.

Acceptance criteria:

- INT8 resolver selects `onnx/int8/model_quantized.onnx`.
- FP32 resolver selects `onnx/fp32/model.onnx`.
- Offline local directory works without HF network access.
- Mocked detector output creates exactly one `first_crack_detected` event.

### Epic 5: Roast Metrics And Log Export

Stories:

- Implement rolling telemetry buffer.
- Compute elapsed time, development time, development percent.
- Compute 60s deltas and RoR.
- Write JSONL log rows.
- Export CSV and summary JSON.
- Add log schema tests.

Acceptance criteria:

- RoR is null before 10 seconds of samples.
- RoR and deltas are correct for regular and irregular sample intervals.
- JSONL, CSV, and summary include required fields.
- Event rows are written immediately, not only on the 1 Hz sample loop.

### Epic 6: Distribution And MCP Registry Publishing

Stories:

- Add PyPI metadata.
- Add README verification string.
- Add `server.json`.
- Add release workflow.
- Add MCP Registry publishing verification spike.
- Document install and hardware setup.

Acceptance criteria:

- Package installs from PyPI.
- `server.json` validates against current MCP schema.
- Registry publish flow is documented and tested before v0.1 release.
- Registry listing points to the PyPI package and stdio transport.

### Epic 7: End-To-End Validation And Release Readiness

Stories:

- Test full mock roast through MCP tools.
- Test package install smoke flow.
- Test MCP client connection.
- Run Hottop manual hardware validation.
- Produce v0.1 release checklist.

Acceptance criteria:

- Full mock roast works from install to exported logs.
- MCP client can discover and call tools.
- Manual hardware results are recorded.
- Release is tagged only after package, registry, and smoke tests pass.

## Spikes

Keep only three spikes:

- Hottop integration verification.
- MCP Registry publishing verification.
- Thin vertical slice through mock roaster, injected first crack, metrics, logs, and MCP tools.

Do not spike HF ONNX loading or basic audio detection unless implementation contradicts the working reference prototype.

## Test Plan

- Unit test session timing, event ordering, phase transitions, and development percent.
- Unit test rolling 60s deltas and RoR with irregular sample intervals.
- Unit test JSONL, CSV, and summary export.
- Unit test HF resolver for INT8, FP32, pinned revision, and local model dir.
- Unit test detector adapter with mocked ONNX outputs.
- Unit test mock driver and `RoasterDriver` contract.
- Unit test Hottop packet build/parse, checksum validation, temp conversion, invalid startup readings, and command-loop cleanup.
- Integration test full mock roast through MCP tools to exported logs.
- Packaging smoke test from built wheel.
- MCP client smoke test.
- Manual Hottop test for connect, telemetry, heat, fan, drop, cooling, stop cooling, and emergency stop.

## Blog Series Framing

RoastPilot is the next spec-driven production rebuild after the first-crack detection project.

Suggested framing:

- The first series productionized the ML detector.
- This series productionizes autonomous roast orchestration.
- The old two-MCP plus n8n system worked, but created synchronization debt.
- The rebuild moves timing, telemetry, first-crack events, development percent, RoR, logs, and hardware control into one runtime.
- The core story is not just "building an MCP server." It is using spec-driven development to turn a working prototype into a distributable, testable, hardware-aware MCP product.

Suggested post angle:

> The prototype proved the idea. The rebuild is about control: one clock, one event timeline, one log, one hardware abstraction, and one MCP surface an agent can safely use later.

Possible post title:

> Building RoastPilot: a Spec-Driven MCP for Autonomous Coffee Roasting

## Assumptions And Defaults

- v0.1 is local-first stdio MCP.
- Agent and n8n orchestration are out of scope.
- INT8 ONNX is the default runtime model.
- FP32 ONNX is supported for validation and quality comparison.
- The reference first-crack prototype is already proven.
- The model repo remains the source of truth for model production and Hugging Face sync.
- Auto-T0 detection is disabled by default.
- First-crack mode defaults to `disabled` so mock install and registry smoke tests do not require model download or audio hardware.
- Release builds should pin the Hugging Face model revision.
- The MCP Registry is preview, so registry publishing gets a verification story before release.
