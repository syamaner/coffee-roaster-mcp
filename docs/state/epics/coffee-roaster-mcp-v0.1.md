# RoastPilot v0.1 Epic

## Epic Summary

Build RoastPilot as a standalone Python MCP server published as `coffee-roaster-mcp`. The server owns roaster control, telemetry, first-crack detection integration, roast timing, derived metrics, event logging, and export in one local stdio process.

The first implementation milestone is a mock vertical slice that requires no roaster hardware and no model download.

## Status Legend

- `[ ]` Not started
- `[~]` In progress
- `[x]` Complete
- `[!]` Blocked

## Active Context

- Current phase: Bootstrap
- Active story: `E1-S3`
- Current target: CLI basics
- Product/display name: `RoastPilot`
- GitHub repo: `syamaner/coffee-roaster-mcp`
- PyPI package: `coffee-roaster-mcp`
- Python import package: `coffee_roaster_mcp`
- Console entrypoint: `coffee-roaster-mcp`
- MCP Registry name: `io.github.syamaner/coffee-roaster-mcp`

## Current Decisions

- v0.1 uses local stdio MCP transport.
- Agent and n8n orchestration are out of scope.
- Default roaster driver is `mock`.
- First-crack mode defaults to `disabled` so mock install and registry smoke tests do not require audio hardware or model download.
- ONNX INT8 is the default real model backend.
- ONNX FP32 is supported by config.
- The `coffee-first-crack-detection` repo remains the source of truth for training, ONNX export, Hugging Face sync, model cards, and dataset cards.
- This repo consumes released Hugging Face artifacts only.
- Auto-T0 detection is disabled by default. `mark_beans_added` is authoritative.

## Current Risks

- Hottop command-loop behavior and temperature units need hardware verification.
- MCP Registry publishing is preview and needs verification before release.
- First-crack event integration must preserve one authoritative session timeline.
- Log schema changes need compatibility discipline once users start collecting roast logs.

## Definition Of Done

Every implementation story is done only when:

- The story acceptance criteria are met.
- Required unit or integration tests are added or updated.
- `ruff check`, formatting check, typecheck, and tests pass once the tooling exists.
- Public docs or runbooks are updated when operator behavior changes.
- The active epic state is updated with status, decision notes, and validation notes.

## Epic 1: Repo, Packaging, And Developer Workflow

Goal: create a usable standalone Python project with durable state, dev workflow, and mock-first defaults.

### Stories

- [x] `E1-S1` Create standalone repo state and project plan docs.
  - Done when `docs/state/registry.md`, this active epic, and `docs/plans/coffee-roaster-mcp-v0.1-overall-plan.md` exist in the repo.

- [x] `E1-S2` Add Python package scaffold.
  - Done when `pyproject.toml`, `src/coffee_roaster_mcp/`, `tests/`, and the `coffee-roaster-mcp` console entrypoint exist.

- [ ] `E1-S3` Add CLI basics.
  - Done when `coffee-roaster-mcp --help` and `coffee-roaster-mcp --version` work after editable install.

- [ ] `E1-S4` Add config loading from YAML and environment variables.
  - Done when config defaults allow a local mock run with no config file and documented env overrides are supported.

- [ ] `E1-S5` Add local dev commands.
  - Done when there are documented commands for lint, format check, typecheck, tests, and mock server run.

- [ ] `E1-S6` Add CI for tests and package build.
  - Done when GitHub Actions runs checks and builds the package on pull requests.

- [ ] `E1-S7` Add initial README and install/run documentation.
  - Done when README explains RoastPilot, local mock run, Hottop config placeholder, Hugging Face model boundary, and log export.

- [ ] `E1-S8` Add repo-local skills or runbooks.
  - Done when `mcp-dev`, `mock-roast`, `hottop-validation`, and `release-registry` workflows exist as repo-local docs or skills.

### Epic Acceptance Criteria

- `coffee-roaster-mcp --help` works after editable install.
- Mock server can start without roaster hardware or model download.
- CI runs tests and package build.
- Config defaults allow a local mock run with no config file.

## Epic 2: MCP Runtime And Session Core

Goal: implement one authoritative roast session runtime and MCP tool surface.

### Stories

- [ ] `E2-S1` Implement stdio MCP server entrypoint.
  - Done when the server starts locally and exposes a minimal tool list.

- [ ] `E2-S2` Implement `RoastSession` lifecycle.
  - Done when sessions have id, monotonic clock, phase, event timeline, telemetry buffer, and log writer references.

- [ ] `E2-S3` Implement core event timeline.
  - Done when `beans_added`, `first_crack_detected`, `beans_dropped`, `cooling_started`, `cooling_stopped`, and `fault` can be recorded with timestamps.

- [ ] `E2-S4` Implement core MCP tools.
  - Done when `start_roast_session`, `get_roast_state`, `set_heat`, `set_fan`, `mark_beans_added`, `mark_first_crack`, `drop_beans`, `start_cooling`, `stop_cooling`, `export_roast_log`, and `emergency_stop` exist.

- [ ] `E2-S5` Implement phase transitions.
  - Done when session phase changes are deterministic for pre-roast, roasting, development, dropped, cooling, complete, and fault states.

- [ ] `E2-S6` Implement emergency stop and fault recording.
  - Done when emergency stop records an event and calls the active driver safety method.

- [ ] `E2-S7` Complete thin vertical slice spike.
  - Done when a mock roast can start, mark beans added, inject first crack, drop beans, return state, and export logs in one process.

### Epic Acceptance Criteria

- A mock roast can start, mark beans added, mark first crack, drop beans, and export logs.
- `get_roast_state` returns consistent timestamps, phase, metrics, and latest controls.
- First crack is recorded once unless manual override is explicitly allowed.
- Emergency stop records an event and calls the driver safety method.

## Epic 3: Roaster Abstraction And Hottop Driver

Goal: support multiple roasters behind one driver contract while preserving current Hottop behavior.

### Stories

- [ ] `E3-S1` Define `RoasterDriver` interface and capabilities model.
  - Done when drivers expose connection lifecycle, read state, heat/fan control, drop, cooling, emergency stop, and capabilities.

- [ ] `E3-S2` Implement mock driver.
  - Done when the mock driver supports deterministic telemetry and passes contract tests.

- [ ] `E3-S3` Implement normalized roaster state model.
  - Done when driver state includes bean temp C, env temp C, heat %, fan %, cooling on/off, connected, and raw vendor data.

- [ ] `E3-S4` Implement Hottop serial connection lifecycle.
  - Done when the Hottop driver can connect, disconnect, and clean up without leaving command loops running.

- [ ] `E3-S5` Implement Hottop command loop.
  - Done when continuous command streaming around 0.3s is implemented and lifecycle-tested.

- [ ] `E3-S6` Implement Hottop packet build/parse.
  - Done when 36-byte packet construction, status parsing, and checksum validation are unit tested.

- [ ] `E3-S7` Implement Hottop heat, fan, drop, cooling, stop cooling, and emergency stop.
  - Done when commands preserve safe defaults and known compound drop/cooling behavior.

- [ ] `E3-S8` Implement Hottop temperature unit handling.
  - Done when `celsius`, `fahrenheit`, and `auto` modes are supported and tested with plausible readings.

- [ ] `E3-S9` Run Hottop integration verification spike.
  - Done when packet parsing, temp units, command cadence, drop, cooling, and cleanup have manual validation notes.

### Epic Acceptance Criteria

- Mock driver passes contract tests.
- Hottop packet tests cover checksum, invalid packets, and temp conversion.
- Command loop starts and stops cleanly.
- Manual Hottop checklist passes before hardware-ready release label.

## Epic 4: First-Crack Detection With HF Models

Goal: consume released Hugging Face model artifacts and feed first-crack events into the single roast timeline.

### Stories

- [ ] `E4-S1` Add Hugging Face artifact resolver.
  - Done when model files can be resolved from `syamaner/coffee-first-crack-detection` using configured revision.

- [ ] `E4-S2` Load INT8 ONNX by default.
  - Done when `onnx/int8/model_quantized.onnx` is selected for `precision: int8`.

- [ ] `E4-S3` Load FP32 ONNX by config.
  - Done when `onnx/fp32/model.onnx` is selected for `precision: fp32`.

- [ ] `E4-S4` Support local offline model directory.
  - Done when `local_model_dir` works without Hugging Face network access.

- [ ] `E4-S5` Validate required detector artifacts before detection starts.
  - Done when missing ONNX model or feature extractor files fail clearly before audio detection begins.

- [ ] `E4-S6` Add audio capture pipeline.
  - Done when configured audio input can feed detector windows without blocking roaster telemetry.

- [ ] `E4-S7` Add detector adapter.
  - Done when detector output maps to a confirmed first-crack event with timestamp, precision, revision, and confidence when available.

- [ ] `E4-S8` Integrate first crack with session timeline.
  - Done when mocked detector output creates exactly one `first_crack_detected` event.

### Epic Acceptance Criteria

- INT8 resolver selects `onnx/int8/model_quantized.onnx`.
- FP32 resolver selects `onnx/fp32/model.onnx`.
- Offline local directory works without HF network access.
- Mocked detector output creates exactly one `first_crack_detected` event.

## Epic 5: Roast Metrics And Log Export

Goal: compute roast metrics from one session clock and export durable logs.

### Stories

- [ ] `E5-S1` Implement rolling telemetry buffer.
  - Done when bean/env samples are retained for rolling metric calculations.

- [ ] `E5-S2` Compute elapsed roast time.
  - Done when `roast_elapsed_seconds` is computed from `beans_added_at` to now or drop.

- [ ] `E5-S3` Compute development time and percent.
  - Done when development time starts at first crack and development percent is `development_time_seconds / roast_elapsed_seconds * 100`.

- [ ] `E5-S4` Compute 60s bean/env deltas.
  - Done when latest minus oldest sample in rolling 60s window is returned for bean and environment temps.

- [ ] `E5-S5` Compute bean/env RoR.
  - Done when RoR is normalized to C/min and returns null before 10 seconds of samples.

- [ ] `E5-S6` Write append-only JSONL roast log.
  - Done when telemetry rows are written at 1 Hz and event rows are written immediately.

- [ ] `E5-S7` Export CSV roast log.
  - Done when CSV includes all required columns from the plan.

- [ ] `E5-S8` Export `summary.json`.
  - Done when summary includes session timestamps, total roast seconds, development metrics, roaster driver, and first-crack model metadata.

- [ ] `E5-S9` Add log schema tests.
  - Done when JSONL, CSV, and summary schema completeness is covered by tests.

### Epic Acceptance Criteria

- RoR is null before 10 seconds of samples.
- RoR and deltas are correct for regular and irregular sample intervals.
- JSONL, CSV, and summary include required fields.
- Event rows are written immediately, not only on the 1 Hz sample loop.

## Epic 6: Distribution And MCP Registry Publishing

Goal: make RoastPilot installable and discoverable through PyPI and the MCP Registry.

### Stories

- [ ] `E6-S1` Add PyPI package metadata.
  - Done when package metadata is complete for `coffee-roaster-mcp`.

- [ ] `E6-S2` Add README MCP verification string.
  - Done when README includes `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.

- [ ] `E6-S3` Add `server.json`.
  - Done when registry metadata uses name `io.github.syamaner/coffee-roaster-mcp`, title `RoastPilot`, package `coffee-roaster-mcp`, and stdio transport.

- [ ] `E6-S4` Add version alignment check.
  - Done when package version and `server.json.version` cannot drift unnoticed.

- [ ] `E6-S5` Add release workflow.
  - Done when CI can build, test, publish to PyPI, and publish registry metadata after tag release.

- [ ] `E6-S6` Run MCP Registry publishing verification spike.
  - Done when `server.json`, PyPI verification, and `mcp-publisher` flow are documented and tested before v0.1 release.

- [ ] `E6-S7` Document install and hardware setup.
  - Done when docs cover mock install, Hottop config, Hugging Face model config, offline model path, and log output paths.

### Epic Acceptance Criteria

- Package installs from PyPI.
- `server.json` validates against current MCP schema.
- Registry publish flow is documented and tested before v0.1 release.
- Registry listing points to the PyPI package and stdio transport.

## Epic 7: End-To-End Validation And Release Readiness

Goal: prove the package works from install through mock roast, MCP client calls, hardware validation, and release.

### Stories

- [ ] `E7-S1` Test full mock roast through MCP tools.
  - Done when a mock roast works from session start to exported logs.

- [ ] `E7-S2` Test package install smoke flow.
  - Done when a built wheel can be installed and `coffee-roaster-mcp --help` works.

- [ ] `E7-S3` Test MCP client connection.
  - Done when a real MCP client can discover and call the server tools.

- [ ] `E7-S4` Run Hottop manual hardware validation.
  - Done when manual validation results are recorded against the checklist.

- [ ] `E7-S5` Produce v0.1 release checklist.
  - Done when release steps cover tests, package build, version alignment, HF revision pin, PyPI publish, registry publish, GitHub release, and hardware-ready labeling.

### Epic Acceptance Criteria

- Full mock roast works from install to exported logs.
- MCP client can discover and call tools.
- Manual hardware results are recorded.
- Release is tagged only after package, registry, and smoke tests pass.

## Spikes

### `SP1` Thin Vertical Slice

- Epic: `E2`
- Goal: prove one process can run mock roaster telemetry, injected first crack, metrics, logs, and MCP tool calls.
- Output: working mock flow and notes on any architecture changes.

### `SP2` Hottop Integration Verification

- Epic: `E3`
- Goal: confirm packet parsing, temperature units, checksum behavior, command cadence, drop, cooling, and cleanup on real hardware.
- Output: validation notes and final driver decisions.

### `SP3` MCP Registry Publishing Verification

- Epic: `E6`
- Goal: verify `server.json`, PyPI verification string, and `mcp-publisher` flow while the registry is preview.
- Output: validated metadata template and release checklist updates.

## Story Workflow

Before starting a story:

1. Read `docs/state/registry.md`.
2. Read this active epic.
3. Read the linked GitHub issue.
4. Confirm acceptance criteria and current risks.
5. Create branch `feature/{issue-number}-{slug}`.

After completing a story:

1. Run required checks.
2. Update story status in this epic.
3. Update Active Context and Current Decisions if behavior changed.
4. Add validation notes.
5. Comment on the GitHub issue with what changed and how it was tested.
6. Open a PR referencing the issue.

## Validation Notes

- E1-S1 created durable state docs and the copied overall plan.
- E1-S2 added the initial Python package scaffold, console entrypoint declaration, package module, CLI module, and package smoke tests.
- Validation run for E1-S2:
  - Parsed `pyproject.toml` with stdlib `tomllib` and confirmed package name plus console script target.
  - Ran `PYTHONPATH=src` package import and CLI parser smoke check successfully.
  - Full `pytest` execution is pending dev environment setup because ambient Python does not have `pytest` installed yet.
