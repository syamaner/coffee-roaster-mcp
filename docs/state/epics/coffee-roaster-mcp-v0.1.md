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
- Active story: `E3-S9`
- Current target: Run the Hottop integration verification spike
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
- `E2-S1` keeps the initial MCP tool surface bootstrap-safe with `get_server_info` and `get_runtime_config`. Roast-session lifecycle and roast-control tools remain later Epic 2 work.
- `E2-S2` uses one in-process `RoastSessionStore` with at most one active `RoastSession` at a time. Session state now owns monotonic timing, phase, event timeline storage, telemetry retention, and log-writer references before tool wiring lands.
- `E2-S3` keeps event writes behind `RoastSessionStore.record_event(...)`. The session timeline now records deterministic append order, authoritative UTC plus monotonic timestamps for core roast events, and idempotent singleton handling for beans added, first crack, bean drop, and cooling transitions.
- `E2-S4` exposed the first roast-session MCP tools on top of the mock path. At that point `export_roast_log` returned a planned manifest only; E2-S7 replaced that placeholder with snapshot JSONL, CSV, and summary file exports.
- `E2-S5` now enforces phase-ordered roast events inside `RoastSessionStore`. New events must start with `pre_roast -> roasting`, may move through `development` when first crack is recorded, may drop directly from `roasting` when first crack is not recorded, and then continue through `dropped -> cooling -> complete`; repeated singleton calls keep their original event rows and fault rows remain appendable without resetting the first-fault timestamp.
- `E2-S6` keeps `RoastSessionStore` as the one-session mutation boundary but moves emergency-stop safety behavior behind the configured driver boundary. The current mock driver fail-closes heat to `0`, fan to `100`, and cooling to `on`; the store records the resulting fault event and stops the session, and MCP responses expose the fault payload plus final session state.
- `E2-S7` completes the first one-process mock vertical slice. `export_roast_log` now writes snapshot JSONL, CSV, and summary files from the current session state, and `get_roast_state` exposes minimal timestamp-derived roast and development metrics. Append-only telemetry writers and final export schemas remain Epic 5 work.
- `E2-S8` completed the final Epic 2 hardening story before broader driver contract work. Pull-request CI now runs tests with coverage for `coffee_roaster_mcp`, writes an easy-to-read GitHub Actions Markdown summary, and uploads an `html-coverage-report` artifact without adding an external hosted coverage service.
- `E3-S1` defines the broader `RoasterDriver` contract without changing MCP tool semantics. Drivers now expose connection lifecycle, normalized state reads, heat and fan controls, drop, cooling, driver-owned emergency stop, and static capabilities for ranges, supported actions, sensor units, and command-streaming requirements. The current mock driver implements this contract while preserving E2 emergency-stop fail-closed behavior.
- `E3-S2` keeps the mock driver deterministic and local-only by advancing a fixed one-second thermal sample on `read_state`. Mock heat raises environment temperature, fan and cooling reduce it, bean temperature follows environment temperature with lag, and control commands return the current state without advancing telemetry. This preserves the current one-session MCP/store semantics while giving later stories a stable driver telemetry source.
- `E3-S3` hardens `RoasterState` as the normalized driver-boundary model. State construction now validates non-empty driver ids, exact boolean connection/cooling flags, finite Celsius temperatures, heat and fan control percentages, and flat raw-vendor diagnostic payloads.
- `E3-S4` adds the first Hottop driver lifecycle slice. The driver can be constructed for `hottop_kn8828b_2k_plus`, requires an explicit serial port before connect, opens serial transport lazily through pyserial without holding the state-read lock, starts a command-loop lifecycle thread on connect, and disconnects by signalling the loop, joining it, closing serial transport, and clearing runtime references. Review hardening keeps Hottop capabilities accurate while controls are not implemented, passes configured port/baudrate/command interval into the driver, closes serial even when join times out, blocks reconnect while a prior command loop is still alive, and uses deterministic test synchronization for loop iteration tests. Packet sending and hardware commands remain later Epic 3 stories.
- `E3-S5` keeps packet bytes and status parsing out of scope but completes the Hottop command-loop scheduler. The loop ticks at the configured cadence, can stream injectable command frames to the serial transport, records frame polls, send attempts, successful writes, last write size, and write errors in raw diagnostics, and defaults to sending no unverified hardware bytes until `E3-S6` implements the Hottop packet format.
- `E3-S6` implements deterministic Hottop packet build/parse primitives without turning on live Hottop hardware commands. The driver module can build 36-byte command packets with checksum bytes, validate packet checksums, parse exact 36-byte status packets, and scan serial buffers for the first valid status packet with raw Celsius bean and environment temperatures. The command loop still keeps the safe no-default-frame behavior until `E3-S7` wires these packet primitives to explicit control commands.
- `E3-S7` wires Hottop control methods to driver-owned command state and the E3-S6 packet builder. Connected Hottop loops now stream verified safe-zero packets by default and then stream the latest explicit heat, main fan, drop, cooling, stop-cooling, or emergency-stop command state. Command methods remain safe to call before connection because no serial bytes are written until the driver lifecycle is opened.
- `E3-S8` adds Hottop temperature unit handling at the driver boundary. Configured `celsius`, `fahrenheit`, and `auto` raw status-packet modes now normalize plausible bean and environment readings to Celsius before `RoasterState` exposure, and startup zero or implausible packet values are ignored until plausible telemetry arrives.
- The old `coffee-roasting` prototype was checked as a behavioral reference for E3-S1. It confirmed the need to model Hottop command streaming, temperature-unit normalization, compound drop/cooling behavior, and cleanup through driver lifecycle methods. Drum control remains an internal driver concern for now because E3-S1 and issue #23 do not require a public drum command.
- ONNX INT8 is the default real model backend.
- ONNX FP32 is supported by config.
- The `coffee-first-crack-detection` repo remains the source of truth for training, ONNX export, Hugging Face sync, model cards, and dataset cards.
- This repo consumes released Hugging Face artifacts only.
- Auto-T0 detection is disabled by default. `mark_beans_added` is authoritative.
- Configuration loads from mock-safe defaults, optional `coffee-roaster-mcp.yaml`, and environment overrides. YAML file support uses PyYAML as a declared runtime dependency.
- Agent rules and repo-local workflows are now part of the scaffold. `AGENTS.md`, `.claude/skills/code-quality`, `.claude/skills/mcp-dev`, `.claude/skills/mock-roast`, `.claude/skills/hottop-validation`, `.claude/skills/release-registry`, and Copilot review instructions should be kept current as story workflow changes.
- The old `coffee-roasting` POC is a behavior reference for Epic 2, especially `roaster_control/mcp_server.py`, `roaster_control/server.py`, `roaster_control/session_manager.py`, and `roaster_control/roast_tracker.py`. It is not a template for carrying forward the old split MCP, Auth0, SSE, or `n8n` architecture.

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

- [x] `E1-S3` Add CLI basics.
  - Done when `coffee-roaster-mcp --help` and `coffee-roaster-mcp --version` work after editable install.

- [x] `E1-S4` Add config loading from YAML and environment variables.
  - Done when config defaults allow a local mock run with no config file and documented env overrides are supported.

- [x] `E1-S5` Add local dev commands.
  - Done when there are documented commands for lint, format check, typecheck, tests, and mock server run.

- [x] `E1-S6` Add CI for tests and package build.
  - Done when GitHub Actions runs checks and builds the package on pull requests.

- [x] `E1-S7` Add initial README and install/run documentation.
  - Done when README explains RoastPilot, local mock run, Hottop config placeholder, Hugging Face model boundary, and log export.

- [x] `E1-S8` Add repo-local skills or runbooks.
  - Done when `mcp-dev`, `mock-roast`, `hottop-validation`, and `release-registry` workflows exist as repo-local docs or skills.

### Epic Acceptance Criteria

- `coffee-roaster-mcp --help` works after editable install.
- Mock server can start without roaster hardware or model download.
- CI runs tests and package build.
- Config defaults allow a local mock run with no config file.

## Epic 2: MCP Runtime And Session Core

Goal: implement one authoritative roast session runtime and MCP tool surface.

### Stories

- [x] `E2-S1` Implement stdio MCP server entrypoint.
  - Done when the server starts locally and exposes a minimal tool list.

- [x] `E2-S2` Implement `RoastSession` lifecycle.
  - Done when sessions have id, monotonic clock, phase, event timeline, telemetry buffer, and log writer references.

- [x] `E2-S3` Implement core event timeline.
  - Done when `beans_added`, `first_crack_detected`, `beans_dropped`, `cooling_started`, `cooling_stopped`, and `fault` can be recorded with timestamps.

- [x] `E2-S4` Implement core MCP tools.
  - Done when `start_roast_session`, `get_roast_state`, `set_heat`, `set_fan`, `mark_beans_added`, `mark_first_crack`, `drop_beans`, `start_cooling`, `stop_cooling`, `export_roast_log`, and `emergency_stop` exist.

- [x] `E2-S5` Implement phase transitions.
  - Done when session phase changes are deterministic for pre-roast, roasting, development, dropped, cooling, complete, and fault states.

- [x] `E2-S6` Implement emergency stop and fault recording.
  - Done when emergency stop records an event and calls the active driver safety method.

- [x] `E2-S7` Complete thin vertical slice spike.
  - Done when a mock roast can start, mark beans added, inject first crack, drop beans, return state, and export logs in one process.

- [x] `E2-S8` Add GitHub Actions code coverage reporting.
  - Done when CI runs tests with coverage for `coffee_roaster_mcp`, publishes a readable GitHub Actions summary, uploads a visually useful HTML coverage artifact, and documents how to read the output.

### Epic Acceptance Criteria

- A mock roast can start, mark beans added, mark first crack, drop beans, and export logs.
- `get_roast_state` returns consistent timestamps, phase, metrics, and latest controls.
- First crack is recorded once unless manual override is explicitly allowed.
- Emergency stop records an event and calls the driver safety method.
- Coverage output is visible from GitHub Actions without reading raw test logs.

## Epic 3: Roaster Abstraction And Hottop Driver

Goal: support multiple roasters behind one driver contract while preserving current Hottop behavior.

### Stories

- [x] `E3-S1` Define `RoasterDriver` interface and capabilities model.
  - Done when drivers expose connection lifecycle, read state, heat/fan control, drop, cooling, emergency stop, and capabilities.

- [x] `E3-S2` Implement mock driver.
  - Done when the mock driver supports deterministic telemetry and passes contract tests.

- [x] `E3-S3` Implement normalized roaster state model.
  - Done when driver state includes bean temp C, env temp C, heat %, fan %, cooling on/off, connected, and raw vendor data.

- [x] `E3-S4` Implement Hottop serial connection lifecycle.
  - Done when the Hottop driver can connect, disconnect, and clean up without leaving command loops running.

- [x] `E3-S5` Implement Hottop command loop.
  - Done when continuous command streaming around 0.3s is implemented and lifecycle-tested.

- [x] `E3-S6` Implement Hottop packet build/parse.
  - Done when 36-byte packet construction, status parsing, and checksum validation are unit tested.

- [x] `E3-S7` Implement Hottop heat, fan, drop, cooling, stop cooling, and emergency stop.
  - Done when commands preserve safe defaults and known compound drop/cooling behavior.

- [x] `E3-S8` Implement Hottop temperature unit handling.
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
- E1-S3 added CLI help/version behavior and smoke coverage.
- E1-S4 added typed config dataclasses, YAML loading, environment override precedence, config documentation, and focused tests. Copilot review hardening added whitespace/case normalization, empty log-dir validation, conventional runtime type checks, and cached config path existence checks.
- E1-S5 added one documented local development workflow across `README.md`, `AGENTS.md`, and `.claude/skills/mcp-dev`. The documented commands now cover setup, tests, lint, format check, typecheck, CLI smoke, and a mock-safe bootstrap smoke path until the stdio MCP server lands in `E2-S1`.
- E1-S6 added a GitHub Actions CI workflow for pull requests plus manual runs. CI now installs project dev dependencies, runs tests, lint, format check, typecheck, CLI smoke checks, and builds sdist plus wheel artifacts. Package build tooling is declared in `pyproject.toml`.
- E1-S7 expanded the initial README into a real install and usage entrypoint. It now explains RoastPilot versus `coffee-roaster-mcp`, the local mock path, the current Hottop placeholder, the Hugging Face model boundary, and the planned log-export behavior without claiming unfinished runtime features.
- E1-S8 completed the repo-local workflow set. The repo now has skills for code quality, scaffold-level MCP setup, mock roast bootstrap validation, guarded Hottop validation, and staged registry release preparation. Those runbooks explicitly keep model training, ONNX export, and Hugging Face sync out of this repo.
- Epic 2 planning now has a local crosswalk for the old `coffee-roasting` POC. It identifies the old stdio entrypoint, tool registration, session manager, and roast tracker as the main implementation references while explicitly rejecting the old split-server and orchestration architecture.
- E2-S1 added the first local stdio FastMCP entrypoint, `coffee-roaster-mcp serve`, and a bootstrap-safe runtime tool surface with `get_server_info` and `get_runtime_config`. It keeps roast-session lifecycle and roast-control tools out of the entrypoint story.
- E2-S2 added `session.py` with an authoritative `RoastSession` model and `RoastSessionStore`. Session lifecycle now has stable ids, monotonic start/stop timing, explicit phase, event-timeline storage, telemetry-buffer retention, log-writer references, and clean single-owner start/stop behavior.
- E2-S3 extended `session.py` with store-owned event recording. The authoritative session now records `beans_added`, `first_crack_detected`, `beans_dropped`, `cooling_started`, `cooling_stopped`, and `fault` in deterministic timeline order while also keeping authoritative UTC and monotonic timestamps for core roast milestones.
- E2-S4 extended the FastMCP runtime with the first real roast-session tool surface. The mock path now supports starting a session, reading state, setting in-memory heat and fan values, recording core events, starting and stopping cooling, returning a planned export manifest, and recording emergency-stop faults through one authoritative session owner.
- Validation run for E1-S8:
  - Reviewed issue #15 acceptance criteria against `AGENTS.md`, the active epic, and the overall plan.
  - Added `.claude/skills/mock-roast`, `.claude/skills/hottop-validation`, and `.claude/skills/release-registry` using the existing repo-local skill format.
  - Confirmed the new runbooks keep model training, ONNX export, and Hugging Face sync in `coffee-first-crack-detection`.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pytest`: 17 passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m ruff check .`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m ruff format --check .`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pyright --pythonpath /tmp/roastpilot-e1s6-venv/bin/python`: 0 errors.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/coffee-roaster-mcp --help` and `--version`: passed.
  - Ran the mock-safe bootstrap smoke command and confirmed output `mock disabled int8`.
- Validation run for E1-S2:
  - Parsed `pyproject.toml` with stdlib `tomllib` and confirmed package name plus console script target.
  - Ran `PYTHONPATH=src` package import and CLI parser smoke check successfully.
  - Full `pytest` execution is pending dev environment setup because ambient Python does not have `pytest` installed yet.
- Validation run for E1-S3:
  - Parsed `pyproject.toml` and confirmed pytest `pythonpath` config.
  - Ran `PYTHONPATH=src` `--help` and `--version` smoke checks successfully.
- Validation run for E1-S4:
  - Created a temporary virtualenv at `/tmp/roastpilot-e1s4-venv` and installed the package with dev dependencies.
  - Ran `/tmp/roastpilot-e1s4-venv/bin/python -m pytest`: 17 passed.
  - Ran `/tmp/roastpilot-e1s4-venv/bin/python -m ruff check .`: passed.
  - Ran `/tmp/roastpilot-e1s4-venv/bin/python -m pyright --pythonpath /tmp/roastpilot-e1s4-venv/bin/python`: 0 errors.
  - PR #65 remains open and mergeable. GitHub issue #11 remains open until PR merge.
- Validation run for E1-S5:
  - Reviewed the documented commands against `pyproject.toml`, `AGENTS.md`, `README.md`, and `.claude/skills/mcp-dev`.
  - Created a temporary virtualenv at `/tmp/roastpilot-e1s5-venv` and installed the package with dev dependencies.
  - Ran `/tmp/roastpilot-e1s5-venv/bin/python -m pytest`: 17 passed.
  - Ran `/tmp/roastpilot-e1s5-venv/bin/python -m ruff check .`: passed.
  - Ran `/tmp/roastpilot-e1s5-venv/bin/python -m ruff format --check .`: passed.
  - Ran `/tmp/roastpilot-e1s5-venv/bin/python -m pyright --pythonpath /tmp/roastpilot-e1s5-venv/bin/python`: 0 errors.
  - Ran `/tmp/roastpilot-e1s5-venv/bin/coffee-roaster-mcp --help` and `--version`: passed.
  - Ran the documented bootstrap smoke command and confirmed output `mock disabled int8`.
- Validation run for E1-S6:
  - Created a temporary virtualenv at `/tmp/roastpilot-e1s6-venv` and installed the package with dev dependencies.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pytest`: 17 passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m ruff check .`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m ruff format --check .`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pyright --pythonpath /tmp/roastpilot-e1s6-venv/bin/python`: 0 errors.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/coffee-roaster-mcp --help` and `--version`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m build`: passed when rerun with network access because isolated build environments must fetch `hatchling`.
- Validation run for E1-S7:
  - Reviewed `README.md` against the story acceptance criteria for product naming, local mock run, Hottop placeholder, Hugging Face model boundary, and log export coverage.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pytest`: 17 passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m ruff check .`: passed.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/python -m pyright --pythonpath /tmp/roastpilot-e1s6-venv/bin/python`: 0 errors.
  - Ran `/tmp/roastpilot-e1s6-venv/bin/coffee-roaster-mcp --help` and `--version`: passed.
- Validation run for E2-S1:
  - Added `mcp>=1.0.0,<2` as a declared runtime dependency and configured local pyright venv resolution.
  - Added `src/coffee_roaster_mcp/mcp_server.py` with a FastMCP stdio server and bootstrap-safe introspection tools only.
  - Added `coffee-roaster-mcp serve` and a module `__main__` guard so the entrypoint works through both the console script and `python -m coffee_roaster_mcp.cli serve`.
  - Updated `README.md`, `AGENTS.md`, `.claude/skills/mcp-dev`, and `.claude/skills/mock-roast` so they no longer claim the stdio server is missing.
  - Ran `./.venv/bin/python -m pytest`: 18 passed, including a stdio startup smoke test that initialized the server and listed tools over MCP.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help` and `--version`: passed.
- Validation run for E2-S2:
  - Added `src/coffee_roaster_mcp/session.py` with `RoastSession`, `RoastEvent`, `TelemetrySample`, `LogWriterReference`, `SessionLifecycleError`, and `RoastSessionStore`.
  - Wired the MCP server lifespan to own one authoritative `RoastSessionStore` for later tool stories.
  - Added `tests/test_session.py` covering session creation, single-owner enforcement, clean stop semantics, and rolling telemetry retention.
  - Ran `./.venv/bin/python -m pytest`: 24 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S3:
  - Extended `src/coffee_roaster_mcp/session.py` with authoritative per-event timestamp fields and store-owned `record_event(...)`.
  - Kept singleton event writes idempotent for `beans_added`, `first_crack_detected`, `beans_dropped`, `cooling_started`, and `cooling_stopped` while allowing `fault` timeline rows.
  - Added `tests/test_session.py` coverage for deterministic event ordering, authoritative timestamp updates, singleton idempotency, and rejecting stopped-session event writes.
  - Ran `./.venv/bin/python -m pytest`: 32 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S4:
  - Extended `src/coffee_roaster_mcp/mcp_server.py` with the first roast-session MCP tools on top of the authoritative session core.
  - Extended `src/coffee_roaster_mcp/session.py` with in-memory mock control state for heat, fan, and cooling plus store-owned mutation helpers used by the MCP tools.
  - Added `tests/test_package.py` coverage for tool registration and a basic end-to-end mock tool flow over stdio MCP, including session start, control updates, event recording, state reads, export-manifest response, and emergency-stop fault recording.
  - Updated `README.md` and `.claude/skills/mock-roast/SKILL.md` so they no longer describe the MCP runtime as introspection-only.
  - Ran `./.venv/bin/python -m pytest`: 35 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S5:
  - Extended `src/coffee_roaster_mcp/session.py` with an explicit allowed-phase map for new event writes while preserving store-owned idempotent singleton handling and repeatable fault rows.
  - Added `tests/test_session.py` coverage for invalid pre-roast transitions, roast-to-drop ordering, repeated singleton behavior after later phase changes, and rejecting first-crack reports after drop.
  - Added `tests/test_package.py` coverage that invalid MCP tool calls surface phase-transition errors over stdio before a valid roast sequence starts.
  - Used the old `coffee-roasting` repo only as a behavioral reference for roast ordering and development/drop/cooling semantics, not as an architecture template.
  - Ran `./.venv/bin/python -m pytest`: 53 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S6:
  - Added `src/coffee_roaster_mcp/drivers.py` with a minimal `RoasterSafetyDriver` protocol and `MockRoasterDriver` emergency-stop safety method.
  - Routed the MCP `emergency_stop` tool through the configured mock driver safety method while keeping `RoastSessionStore` responsible for fault recording, session stop semantics, and snapshots.
  - Added unit coverage for the mock driver emergency-stop behavior, store-owned safety payload application, driver failure fail-closed fallback, payload collision handling, stopped-latest fault recording after a driver-side emergency stop, and MCP fault payload visibility through `get_roast_state`.
  - Review hardening moved driver emergency-stop execution outside the store lock, falls back to centralized safe controls when the driver call raises, preserves core fault payload fields such as `reason`, wraps unknown driver startup as `ConfigError`, and still records a fault if the same latest session stops after the driver safety method has already run.
  - Ran `./.venv/bin/python -m pytest`: 62 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S7:
  - Added `src/coffee_roaster_mcp/exports.py` with a deterministic snapshot export path for the current in-process session state.
  - `export_roast_log` now writes `roast.jsonl`, `roast.csv`, and `summary.json` under the session log directory and reports `ready: true`.
  - `get_roast_state` now includes minimal timestamp-derived metrics for roast elapsed seconds, development time seconds, and development percent when enough events exist.
  - Updated the stdio MCP smoke flow to prove one process can start a mock roast, mark beans added, inject first crack, drop beans, return state, and export readable files without hardware or model download.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py tests/test_package.py`: 50 passed.
  - Ran `./.venv/bin/python -m pytest`: 64 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed after applying `ruff format`.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E2-S8:
  - Added `pytest-cov` as a declared dev dependency and configured branch-aware coverage for `coffee_roaster_mcp`.
  - Updated pull-request CI so the `Checks` job runs pytest with terminal, JSON, and HTML coverage outputs.
  - Added `.github/scripts/write_coverage_summary.py` to convert `coverage.json` into a readable GitHub Actions Markdown summary with total coverage, line counts, a progress bar, lowest-covered source files, and artifact guidance.
  - CI uploads `html-coverage-report` for file-by-file drill-down without adding an external hosted coverage service.
  - Updated `README.md` with local coverage commands and how to read the GitHub Actions summary and artifact.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`: 65 passed, total coverage 77%.
  - Ran `./.venv/bin/python .github/scripts/write_coverage_summary.py coverage.json`: passed and produced the expected Markdown summary.
  - Ran `./.venv/bin/python -m pytest`: 65 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed after applying `ruff format`.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S1:
  - Replaced the E2 safety-only driver shape with a typed `RoasterDriver` protocol covering connection lifecycle, state reads, heat and fan control, bean drop, cooling control, emergency stop, and capabilities.
  - Added capability models for control ranges, supported actions, sensor units, and command-streaming requirements, plus a normalized `RoasterState` return type.
  - Extended `MockRoasterDriver` to satisfy the full contract while preserving the existing emergency-stop event payload and fail-closed heat `0`, fan `100`, cooling `on` behavior.
  - Kept normal MCP heat, fan, drop, and cooling semantics on the existing one-session store boundary; only the server's configured driver type and factory moved to the broader contract.
  - Checked the old `coffee-roasting` prototype as a behavioral reference and kept drum motor behavior internal to future Hottop driver implementation rather than adding it to the public E3-S1 contract.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 11 passed.
  - Ran `./.venv/bin/python -m pytest`: 74 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S2:
  - Extended `MockRoasterDriver` with deterministic fixed-step telemetry for normalized bean and environment temperatures.
  - Mock `read_state` advances one sample at a time and records `sample_index` plus the telemetry model in raw vendor diagnostics.
  - Mock heat, fan, drop, cooling, stop cooling, and emergency stop keep their E3-S1 control semantics; command methods return the current state without advancing telemetry.
  - Checked the old `coffee-roasting` mock roaster as a behavioral reference for heat/fan/cooling temperature effects and bean-temperature lag, but kept this implementation simpler and contract-focused.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 19 passed.
  - Ran `./.venv/bin/python -m pytest`: 82 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed after applying `ruff format`.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S3:
  - Hardened `RoasterState` with construction-time validation for normalized driver-boundary state.
  - State validation now rejects empty driver ids, non-boolean connection and cooling flags, non-finite or non-numeric Celsius temperatures, invalid heat and fan percentages, and nested or non-string-key raw vendor diagnostics.
  - Kept the existing `RoasterDriver` contract and mock-driver control behavior unchanged; this story only tightened normalized state model invariants.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 34 passed.
  - Ran `./.venv/bin/python -m pytest`: 97 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S4:
  - Added `HottopRoasterDriver` for `hottop_kn8828b_2k_plus` with lazy pyserial transport creation, command-loop thread startup, lifecycle state reporting, idempotent disconnect, thread join, serial close, and runtime reference cleanup.
  - Added `pyserial>=3.5` as a declared runtime dependency because the Hottop lifecycle now owns serial transport creation.
  - Kept packet construction, status parsing, and Hottop heat/fan/drop/cooling command behavior out of scope for later Epic 3 stories.
  - Checked the old `coffee-roasting` Hottop prototype as a behavioral reference for connect, command-loop startup, disconnect, thread join, and serial close cleanup, but kept this implementation test-injectable and scoped to lifecycle only.
  - Review hardening corrected Hottop capability flags for not-yet-implemented controls, threaded configured port/baudrate/command interval through `build_server_context`, removed the host-specific default port, guaranteed serial close on join timeout, blocked reconnect until a previous command loop is fully stopped, moved slow serial open out from under `_state_lock`, and replaced timing-based loop tests with event-based synchronization.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py tests/test_package.py`: 57 passed.
  - Ran `./.venv/bin/python -m pytest`: 107 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S5:
  - Extended the Hottop command loop so each cadence tick obtains the current command frame and writes it to the serial transport when a frame is available.
  - Kept hardware-safe defaults by returning no command frame until `E3-S6` implements and tests the Hottop packet format, so this story validates scheduling without sending unverified bytes.
  - Added raw diagnostic counters for command-loop iterations, frame polls, send attempts, successful writes, last write size, and write errors.
  - Added deterministic tests for repeated injected-frame streaming, no-frame safe default behavior, write-failure diagnostics, disconnect race protection, and clean disconnect behavior.
  - Review hardening tightened the serial `write` contract to return bytes written, separated frame-poll diagnostics from actual send attempts, coordinated disconnect with the write path so no command frame is written after stop has been requested, serializes normal serial close with the write path without waiting behind blocked writes, records partial serial writes as errors instead of successful full-frame writes, configures a dedicated cadence-derived serial write timeout, resets stale write-size diagnostics on exceptions, and fails closed when a command-loop hook raises.
  - Checked the old `coffee-roasting` Hottop prototype as a behavioral reference for the 0.3s continuous-command requirement, while leaving 36-byte packet construction and parsing to `E3-S6`.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 52 passed.
  - Ran `./.venv/bin/python -m pytest`: 116 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S6:
  - Added Hottop packet primitives for 36-byte command construction, checksum calculation, checksum validation, exact status-packet parsing, and serial-buffer scanning for the first valid status packet.
  - Command packets preserve the direct-serial behavioral reference layout: `A5 96 B0 A0 01 01 24` header fields, heat at byte `10`, roast fan and main fan on the Hottop `0-10` scale at bytes `11` and `12`, solenoid/drum/cooling bits at bytes `16` through `18`, and checksum at byte `35`.
  - Status packet parsing validates length, `A5 96` prefix, rejects command-header echoes, and validates checksum before extracting raw Celsius environment temperature from bytes `23-24` and raw Celsius bean temperature from bytes `25-26`; buffer scanning skips leading noise, invalid checksum candidates, and echoed command packets.
  - Review hardening replaced Python banker's rounding in fan-scale mapping with explicit half-up integer scaling and added boundary tests for `x5` percentages.
  - Kept command-loop safe defaults unchanged; E3-S6 does not enable heat, fan, drop, cooling, stop-cooling, or emergency-stop hardware commands.
  - Checked the old `coffee-roasting` Hottop prototype as a behavioral reference for packet layout and status temperature offsets, but kept the new implementation isolated and unit-testable.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 81 passed.
  - Ran `./.venv/bin/python -m pytest`: 145 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S7:
  - Replaced the Hottop driver's unsupported control stubs with driver-owned command state for heat, roast fan, main fan, solenoid, drum motor, and cooling motor.
  - The default connected command loop now streams verified E3-S6 Hottop command packets from safe-zero state instead of sending no frame; injected frame providers remain supported for scheduler tests.
  - Hottop heat turns on the drum when heat is nonzero, fan controls the main-fan packet byte, drop forces heat off, drum off, solenoid open, cooling on, and main fan high, stop-cooling clears cooling, solenoid, and main fan, and emergency stop forces heat off, drum off, solenoid closed, cooling on, and main fan high.
  - Commands are safe to call before `connect()` because they only mutate driver state; serial writes happen only through the connected command loop.
  - Added mocked-serial tests proving safe-zero packet streaming, heat/fan packet state, compound drop/cooling packet state, and emergency-stop packet state.
  - Checked the old `coffee-roasting` Hottop prototype as a behavioral reference for compound drop/cooling behavior and command-state packet fields, but kept this implementation inside the existing driver abstraction.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 84 passed.
  - Ran `./.venv/bin/python -m pytest`: 148 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E3-S8:
  - Added Hottop status-packet temperature normalization for configured `celsius`, `fahrenheit`, and `auto` modes while keeping `RoasterState` temperatures Celsius-only at the driver boundary.
  - The Hottop command loop now reads available serial status bytes after command writes, records raw temperature diagnostics, and updates latest normalized temperatures only after plausible packet values arrive.
  - Startup zero and implausible readings are ignored by returning `None` temperatures and incrementing diagnostics instead of publishing misleading normalized telemetry.
  - `auto` mode prefers plausible Celsius readings to preserve the old prototype's direct-Celsius behavior, then falls back to Fahrenheit conversion when Celsius values are implausible.
  - Threaded configured `roaster.temperature_unit` from MCP server context into the Hottop driver factory without changing one-session store or MCP tool semantics.
  - Checked the old `coffee-roasting` Hottop prototype as a behavioral reference for status packet offsets and big-endian temperature extraction, but kept this implementation in the new driver boundary.
  - Review hardening preserved unread status-buffer bytes across burst and partial serial reads, processed multiple valid status packets from one read, and renamed status-packet test helper fields from Celsius-specific names to raw-temperature names.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 92 passed.
  - Ran `./.venv/bin/python -m pytest`: 156 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed after applying `ruff format`.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
