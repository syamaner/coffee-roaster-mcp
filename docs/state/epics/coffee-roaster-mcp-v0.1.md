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
- Active story: `E7-S2`
- Current target: Package install smoke validation
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
- `E7-S1` keeps broad mock-safe validation on the public stdio MCP tool path:
  a default-config server uses the mock driver, first-crack mode remains
  disabled, auto-T0 remains disabled, and exported JSONL, CSV, and
  `summary.json` outputs are verified from the completed mock roast.
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
- `E3-S9` is a guarded hardware verification spike, not a driver redesign. The validation path now uses a dedicated `coffee-roaster-mcp hottop-validate` command that records JSON evidence from the Hottop driver boundary, keeps irreversible drop and emergency-stop steps opt-in, and leaves the current MCP one-session store semantics unchanged until a later story wires normal MCP control tools to live driver commands deliberately.
- `E3-S9` completed the guarded Hottop integration verification spike at the driver boundary. The non-destructive run passed first, then the full connected-Hottop run passed on `/dev/cu.usbserial-DN016OJ3` with `100%` heat and `100%` fan checks, drop, cooling stop, and emergency stop included. The validation evidence supports treating the Hottop driver boundary as hardware-ready while preserving the current MCP one-session store semantics until normal MCP control tools are deliberately wired to live driver commands.
- A follow-up 60-second connected-Hottop stability test held fan at `10%`, heat at `40%` for 30 seconds, then heat at `100%` for 30 seconds. Command streaming stayed continuous with no command-loop or status-read errors. A plain heat/fan zero stop leaves the drum command state on after prior heat, so operational stop procedures should use the explicit emergency-stop or drop/cooling command path when drum-off is required.
- The old `coffee-roasting` prototype was checked as a behavioral reference for E3-S1. It confirmed the need to model Hottop command streaming, temperature-unit normalization, compound drop/cooling behavior, and cleanup through driver lifecycle methods. Drum control remains an internal driver concern for now because E3-S1 and issue #23 do not require a public drum command.
- ONNX INT8 is the default real model backend.
- ONNX FP32 is supported by config.
- The `coffee-first-crack-detection` repo remains the source of truth for training, ONNX export, Hugging Face sync, model cards, and dataset cards.
- This repo consumes released Hugging Face artifacts only.
- `E4-S1` adds only a narrow Hugging Face Hub artifact resolver. It resolves repository-relative released files from the configured first-crack repo and revision, uses `huggingface_hub` as a declared runtime dependency, and leaves precision-specific ONNX selection, local offline directories, artifact validation, detector startup, and session-timeline integration to later Epic 4 stories.
- `E4-S2` resolves the configured first-crack ONNX model for default `int8` precision by selecting `onnx/int8/model_quantized.onnx` through the E4-S1 Hugging Face artifact resolver. FP32 selection, local offline directories, artifact validation, detector startup, audio capture, and session-timeline integration remain later Epic 4 work.
- `E4-S3` resolves the configured first-crack ONNX model for `fp32` precision by selecting `onnx/fp32/model.onnx` through the E4-S1/E4-S2 resolver boundary. Local offline directories, artifact validation, detector startup, audio capture, and session-timeline integration remain later Epic 4 work.
- `E4-S4` resolves configured first-crack artifacts from `first_crack.local_model_dir` before any Hugging Face Hub download. The local path uses the same repository-relative artifact names as the released Hub layout, fails clearly when the target local file is missing, and leaves broader detector artifact validation, detector startup, audio capture, and session-timeline integration to later Epic 4 work.
- `E4-S5` validates the required first-crack detector artifact set through the existing resolver boundary before audio detection begins. The validation resolves the configured ONNX model plus `onnx/int8/preprocessor_config.json` or `onnx/fp32/preprocessor_config.json`, depending on precision, and keeps detector startup, audio capture, artifact content validation, and session-timeline integration out of scope.
- `E4-S6` adds an injectable audio capture pipeline that builds its source from `AudioConfig`, reads on a background worker, frames complete one-second mono detector windows at the configured sample rate, and hands windows to a bounded non-blocking queue for the future detector adapter. Live audio backend selection, detector adapter behavior, model inference, and session-timeline integration remain later work.
- `E4-S7` adds a narrow detector adapter boundary. Injected detector backends
  process E4-S6 `AudioWindow` instances and confirmed outputs become
  first-crack event candidates with monotonic timestamp, configured precision,
  revision, resolved artifact filenames, repository id, source window sequence,
  and optional confidence. The adapter does not start audio capture, perform
  ONNX inference by itself, or write to the authoritative session timeline;
  E4-S9 owns timeline integration.
- `E4-S8` is inserted after the detector adapter story to add concrete microphone and WAV audio input adapters behind the E4-S6 `AudioInput` boundary. This keeps Linux/Raspberry Pi microphone behavior and recorded-session replay explicit before first-crack events are wired into the session timeline in `E4-S9`.
- `E4-S8` adds configured concrete audio sources behind the existing E4-S6
  `AudioInput` boundary. `audio.source: microphone` opens a lazy
  PortAudio-backed `sounddevice` raw input stream with configured device and
  sample rate, keeping platform-specific macOS/Linux/Raspberry Pi behavior
  behind the adapter. Leaving `audio.input_device` as `null` uses the system
  default input. Operators can pin a specific microphone with a
  PortAudio-resolvable device name or platform identifier; on Linux/Raspberry Pi
  `arecord -l` / `arecord -L` should be used during manual setup, and
  `plughw:...` identifiers are often more forgiving than raw `hw:...` devices.
  `audio.source: wav` reads PCM WAV files with stdlib decoding, requires the WAV
  sample rate to match `audio.sample_rate`, converts multi-channel files to
  mono, and returns the same mono float sample contract as live microphone
  capture.
- `E4-S9` integrates confirmed detector output with the authoritative session
  timeline through an explicit helper that keeps mutation behind
  `RoastSessionStore`. The integration is gated to `first_crack.mode: audio`,
  writes the first-crack event at the detector-provided monotonic timestamp with
  detector metadata payload, accepts adapter-inferred default timestamps that
  land slightly ahead of the integration clock within the active detector-window
  tolerance while still rejecting explicit future detector timestamps, ignores
  confirmed detector output before beans are added, ignores detector output once
  the session leaves active `roasting`, leaves disabled and manual modes
  disconnected from detector writes, and allows automatic detection even when
  manual override is disabled.
- `E4-S10` closes Epic 4 with targeted test hardening before the next epic.
  Direct in-process MCP tool tests now cover the registered FastMCP tool bodies
  for the current mock-safe session/control surface, including manual
  first-crack behavior, audio-mode bootstrap reporting, error propagation, and
  export through the public tool. Export tests prove automatic first-crack
  detector metadata is preserved in current JSONL and CSV event exports, while
  `summary.json` remains limited to first-crack timestamps and metrics until
  Epic 5 finalizes schemas. Coverage now has a stable `90%` package floor, with
  local branch-aware coverage at `91.73%`. Normal CI remains mock-safe with no
  microphone, Hottop hardware, model download, or network requirement.
- Epic 4.1 is inserted before Epic 5 because the installed Claude-local MCP
  operational path is not complete yet. E4.1-S1 wires MCP heat/fan/drop/cooling
  tools to the configured `RoasterDriver` boundary while preserving the default
  mock path, one-session store semantics, fail-closed emergency behavior, and
  no-live-hardware CI. Current first-crack components resolve
  released artifacts, capture audio windows, run the released ONNX detector
  adapter, and write confirmed first crack to the session timeline through a
  session-owned runtime. Automatic T0 remains a later Epic 4.1 story. Epic 4.1
  makes the operational MCP flow explicit: Claude should be able to start a
  roast, adjust the configured roaster, read current device/session state, and
  know whether first crack has happened before Epic 5 adds richer telemetry
  metrics and final log schemas.
- `E4.1-S1` keeps driver commands outside the store lock while ensuring invalid
  drop/cooling phase calls fail before the driver boundary is touched. Driver
  command failures surface as MCP tool errors before session mutation, while
  emergency stop continues to record a fail-closed fault event even if the
  driver safety call fails. `drop_beans` records both `beans_dropped` and
  `cooling_started` when the driver reports cooling active, which is the normal
  Hottop compound drop/cooling path and the mock-safe default path.
- `E4.1-S1` review hardening reserves session startup before driver `connect()`
  so concurrent starts cannot both reach the configured driver. Reserved
  `stop_cooling` completion trusts the driver's returned cooling state and does
  not mark the session complete if cooling remains active. Stale non-emergency
  command fail-closed handling is scoped to the reservation's session id so a
  previous session's late command cannot emergency-stop a newer active session
  without that session owning the fault.
- `E4.1-S2` expands `get_roast_state` into the operational MCP state read for
  Claude/operator decisions. The tool now reads the configured driver through
  the existing `RoasterDriver.read_state()` boundary and returns connected
  status, driver id, bean/environment temperatures when available, heat/fan
  levels, cooling state, and flat safe raw diagnostics. The same response now
  includes authoritative UTC and monotonic event timestamps for beans added,
  first crack, bean drop, cooling start, cooling stop, and faults, plus a
  structured first-crack status derived from current config and the session
  timeline. Driver state-read failures surface as clear MCP tool errors and do
  not mutate session history.
- `mark_beans_added` and `mark_first_crack` remain exposed as explicit
  override tools. The primary runtime path should be internal auto-T0 detection
  when enabled and internal first-crack detector confirmation in audio mode.
  `drop_beans` is the normal agent/operator command that should trigger the
  roaster drop/cooling behavior and record the relevant session events;
  `start_cooling` remains an advanced recovery/manual tool, not the normal
  Claude roast flow.
- `E4.1-S6` is added so the automatic T0 runtime path is explicitly owned
  before the operational epic closes. The explicit `mark_beans_added` override
  remains available, but a fully agent-driven roast should not depend on that
  override as the primary T0 path when auto-T0 is enabled. T0 is beans added:
  the runtime should track the max preheat/charge bean temperature before T0,
  then record `beans_added` when current bean temperature drops from that max by
  the configured threshold. The pre-drop max is the charge temperature.
- `E4.1-S3` adds the released-artifact ONNX first-crack detector backend
  without starting any session-owned detector loop. `first_crack.mode: audio`
  can now resolve the configured precision-specific Hugging Face artifacts,
  load `onnx/{precision}/preprocessor_config.json` through
  `transformers.ASTFeatureExtractor`, create an ONNX Runtime CPU session for the
  resolved ONNX model using configured thread limits, and adapt model logits
  into existing detector outputs with first-crack confidence. Tests use fake
  artifact paths, fake feature extraction, and fake ONNX sessions so normal CI
  remains mock-safe and requires no model download, microphone, Hottop hardware,
  or network.
- `E4.1-S4` adds the session-owned first-crack runtime. In
  `first_crack.mode: audio`, `start_roast_session` prepares the configured
  audio capture pipeline and released-artifact ONNX detector adapter without
  requiring CI to use real microphone input, real model files, Hottop hardware,
  or network. Detector processing is activated only after authoritative T0 puts
  the active session into `roasting`; queued windows are processed through the
  existing detector adapter and `RoastSessionStore` integration path, confirmed
  output records first crack exactly once, and capture/detector/artifact
  failures are reflected through MCP first-crack status as `faulted` or
  `unavailable` without crashing normal session control. The runtime stops on
  confirmed first crack, explicit manual first-crack override, drop, cooling
  completion, emergency stop, and process shutdown. Disabled and manual modes do
  not start audio or detector runtime.
- `E4.1-S5` adds operational MCP readiness coverage and docs before Epic 5.
  The public stdio MCP test path now covers the mock-safe Claude/operator flow:
  start a roast, set heat and fan, use explicit override tools when needed,
  drop beans through the normal drop/cooling command, stop cooling, read current
  device/session state, understand first-crack status, and export snapshot logs.
  The same coverage asserts `get_roast_state` schemas for lifecycle timestamps,
  configured-device state, and first-crack status to prevent accidental tool
  shape drift. README docs now explain the operational flow, first-crack status
  meanings, explicit override semantics, and gated optional live Hottop/real
  microphone validation evidence.
- `E4.1-S6` adds the automatic T0 runtime path. Auto-T0 detection is disabled by
  default, but when `session.auto_t0_detection_enabled` is configured,
  successful `get_roast_state` driver reads process the current bean
  temperature before first-crack runtime windows. The session store tracks max
  preheat/charge bean temperature before T0, records the authoritative
  `beans_added` event when current bean temperature drops from that max by
  `session.auto_t0_drop_threshold_c`, and preserves charge temperature,
  detected bean temperature, drop, and threshold diagnostics in the event
  payload and `get_roast_state.t0_status`. `mark_beans_added` remains an
  explicit idempotent override.
- `E5-S1` implements the rolling telemetry buffer capture path owned by the
  authoritative `RoastSessionStore`. Successful operational `get_roast_state`
  polling now appends normalized samples from the configured
  `RoasterDriver.read_state()` result for the latest active session, preserving
  UTC and monotonic timestamp order, bean/environment temperatures, heat/fan
  levels, and cooling state for later Epic 5 derived metrics. Driver read
  failures still do not mutate session state, stopped or non-latest sessions do
  not receive new samples, and final log schemas/RoR/development metrics remain
  later Epic 5 work.
- `E5-S2` makes roast elapsed time explicit through
  `compute_roast_elapsed_seconds(...)`. The value is `None` before T0, then
  runs from authoritative `beans_added` monotonic time to the current session
  clock until drop, and freezes at authoritative `beans_dropped` monotonic time
  after drop. Existing `get_roast_state` and snapshot summary metrics now use
  this helper through `compute_roast_metrics(...)`. Development time/percent,
  60-second deltas, RoR, append-only telemetry writers, and final log schemas
  remain later Epic 5 work.
- `E5-S3` makes development time and percent explicit through
  `compute_development_time_seconds(...)` and
  `compute_development_percent(...)`. Development time is `None` before first
  crack, runs from authoritative `first_crack_detected` monotonic time to the
  current session clock until drop, and freezes at authoritative
  `beans_dropped` monotonic time after drop. Development percent uses the E5-S2
  `compute_roast_elapsed_seconds(...)` helper as the denominator:
  `development_time_seconds / roast_elapsed_seconds * 100`. Existing
  `get_roast_state` and snapshot summary metrics use these helpers through
  `compute_roast_metrics(...)`. 60-second deltas, RoR, append-only telemetry
  writers, and final log schemas remain later Epic 5 work.
- `E5-S4` makes 60-second bean and environment temperature deltas explicit
  through `compute_bean_temp_delta_60s_c(...)` and
  `compute_env_temp_delta_60s_c(...)`. The helpers use the E5-S1 rolling
  telemetry buffer, anchor the inclusive 60-second window at the latest
  retained telemetry sample, skip missing sensor values per sensor, and return
  latest minus oldest retained temperature in that window. Existing
  `get_roast_state` and snapshot summary metric surfaces use these helpers
  through `compute_roast_metrics(...)`. RoR, append-only telemetry writers, and
  final log schemas remain later Epic 5 work.
- `E5-S5` makes bean and environment rate-of-rise metrics explicit through
  `compute_bean_ror_c_per_min(...)` and `compute_env_ror_c_per_min(...)`. The
  helpers use the E5-S1 rolling telemetry buffer, anchor the rolling RoR window
  at the latest retained telemetry sample, skip missing sensor values per
  sensor, normalize the latest-minus-oldest retained temperature slope to
  Celsius per minute using the actual valid sample span, and return `None`
  until the relevant sensor has at least the configured minimum sample span,
  defaulting to 10 seconds. Existing `get_roast_state` and snapshot summary
  metric surfaces use these helpers through `compute_roast_metrics(...)`.
  Append-only telemetry writers and final log schemas remain later Epic 5 work.
- `E5-S6` writes append-only runtime JSONL logs from the authoritative
  `RoastSessionStore`. Event rows are appended to `roast.jsonl` immediately
  when new timeline events are recorded, and telemetry rows are appended from
  the existing E5-S1 polling path at the configured
  `logging.sample_interval_seconds`, defaulting to 5 seconds. Snapshot export
  keeps writing CSV and `summary.json` from the current session but no longer
  overwrites an existing append-only JSONL log. Final CSV and summary schemas
  remain later Epic 5 work.
- `E5-S7` adds the planned CSV roast log export schema to snapshot
  `export_roast_log` output. `roast.csv` now includes telemetry and event rows
  with the required plan columns for timestamps, elapsed seconds, inferred
  phase, temperatures, controls, cooling state, event markers, event flags,
  development percent, RoR/delta metrics, and first-crack model metadata.
  Append-only JSONL runtime logging, existing metric helpers, the one-session
  store boundary, mock-safe MCP behavior, and `summary.json` behavior remain
  unchanged. Final `summary.json` schema work remains later Epic 5 work.
- `E5-S8` adds the planned `summary.json` session-level schema. Snapshot
  summary export now includes session timestamps, total roast seconds,
  development seconds and percent, the configured roaster driver, and
  first-crack model metadata from the authoritative first-crack event payload.
  Existing summary fields and metric helper values are preserved, and the
  append-only JSONL runtime writer, CSV schema, one-session store boundary, and
  mock-safe MCP behavior remain unchanged.
- `E5-S9` adds narrow log schema completeness tests without changing runtime
  behavior. The append-only JSONL runtime log now has exact key-set coverage for
  telemetry and event rows, CSV export remains pinned to the E5-S7 field order,
  and `summary.json` has exact top-level, nested metrics, and first-crack model
  metadata key-set coverage. Existing metric helpers, append-only JSONL writes,
  CSV/summary values, session/runtime boundaries, and mock-safe behavior remain
  unchanged.
- `E5-S10` added the autonomous telemetry sampler before distribution.
  Starting a roast session now starts a session-owned background sampler that
  polls the configured driver at `logging.sample_interval_seconds`, defaulting
  to 5 seconds. Sampled state is appended through the existing
  `RoastSessionStore.record_active_telemetry_sample(...)` path, so rolling
  metrics and append-only JSONL telemetry rows advance even when no MCP client
  polls `get_roast_state`. MCP state reads still refresh telemetry
  opportunistically. The sampler waits for the configured interval before its
  first autonomous sample so explicit tool reads remain deterministic, stops
  when the owning session is no longer active or when the MCP process shuts
  down, and fails closed with a diagnosable fault event if the configured driver
  read fails. The implementation preserves the one-session boundary,
  configured-driver control wiring, mock-safe CI, automatic T0 processing,
  session-owned first-crack runtime processing, append-only JSONL logging, CSV
  and summary export schemas, and Hottop/model/audio validation boundaries.
- `E6-S1` completes PyPI package metadata for `coffee-roaster-mcp` without
  adding publishing or MCP Registry behavior. Project metadata now includes
  maintainer metadata, a fuller keyword set, PyPI classifiers for console
  usage, Apache licensing, OS independence, hardware/utilities topics, and
  typed-package status, plus a documentation project URL. `RoastPilot` remains
  the human-facing title in the package summary. The distribution includes a
  `py.typed` marker, and tests inspect installed package metadata plus the
  console script entry point.
- `E6-S2` adds the MCP Registry README verification string only. The README now
  includes `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`, and a
  focused README test pins that the verification string appears exactly once.
  This story does not add `server.json`, PyPI publishing, MCP Registry
  publishing, release workflow behavior, live hardware validation, model
  training/export/sync, real microphone validation, or broad release
  validation.
- `E6-S3` adds the root MCP Registry `server.json` only. Registry metadata now
  declares the server name `io.github.syamaner/coffee-roaster-mcp`, display
  title `RoastPilot`, PyPI package `coffee-roaster-mcp`, package runtime hint
  `uvx`, stdio transport, repository metadata, and the current MCP schema URI.
  Focused coverage validates the metadata shape against the relevant MCP
  Registry schema constraints, including URI format validation, and pins the
  E6-S3 acceptance fields. Version alignment automation, PyPI publishing, MCP
  Registry publishing, release workflow behavior, live hardware validation,
  model training/export/sync, real microphone validation, and broad release
  validation remain later stories.
- `E6-S4` adds the version alignment check only. Focused `server.json` coverage
  now compares both top-level `server.json.version` and the PyPI package entry
  version against `coffee_roaster_mcp.__version__`, so registry metadata and
  package metadata cannot drift unnoticed. PyPI publishing, MCP Registry
  publishing, release workflow behavior, live hardware validation, model
  training/export/sync, real microphone validation, and broad release validation
  remain later stories.
- `E6-S5` adds the guarded release workflow and operator prerequisite runbook.
  `.github/workflows/release.yml` runs checks, validates tag/version alignment,
  builds distribution artifacts, supports manual dry run without uploading,
  publishes to PyPI through Trusted Publishing after `release` environment
  approval, and publishes MCP Registry metadata with `mcp-publisher` GitHub
  OIDC only after PyPI succeeds. Review hardening pins GitHub Actions refs to
  commit SHAs, disables checkout credential persistence, and pins the
  `mcp-publisher` v1.7.9 Linux amd64 asset with SHA-256 verification before
  execution. `docs/release.md` documents PyPI ownership, 2FA/recovery codes,
  Trusted Publishing setup for `release.yml`/`release`/
  `publish-pypi`, protected `v*` tag rules, TestPyPI status, and the
  `PYPI_API_TOKEN` fallback secret name. Live publishing was not executed by
  E6-S5.
- `E6-S6` completes the MCP Registry publishing verification spike without
  executing a live PyPI release or live Registry publish. `server.json`
  validated against the downloaded official `2025-12-11` Registry schema and
  against the preview Registry API through `mcp-publisher validate server.json`.
  The pinned `mcp-publisher` v1.7.9 Linux amd64 workflow asset checksum matched
  the expected SHA-256, GitHub OIDC authentication was confirmed to require the
  GitHub Actions OIDC environment, and the release workflow now validates
  `server.json` before authenticating and publishing. `docs/release.md`
  documents the PyPI verification marker, non-destructive validation commands,
  live publish stop point, prerequisites, expected outcome, and preview
  Registry risk. The remaining destructive step is the tag-triggered live
  release path after production PyPI publication succeeds.
- `E6-S7` documents install and hardware setup without executing any live
  publishing, hardware validation, model training/export/sync, or real
  microphone validation. `docs/install-and-hardware-setup.md` now covers the
  mock install path, Hottop configuration, Hugging Face model configuration,
  offline model directory layout, and log output paths. README and
  `docs/release.md` cross-reference that setup guide for release and operator
  readiness.
- `E6-S8` executed the controlled live release for `coffee-roaster-mcp`
  `0.1.0`. The tag-triggered GitHub Actions release run published to production
  PyPI, then published MCP Registry metadata after PyPI succeeded. Production
  PyPI exposes the matching version and exact `mcp-name` marker, a clean PyPI
  install smoke passed, `mcp-publisher validate server.json` passed, and
  Registry search returns `io.github.syamaner/coffee-roaster-mcp` pointing to
  PyPI package `coffee-roaster-mcp` with stdio transport. `docs/release.md`
  records commands, links, outcomes, risks, and retry/rollback notes.
- Configuration loads from mock-safe defaults, optional `coffee-roaster-mcp.yaml`, and environment overrides. YAML file support uses PyYAML as a declared runtime dependency.
- Agent rules and repo-local workflows are now part of the scaffold. `AGENTS.md`, `.claude/skills/code-quality`, `.claude/skills/mcp-dev`, `.claude/skills/mock-roast`, `.claude/skills/hottop-validation`, `.claude/skills/release-registry`, and Copilot review instructions should be kept current as story workflow changes.
- The old `coffee-roasting` POC is a behavior reference for Epic 2, especially `roaster_control/mcp_server.py`, `roaster_control/server.py`, `roaster_control/session_manager.py`, and `roaster_control/roast_tracker.py`. It is not a template for carrying forward the old split MCP, Auth0, SSE, or `n8n` architecture.

## Current Risks

- MCP Registry publishing is preview; the live `0.1.0` listing is published,
  but preview Registry data can still reset or change before general
  availability.
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

- [x] `E3-S9` Run Hottop integration verification spike.
  - Done when packet parsing, temp units, command cadence, drop, cooling, and cleanup have manual validation notes.

### Epic Acceptance Criteria

- Mock driver passes contract tests.
- Hottop packet tests cover checksum, invalid packets, and temp conversion.
- Command loop starts and stops cleanly.
- Manual Hottop checklist passes before hardware-ready release label.

## Epic 4: First-Crack Detection With HF Models

Goal: consume released Hugging Face model artifacts and feed first-crack events into the single roast timeline.

### Stories

- [x] `E4-S1` Add Hugging Face artifact resolver.
  - Done when model files can be resolved from `syamaner/coffee-first-crack-detection` using configured revision.

- [x] `E4-S2` Load INT8 ONNX by default.
  - Done when `onnx/int8/model_quantized.onnx` is selected for `precision: int8`.

- [x] `E4-S3` Load FP32 ONNX by config.
  - Done when `onnx/fp32/model.onnx` is selected for `precision: fp32`.

- [x] `E4-S4` Support local offline model directory.
  - Done when `local_model_dir` works without Hugging Face network access.

- [x] `E4-S5` Validate required detector artifacts before detection starts.
  - Done when missing ONNX model or feature extractor files fail clearly before audio detection begins.

- [x] `E4-S6` Add audio capture pipeline.
  - Done when configured audio input can feed detector windows without blocking roaster telemetry.

- [x] `E4-S7` Add detector adapter.
  - Done when detector output maps to a confirmed first-crack event with timestamp, precision, revision, and confidence when available.

- [x] `E4-S8` Add microphone and WAV audio input adapters.
  - Done when configured microphone and recorded WAV inputs can feed the detector window pipeline through the same audio source boundary.

- [x] `E4-S9` Integrate first crack with session timeline.
  - Done when mocked detector output creates exactly one `first_crack_detected` event.

- [x] `E4-S10` Harden first-crack and MCP coverage before next epic.
  - Done when automated tests cover the assembled first-crack path, MCP-facing behavior, current export surfaces, duplicate/no-confirmation/error cases, disabled/manual modes, missing artifacts, and materially reduce `mcp_server.py`, `exports.py`, and Epic 4 coverage gaps.
  - Manual real-microphone validation may be added only behind an explicit opt-in gate and must be skipped by default unless a microphone is configured and ready.

### Epic Acceptance Criteria

- INT8 resolver selects `onnx/int8/model_quantized.onnx`.
- FP32 resolver selects `onnx/fp32/model.onnx`.
- Offline local directory works without HF network access.
- Configured microphone and WAV audio sources can feed the detector window pipeline.
- Mocked detector output creates exactly one `first_crack_detected` event.
- Epic 4 closes with targeted automated coverage for first-crack integration,
  MCP-facing behavior, current export surfaces, and mock-safe failure modes.
- Real microphone validation is optional, explicitly gated, and never required
  for normal CI.

## Epic 4.1: Operational MCP Runtime

Goal: make the locally installed MCP server operational for Claude before the
metrics/logging epic. Claude should be able to start a roast, adjust the
configured roaster, read current device and session state, and know whether
first crack has happened through MCP tools.

### Stories

- [x] `E4.1-S1` Wire MCP roast-control tools to configured driver.
  - Done when `start_roast_session`, `set_heat`, `set_fan`, `drop_beans`,
    `start_cooling`, `stop_cooling`, and `emergency_stop` call the configured
    `RoasterDriver` boundary where appropriate while preserving the mock
    default, one-session store semantics, fail-closed safety behavior, and
    no-live-hardware CI. `drop_beans` is the normal MCP path for drop and
    cooling transition; `start_cooling` remains available only as an explicit
    advanced/manual recovery control.

- [x] `E4.1-S2` Expose current roaster device state through MCP.
  - Done when MCP state output includes current driver state needed for
    operational decisions: connected status, bean/environment temperatures when
    available, heat/fan levels, cooling state, driver id, and safe raw
    diagnostics, plus authoritative event timestamps for beans added, first
    crack, bean drop, cooling start, and cooling stop, without implementing Epic
    5 rolling metrics.

- [x] `E4.1-S3` Add released-artifact ONNX first-crack detector backend.
  - Done when `first_crack.mode: audio` can construct a detector backend from
    the resolved Hugging Face ONNX model and feature-extractor config using the
    existing released-artifact resolver boundary, with mock-safe tests and no
    training, export, or Hugging Face sync behavior.

- [x] `E4.1-S4` Start first-crack detection runtime with roast sessions.
  - Done when audio mode starts/stops the configured audio pipeline and detector
    runtime with the roast session, records confirmed first crack exactly once,
    exposes disabled/manual/pending/detected/faulted/unavailable status through
    MCP, and keeps detector/audio failures from crashing normal session control.
    `mark_first_crack` remains only the explicit manual override path when
    configuration allows it.

- [x] `E4.1-S5` Add MCP operational readiness tests and docs.
  - Done when automated MCP tests cover the local Claude-installed operational
    flow on the mock-safe path, MCP response schemas for device state and
    first-crack status are asserted, override-tool semantics for
    `mark_beans_added` and `mark_first_crack` are documented, `drop_beans` is
    documented as the normal drop/cooling command, and optional live
    Hottop/real microphone validation docs remain explicitly gated.

- [x] `E4.1-S6` Add automatic T0 runtime path.
  - Done when `session.auto_t0_detection_enabled` can record the authoritative
    `beans_added` event internally through `RoastSessionStore` without using
    `mark_beans_added` as the primary path, tracks max preheat/charge bean
    temperature from `RoasterDriver.read_state()` before T0, records T0 at the
    first reading where current bean temperature drops from that max by the
    configured threshold, preserves the pre-drop max as charge temperature in
    diagnostics, handles gradual drops by comparing against the tracked max
    rather than only the previous reading, remains disabled by default,
    preserves `mark_beans_added` as an explicit idempotent override, rejects
    invalid phases and duplicates, exposes the resulting T0 timestamps and
    charge-temperature diagnostics through `get_roast_state`, and keeps normal
    CI mock-safe.

### Epic Acceptance Criteria

- Claude can start a roast through MCP.
- Claude can adjust configured roaster controls through MCP while the default
  mock path remains hardware-free.
- Claude can read current configured-device state and authoritative session
  state through MCP.
- Claude can determine whether first crack is disabled, manual, pending,
  detected, faulted, or unavailable due to configuration/artifact/audio errors.
- Claude can read event timestamps for beans added, first crack, bean drop,
  cooling started, and cooling stopped from `get_roast_state`.
- Automatic T0 and automatic first-crack detection are internal runtime paths;
  exposed mark tools are explicit overrides.
- Automatic T0 means bean-charge detection from a configured bean-temperature
  drop threshold against max preheat/charge temperature, not temperature
  recovery after the drop.
- In audio mode, first-crack runtime uses released Hugging Face ONNX artifacts
  consumed by this repo; model training, ONNX export, and Hugging Face sync stay
  in `coffee-first-crack-detection`.
- `drop_beans` is the normal operational command for drop and cooling
  transition; `start_cooling` is an advanced/manual recovery path.
- Normal CI requires no Hottop hardware, microphone, model download, or network.
- Model training, ONNX export, Hugging Face sync, final telemetry metrics, and
  final log schemas remain out of scope for this epic.

## Epic 5: Roast Metrics And Log Export

Goal: compute roast metrics from one session clock and export durable logs.

### Stories

- [x] `E5-S1` Implement rolling telemetry buffer.
  - Done when bean/env samples are retained for rolling metric calculations.

- [x] `E5-S2` Compute elapsed roast time.
  - Done when `roast_elapsed_seconds` is computed from `beans_added_at` to now or drop.

- [x] `E5-S3` Compute development time and percent.
  - Done when development time starts at first crack and development percent is `development_time_seconds / roast_elapsed_seconds * 100`.

- [x] `E5-S4` Compute 60s bean/env deltas.
  - Done when latest minus oldest sample in rolling 60s window is returned for bean and environment temps.

- [x] `E5-S5` Compute bean/env RoR.
  - Done when RoR is normalized to C/min and returns null before 10 seconds of samples.

- [x] `E5-S6` Write append-only JSONL roast log.
  - Done when telemetry rows are written at the configured interval and event rows are written immediately.

- [x] `E5-S7` Export CSV roast log.
  - Done when CSV includes all required columns from the plan.

- [x] `E5-S8` Export `summary.json`.
  - Done when summary includes session timestamps, total roast seconds, development metrics, roaster driver, and first-crack model metadata.

- [x] `E5-S9` Add log schema tests.
  - Done when JSONL, CSV, and summary schema completeness is covered by tests.

- [x] `E5-S10` Add autonomous telemetry sampler.
  - Done when `start_roast_session` starts a session-owned telemetry sampler
    that polls the configured roaster driver at `logging.sample_interval_seconds`
    without requiring `get_roast_state` polling; the default configured interval
    is 5 seconds.
  - Done when MCP tool calls may refresh telemetry opportunistically without
    being the only path that advances telemetry.
  - Done when sampled driver state is appended through the existing
    `RoastSessionStore` telemetry path so rolling metrics and append-only JSONL
    telemetry rows advance on the sampler cadence.
  - Done when the sampler stops cleanly on drop/cooling completion, emergency
    stop, session stop, driver read failure, and MCP process shutdown without
    leaking background workers.
  - Done when driver read failures surface as diagnosable fault/unavailable
    state without unsafe hardware commands, and normal mock-safe CI requires no
    Hottop hardware, microphone, model download, or network.
  - Required tests: mock-safe sampler lifecycle tests, no-client-poll telemetry
    accumulation/logging test, sampler shutdown tests, driver-read failure test,
    and MCP smoke coverage proving metrics/logs advance without repeated
    `get_roast_state` calls.

### Epic Acceptance Criteria

- RoR is null before 10 seconds of samples.
- RoR and deltas are correct for regular and irregular sample intervals.
- JSONL, CSV, and summary include required fields.
- Event rows are written immediately, not only on the sampled telemetry loop.
- Telemetry rows and rolling metrics advance at the configured sampler cadence
  even when an MCP client does not poll `get_roast_state`.
- Epic 5 is complete through E5-S10. Runtime telemetry now advances both from
  the autonomous sampler cadence and from opportunistic MCP state reads.

## Epic 6: Distribution And MCP Registry Publishing

Goal: make RoastPilot installable and discoverable through PyPI and the MCP Registry.

### Stories

- [x] `E6-S1` Add PyPI package metadata.
  - Done when package metadata is complete for `coffee-roaster-mcp`.

- [x] `E6-S2` Add README MCP verification string.
  - Done when README includes `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.

- [x] `E6-S3` Add `server.json`.
  - Done when registry metadata uses name `io.github.syamaner/coffee-roaster-mcp`, title `RoastPilot`, package `coffee-roaster-mcp`, and stdio transport.

- [x] `E6-S4` Add version alignment check.
  - Done when package version and `server.json.version` cannot drift unnoticed.

- [x] `E6-S5` Add release workflow.
  - Done when CI can build, test, publish to PyPI, and publish registry
    metadata after tag release.
  - Operator prerequisites must be documented before workflow implementation:
    - PyPI account exists for the release owner.
    - TestPyPI account exists if the workflow supports TestPyPI rehearsal.
    - PyPI project name `coffee-roaster-mcp` ownership is confirmed or reserved.
    - PyPI two-factor authentication and recovery codes are configured.
    - PyPI Trusted Publishing is configured for this GitHub repository,
      release workflow filename, release environment, and tag-triggered
      publishing job.
    - A token-based fallback is documented only if Trusted Publishing is not
      usable, including the exact GitHub secret names and rotation notes.
    - Required GitHub release environment approvals and protected-tag rules are
      documented before live publishing is enabled.
    - Workflow dry run proves build/test/package steps without uploading to
      production PyPI.
  - Implemented in `.github/workflows/release.yml` and documented in
    `docs/release.md`. Live PyPI and MCP Registry publishing remain guarded by
    tag trigger plus the `release` GitHub environment; this story did not run a
    live release.

- [x] `E6-S6` Run MCP Registry publishing verification spike.
  - Done when `server.json`, PyPI verification, and `mcp-publisher` flow are documented and tested before v0.1 release.
  - Verified against the official `2025-12-11` schema and preview Registry API
    with `mcp-publisher validate server.json`. The live publish command remains
    guarded behind the release workflow because it requires production PyPI
    publication and Registry mutation.

- [x] `E6-S7` Document install and hardware setup.
  - Done when docs cover mock install, Hottop config, Hugging Face model config, offline model path, and log output paths.

- [x] `E6-S8` Execute live PyPI and MCP Registry publish.
  - GitHub issue: #135
  - Done when production PyPI contains the matching `coffee-roaster-mcp`
    version, the published PyPI long description includes
    `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`, the package
    installs from PyPI with CLI smoke checks passing, `mcp-publisher validate
    server.json` passes against the live package and preview Registry API, the
    guarded release workflow publishes Registry metadata after PyPI succeeds,
    Registry search returns `io.github.syamaner/coffee-roaster-mcp`, and the
    listing points to PyPI package `coffee-roaster-mcp` with stdio transport.
  - Live publish outcome, links, commands, risks, and any rollback or retry
    notes must be recorded in `docs/release.md`, this active epic, registry
    state, and a session summary.
  - Do not run the live Registry publish until production PyPI shows the
    matching package version and its long description includes the exact
    `mcp-name` marker.

### Epic Acceptance Criteria

- Package installs from PyPI.
- `server.json` validates against current MCP schema.
- Registry publish flow is documented and tested before v0.1 release.
- Registry listing points to the PyPI package and stdio transport.

## Epic 7: End-To-End Validation And Release Readiness

Goal: prove the package works from install through mock roast, MCP client calls, hardware validation, and release.

### Stories

- [x] `E7-S1` Test full mock roast through MCP tools.
  - Done when a mock roast works from session start to exported logs.

- [ ] `E7-S2` Test package install smoke flow.
  - Done when a built wheel can be installed and `coffee-roaster-mcp --help` works.

- [ ] `E7-S3` Test Warp MCP client connection.
  - Done when Warp can configure, start, discover, and call the local stdio
    MCP server on the mock-safe path, confirm `mock` / `disabled` defaults,
    complete a full mock roast through public MCP tools, and verify exported
    JSONL, CSV, and summary outputs.

- [ ] `E7-S4` Run Warp manual Hottop MCP control validation.
  - Done when Warp can connect to the Hottop-configured RoastPilot MCP server
    and the operator manually approves each hardware-affecting tool call for
    connect, telemetry, heat, fan, drop, cooling, stop-cooling, emergency stop,
    and exported-log review. No autonomous hardware-control decisions are in
    scope.

- [ ] `E7-S5` Produce v0.1 release checklist.
  - Done when release steps cover tests, package build, version alignment, HF revision pin, PyPI publish, registry publish, GitHub release, and hardware-ready labeling.

- [ ] `E7-S6` Run end-to-end agent roast validation with HF ONNX audio path.
  - Done when a real MCP client or agent can install/connect to the package and
    run a full roast flow using public MCP tools, configured Hottop hardware,
    released Hugging Face ONNX first-crack artifacts, and real microphone/audio
    input; validation evidence records lifecycle timestamps, first-crack
    status/metadata, roaster device state, Epic 5 metrics/stat fields,
    exported logs, configuration, artifact revision, hardware/audio setup, and
    operator interventions.

### Epic Acceptance Criteria

- Full mock roast works from install to exported logs.
- Warp can discover and call tools through the local stdio MCP server.
- Warp manual Hottop hardware-control results are recorded with explicit
  operator approvals.
- A real MCP client or agent can complete an end-to-end roast validation using
  configured hardware, real audio, and released Hugging Face ONNX first-crack
  artifacts, with correct state, stats, and exported logs recorded as evidence.
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
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S8:
  - Verified PR #136 was merged and issue #55 was closed before starting.
  - Synced `main` to merge commit `276ec81056e05ed8a863c5e5bb9bf28e45308383`
    and created branch `feature/135-live-pypi-and-mcp-registry-publish`.
  - Confirmed no local or remote `v0.1.0` tag existed before release.
  - Confirmed production PyPI returned `Not Found` before release and Registry
    search returned no listing for `io.github.syamaner/coffee-roaster-mcp`.
  - Ran `./.venv/bin/python -m pytest`: 356 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/python -m build`: built
    `coffee_roaster_mcp-0.1.0.tar.gz` and
    `coffee_roaster_mcp-0.1.0-py3-none-any.whl`.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`:
    `coffee-roaster-mcp 0.1.0`.
  - Ran `/tmp/mcp-publisher validate server.json`: `server.json is valid`.
  - Pushed tag `v0.1.0`; GitHub reported a protected-tag creation rule was
    bypassed for this tag.
  - Confirmed GitHub Actions release run `26367482422` completed successfully:
    `Validate Release Metadata`, `Checks`, `Build Package`, `Publish PyPI`,
    and `Publish MCP Registry` all succeeded.
  - Confirmed PyPI project `https://pypi.org/project/coffee-roaster-mcp/` and
    release `https://pypi.org/project/coffee-roaster-mcp/0.1.0/` expose version
    `0.1.0` with the exact `mcp-name` marker in the long description.
  - Confirmed PyPI artifact SHA-256 hashes:
    `coffee_roaster_mcp-0.1.0-py3-none-any.whl`
    `d8cd00257bf30ddf89b98eff07d2b3d93369e3b441d9ef60f99b825e45436f33` and
    `coffee_roaster_mcp-0.1.0.tar.gz`
    `8c6ea87f4ccbae4654ac6df2c1588b86f79bdf1e54e19ec301aa7ef87b283e0c`.
  - Ran a clean production-PyPI install smoke in
    `/tmp/coffee-roaster-mcp-pypi-smoke`: `coffee-roaster-mcp==0.1.0`
    installed successfully, `coffee-roaster-mcp --help` passed,
    `coffee-roaster-mcp --version` returned `coffee-roaster-mcp 0.1.0`, and
    the mock-safe default config smoke returned `mock disabled int8`.
  - Confirmed Registry search returns `io.github.syamaner/coffee-roaster-mcp`
    with PyPI package `coffee-roaster-mcp`, version `0.1.0`, runtime hint
    `uvx`, and stdio transport.
  - Did not run hardware validation, model training/export/sync, or real
    microphone validation.
- Validation run for E7-S1:
  - Verified PR #137 was merged and issue #135 was closed before starting.
  - Synced `main` to merge commit `5052ab29ec142cfe6e28bfb3e5bf17d529d006c3`
    and created branch `feature/56-full-mock-roast-mcp-tools`.
  - Tightened the stdio MCP mock-roast flow in `tests/test_package.py` so it
    verifies the default runtime config stays on roaster driver `mock`,
    first-crack mode `disabled`, and automatic T0 disabled before driving the
    public MCP tools.
  - Verified the mock roast from `start_roast_session` through heat/fan,
    manual beans-added and first-crack override, drop, cooling stop, state read,
    and `export_roast_log` using stdio MCP calls.
  - Verified exported `roast.jsonl`, `roast.csv`, and `summary.json` outputs
    from the MCP export result, including event order, CSV lifecycle phases,
    mock roaster metadata, empty model metadata for disabled first crack, and
    populated roast/development metrics.
  - Kept hardware validation, model training/export/sync, real microphone
    validation, and live release publishing out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_package.py::test_stdio_server_supports_basic_mock_roast_tool_flow`:
    1 passed.
  - Ran `./.venv/bin/python -m pytest tests/test_package.py`: 19 passed.
  - Ran `./.venv/bin/python -m pytest`: 356 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: 30 files already
    formatted.
  - Ran `./.venv/bin/python -m pyright`: 0 errors, 0 warnings,
    0 informations.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`:
    `coffee-roaster-mcp 0.1.0`.

- Validation run for E6-S4:
  - Added focused version alignment coverage in `tests/test_server_json.py`.
  - Pinned top-level `server.json.version` to `coffee_roaster_mcp.__version__`.
  - Pinned the PyPI package entry version in `server.json` to
    `coffee_roaster_mcp.__version__`.
  - Kept PyPI publishing, MCP Registry publishing, release workflow behavior,
    live hardware validation, model training/export/sync, real microphone
    validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_server_json.py`: 4 passed.
  - Ran `./.venv/bin/python -m pytest`: 348 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
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
  - Review hardening preserved unread status-buffer bytes across burst and partial serial reads, processed multiple valid status packets from one read, renamed status-packet test helper fields from Celsius-specific names to raw-temperature names, clears the resolved raw unit diagnostic when the latest raw packet is ignored as implausible, makes Hottop temperature-unit validation deterministic for non-string and mixed-case inputs, releases the command write lock before status reads, caps per-loop status reads, and parses status buffers outside the state lock before publishing results.
  - Ran `./.venv/bin/python -m pytest tests/test_drivers.py`: 97 passed.
  - Ran `./.venv/bin/python -m pytest`: 161 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed after applying `ruff format`.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation prep for E3-S9:
  - Added `coffee-roaster-mcp hottop-validate` as a guarded hardware validation harness for the Hottop driver boundary.
  - The command requires `--i-understand-this-controls-hardware`, validates that config uses `hottop_kn8828b_2k_plus` with an explicit serial port, writes optional JSON evidence, and keeps `--include-drop` plus `--include-emergency-stop` opt-in.
  - Updated `.claude/skills/hottop-validation` and `README.md` with the non-destructive and full validation commands, safety gates, pass/fail criteria, hard abort conditions, troubleshooting paths, source artifact references, report template, and evidence expectations.
  - Preserved the current MCP/session boundary: driver-level validation does not imply MCP heat, fan, drop, or cooling tools are live-hardware control surfaces yet.
  - Added fake-driver tests for acknowledgement gating, Hottop config gating, skipped destructive steps, evidence output, and full validation report status.
  - Manual Hottop hardware execution was still pending before E3-S9 could be marked complete.
  - Ran `./.venv/bin/python -m pytest tests/test_hottop_validation.py tests/test_package.py`: 18 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Non-destructive connected-Hottop validation for E3-S9:
  - Used config source `/tmp/coffee-roaster-mcp-hottop.yaml` with driver `hottop_kn8828b_2k_plus`, port `/dev/cu.usbserial-DN016OJ3`, baudrate `115200`, `temperature_unit: auto`, and command interval `0.3`.
  - Ran `./.venv/bin/coffee-roaster-mcp hottop-validate --config /tmp/coffee-roaster-mcp-hottop.yaml --output /tmp/hottop-e3-s9-non-destructive.json --i-understand-this-controls-hardware`.
  - Evidence file: `/tmp/hottop-e3-s9-non-destructive.json`; SHA-256 `eafc565eb11b8db4bc9b813894714f67732b4d57867a970fc2a7dd64a40571e0`.
  - Connected successfully and streamed command frames at the configured cadence. By the final cooling-stop step the run recorded `49` command-loop iterations, `49` send attempts, `49` successful writes, last write size `36`, and `0` command-loop errors.
  - Stable telemetry passed. The run recorded plausible room-temperature telemetry at `23.0 C` bean and `23.0 C` environment, `151` status packets by the final cooling-stop step, `0` ignored temperature packets, `0` status-read errors, and resolved `auto` mode to `celsius`.
  - Heat validation passed at conservative `10%` heat, then heat-off returned heat to `0%`.
  - Fan validation passed at `30%` main fan.
  - Cooling validation passed: cooling start set cooling on and fan high; cooling stop cleared cooling and fan.
  - Drop and emergency stop were intentionally skipped because the first hardware pass was non-destructive. This blocks any hardware-ready release label until a supervised full run validates those steps.
- Full connected-Hottop validation for E3-S9:
  - Used the same config source `/tmp/coffee-roaster-mcp-hottop.yaml` with driver `hottop_kn8828b_2k_plus`, port `/dev/cu.usbserial-DN016OJ3`, baudrate `115200`, `temperature_unit: auto`, and command interval `0.3`.
  - Ran `./.venv/bin/coffee-roaster-mcp hottop-validate --config /tmp/coffee-roaster-mcp-hottop.yaml --output /tmp/hottop-e3-s9-full.json --i-understand-this-controls-hardware --heat-percent 100 --fan-percent 100 --include-drop --include-emergency-stop`.
  - Evidence file: `/tmp/hottop-e3-s9-full.json`; SHA-256 `3756dc9a3481d3859f0767b10940ae481cbef5a4e3544357bd76121d5e0a22a1`.
  - Connected successfully and streamed command frames at the configured cadence. By the emergency-stop step the run recorded `62` command-loop iterations, `62` send attempts, `62` successful writes, last write size `36`, and `0` command-loop errors.
  - Stable telemetry passed. The run recorded plausible room-temperature telemetry at `23.0 C` bean and `23.0 C` environment, `191` status packets by the emergency-stop step, `0` ignored temperature packets, `0` status-read errors, and resolved `auto` mode to `celsius`.
  - Heat validation passed at `100%` heat, then heat-off returned heat to `0%`.
  - Fan validation passed at `100%` main fan.
  - Drop validation passed: heat stayed `0%`, drum motor off, solenoid open, cooling on, and fan high.
  - Cooling-stop validation passed: cooling off, solenoid closed, fan `0%`, heat `0%`, and drum motor off.
  - Emergency-stop validation passed: heat `0%`, drum motor off, solenoid closed, cooling on, and fan high.
  - The validation report set `hardware_ready_release_label_allowed` to `true` for the Hottop driver boundary.
- 60-second connected-Hottop stability test for E3-S9:
  - Ran a supervised live stability test on `/dev/cu.usbserial-DN016OJ3` with fan held at `10%`, heat at `40%` for 30 seconds, then heat at `100%` for 30 seconds.
  - Evidence file: `/tmp/hottop-e3-s9-60s-stability.json`; SHA-256 `2887c42c301ce08f01b353b40c8ed8ab96137e21baef7f734708c10539e4a4cf`.
  - Command streaming stayed continuous for the full run. At the 60-second sample the driver reported `197` command-loop iterations, `197` send attempts, `197` successful writes, last write size `36`, and `0` command-loop errors.
  - Status reads stayed clean. At the 60-second sample the driver reported `607` status packets, `0` status-read errors, and resolved `auto` mode to `celsius`.
  - Telemetry remained plausible during the short test: bean and environment readings stayed at `23.0 C` during the one-minute hold, then read `24.0 C` in the final safe-zero sample.
  - After setting heat and fan to `0`, the command state still reported `drum_motor_on: true` because `set_heat(0)` does not clear a drum command that was enabled by prior nonzero heat. A follow-up safe-stop sequence used emergency stop, then cooling stop and zero heat/fan, and ended with heat `0%`, fan `0%`, cooling off, solenoid closed, drum motor off, and `0` command-loop/status-read errors.
- PR review hardening for E3-S9:
  - Codex review found that aborted validation runs could lose the most important partial evidence, and that stable-telemetry status and evidence could come from different concurrent state snapshots.
  - Copilot review found additional hardware-safety and auditability gaps: telemetry `needs_review` did not abort before actuation, heat/fan CLI values were validated too late, disconnect failures could mask earlier failures, output paths were not preflighted before hardware commands, drop validation was partially masked by earlier cooling, readiness ignored raw diagnostics, non-destructive cleanup could leave the drum command on, reusable runbook text still assumed E3-S9 was active, registry status text conflicted, and `/tmp` evidence lacked durable checksums.
  - Follow-up Codex review found that hidden safe cleanup violated the emergency-stop opt-in contract, that control-step readiness needed fresh command-write progress rather than stale write counters, that heat/fan steps also needed to verify the resulting driver state matched the requested target, and that CLI/evidence automation needed clearer failure behavior.
  - Hardened the validation harness to validate heat/fan/durations before connecting, defer evidence output creation until config and required Hottop driver/port preflight passes, require finite durations, capture one stable-telemetry snapshot, abort before controls when telemetry does not pass, write partial failure reports before re-raising, record disconnect failures, drop before cooling-stop validation in full runs, honor the skipped emergency-stop contract without hidden emergency-stop commands, require per-step command-write progress plus requested heat/fan target state, return non-zero from the CLI when the report is not hardware-ready, and base readiness on required passed steps plus no failed steps.
  - Added SHA-256 checksums for all three hardware evidence files instead of committing raw JSON evidence.
  - Updated the session summary with review quality, overlap, and response details.
  - Ran `./.venv/bin/python -m pytest`: 170 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S1:
  - Added `src/coffee_roaster_mcp/artifacts.py` with a small Hugging Face Hub artifact resolver for released first-crack model files.
  - The resolver accepts a repository-relative filename, uses `FirstCrackConfig.repo_id` and `FirstCrackConfig.revision`, returns the local Hub cache path, and wraps download failures with repository, revision, and artifact context.
  - Added `huggingface_hub>=0.23,<1` as a declared runtime dependency while keeping the Hub import lazy so mocked resolver tests do not require network access or a real download.
  - Added mocked resolver tests covering the default `syamaner/coffee-first-crack-detection` repository, configured revision handling, configured repository handling, invalid artifact names, and contextual failure messages.
  - Kept model training, ONNX export, Hugging Face sync, precision-specific model selection, local offline directory handling, artifact validation, detector startup, and MCP/session integration out of scope.
  - Ran `./.venv/bin/python -m pytest`: 184 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S2:
  - Added `resolve_first_crack_onnx_model(...)` as the narrow ONNX model selection entry point on top of the E4-S1 Hugging Face artifact resolver.
  - The selector resolves `onnx/int8/model_quantized.onnx` for configured `int8` precision, including the default `FirstCrackConfig()` precision.
  - FP32 selection remains deferred to E4-S3, and model training, ONNX export, Hugging Face sync, detector startup, audio capture, local offline directories, artifact validation, and MCP/session integration remain out of scope.
  - Added mocked resolver tests for default INT8 selection, explicit INT8 selection with revision propagation, and the deferred FP32 boundary.
  - Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: 14 passed.
  - Ran `./.venv/bin/python -m pytest`: 189 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S3:
  - Extended `resolve_first_crack_onnx_model(...)` to select `onnx/fp32/model.onnx` when `FirstCrackConfig.precision` is `fp32`.
  - Preserved the default `int8` selection of `onnx/int8/model_quantized.onnx` and kept repository, revision, filename validation, and download behavior delegated to the E4-S1 Hugging Face artifact resolver.
  - Kept model training, ONNX export, Hugging Face sync, detector startup, audio capture, local offline directories, artifact validation, and MCP/session integration out of scope.
  - Added mocked resolver coverage for configured FP32 selection with revision propagation and unsupported precision domain-error handling.
  - Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: 15 passed.
  - Ran `./.venv/bin/python -m pytest`: 190 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S4:
  - Updated the first-crack artifact resolver to prefer `FirstCrackConfig.local_model_dir` when configured, resolving the same repository-relative artifact filenames from local storage before any Hugging Face Hub download.
  - Missing local artifacts now raise `ArtifactResolutionError` with the repository-relative filename, configured local directory, and computed local path.
  - Preserved existing Hugging Face Hub behavior when `local_model_dir` is unset and kept model training, ONNX export, Hugging Face sync, detector startup, audio capture, broad artifact validation, and MCP/session integration out of scope.
  - Added offline resolver tests for default INT8 local model selection, FP32 local model selection, and missing local model failures before downloader use.
  - Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: 18 passed.
- Validation run for E4-S5:
  - Added `ResolvedDetectorArtifacts` and `resolve_first_crack_detector_artifacts(...)` as the narrow pre-audio detector artifact validation entry point.
  - The detector artifact resolver validates the configured ONNX model plus the precision-specific feature extractor preprocessor config: `onnx/int8/preprocessor_config.json` for INT8 and `onnx/fp32/preprocessor_config.json` for FP32.
  - Missing ONNX models fail before feature extractor resolution, and missing feature extractor configs fail with repository, revision, and filename context while preserving local offline directory behavior.
  - Kept model training, ONNX export, Hugging Face sync, detector startup beyond validation prerequisites, audio capture, local directory sync behavior, artifact content validation, and MCP/session integration out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_artifacts.py`: 24 passed.
- Validation run for E4-S6:
  - Added `src/coffee_roaster_mcp/audio.py` with an injectable audio capture pipeline for future first-crack detector use.
  - The pipeline builds validated capture settings from `AudioConfig`, passes the configured input device and sample rate into an injected input factory, reads samples on a daemon worker thread, and emits complete one-second mono `AudioWindow` detector windows.
  - Detector-window handoff uses a bounded queue and `put_nowait`; when the detector side is full, windows are dropped and counted instead of blocking capture or roaster telemetry work elsewhere in the process.
  - Kept live microphone backend selection, detector adapter behavior, ONNX inference, first-crack session timeline integration, model training, ONNX export, and Hugging Face sync out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_audio.py`: 8 passed.
  - Ran `./.venv/bin/python -m pytest`: 208 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- PR review fix for E4-S6:
  - Reset audio capture run-scoped state on each `AudioCapturePipeline.start()` so partial samples, queued windows, sequence numbers, counters, and prior errors cannot leak across a stopped and restarted pipeline instance.
  - Added a restart regression test proving leftover partial samples from the first run are not mixed into the first detector window of the next run, and sequence numbers restart from zero.
  - Kept the detector window queue stable across restarts by draining and reusing the existing queue instead of replacing it, so blocking detector consumers are not stranded on an old queue object.
  - Added a blocking-consumer restart regression test proving a consumer waiting before restart receives the next window after capture restarts.
  - Ran `./.venv/bin/python -m pytest tests/test_audio.py`: 10 passed.
  - Ran `./.venv/bin/python -m pytest`: 210 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Planning update after E4-S6:
  - Inserted `E4-S8` / issue `#97` for concrete microphone and recorded WAV audio input adapters after the detector adapter story and before session timeline integration.
  - Renamed the previous timeline integration issue `#39` to `E4-S9` so Raspberry Pi/Linux microphone behavior and recorded-session replay are captured explicitly before detector results are wired into the authoritative session timeline.
  - Updated Epic 4 acceptance criteria to include configured microphone and WAV sources feeding the detector window pipeline.
- Validation run for E4-S7:
  - Added `src/coffee_roaster_mcp/detector.py` with the narrow first-crack detector adapter boundary.
  - The adapter accepts E4-S6 `AudioWindow` instances and an injected backend, ignores unconfirmed detector outputs, and maps confirmed outputs to `first_crack_detected` event candidates with monotonic timestamp, configured precision, revision, repository id, resolved artifact filenames, source window sequence number, and optional confidence.
  - The adapter falls back to the audio-window end timestamp when the backend does not provide a detection timestamp and validates detector confidence plus finite timestamps.
  - Kept ONNX runtime inference, model training, ONNX export, Hugging Face sync, concrete microphone/WAV adapters, local directory sync behavior, MCP tool behavior, and authoritative session timeline writes out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_detector.py`: 8 passed.
  - Ran `./.venv/bin/python -m pytest`: 218 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S8:
  - Added explicit audio source selection with `audio.source: microphone|wav`,
    `audio.wav_path`, and environment overrides for source, sample rate, and WAV
    path while preserving `first_crack.mode: disabled` as the mock-safe default.
  - Added `WavAudioInput` behind the E4-S6 `AudioInput` boundary. It reads PCM
    WAV files with stdlib `wave`, supports 8/16/24/32-bit PCM sample widths,
    converts multi-channel files to mono, emits normalized finite float samples,
    and fails clearly when the WAV sample rate differs from configured
    `audio.sample_rate`.
  - Added `MicrophoneAudioInput` behind the same boundary. It opens a lazy
    PortAudio-backed `sounddevice.RawInputStream` with configured device and
    sample rate, reads mono float32 samples, and reports backend read/open errors
    plus overflow as `AudioCaptureError`.
  - Added `build_configured_audio_input(...)` as the source-selection factory
    used by `build_audio_capture_pipeline(...)` when no test factory is injected.
  - Tests cover config/source selection, generated WAV replay, stereo-to-mono WAV
    conversion, WAV sample-rate mismatch, mocked microphone backend selection,
    microphone overflow handling, and pipeline compatibility proving microphone
    and WAV sources feed identical detector-window contracts.
  - Kept detector inference, ONNX export, model training, Hugging Face sync,
    local directory sync behavior, first-crack session timeline integration,
    broad coverage hardening, and live Hottop control changes out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_audio.py tests/test_config.py`: 30 passed.
  - Ran `./.venv/bin/python -m pytest`: 226 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S9:
  - Added an explicit detector-to-session integration helper in
    `src/coffee_roaster_mcp/detector.py` that processes one `AudioWindow` with
    the existing detector adapter and records confirmed output as an
    authoritative `first_crack_detected` event through `RoastSessionStore`.
  - The integration is gated to `first_crack.mode: audio`; disabled and manual
    modes do not call the detector adapter or mutate the timeline.
  - Confirmed detector payloads include detector source, detected monotonic
    timestamp, precision, revision, repository id, resolved ONNX artifact,
    feature-extractor artifact, source window sequence number, and optional
    confidence.
  - Repeated detector confirmations and detector confirmations after a manual
    first-crack event are ignored after the session leaves active `roasting`, so
    late detector output does not append duplicate timeline rows.
  - PR review hardening moved automatic first-crack recording onto a dedicated
    `RoastSessionStore.record_first_crack_detection_snapshot(...)` path so the
    authoritative event timestamp and downstream development metrics use the
    detector-provided monotonic timestamp rather than the later integration time.
  - A follow-up review fix allows adapter-inferred default window-end timestamps
    that are slightly ahead of the integration clock, bounded by the detector
    window duration, so backends without explicit timestamps do not fail the
    automatic path.
  - Another review fix ignores confirmed detector output before beans are added
    so early false positives cannot bubble a lifecycle exception and break the
    detection loop before roasting starts.
  - A final review fix restricts future-timestamp tolerance to adapter-inferred
    window-end timestamps only; explicit future timestamps from detector
    backends still fail fast so backend clock or timestamp bugs are not silently
    clamped.
  - The latest review fix also ignores detector output after the session leaves
    active `roasting`, covering late confirmations after first crack, drop,
    cooling, completion, fault, or stop without relying on store-level lifecycle
    exceptions.
  - Automatic detector integration remains independent of manual override
    permission, so `allow_manual_override: false` only disables the manual MCP
    override path.
  - Kept model training, ONNX export, Hugging Face sync, local directory sync,
    detector startup, audio capture startup, broad coverage hardening, and live
    Hottop control changes out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py`: 13 passed.
  - Ran `./.venv/bin/python -m pytest`: 234 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Final review-fix validation ran
    `./.venv/bin/python -m pytest tests/test_first_crack_integration.py tests/test_detector.py tests/test_session.py`:
    55 passed.
  - Final full validation ran `./.venv/bin/python -m pytest`: 238 passed,
    `./.venv/bin/python -m ruff check .`: passed,
    `./.venv/bin/python -m ruff format --check .`: passed, and
    `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4-S10:
  - Added direct in-process MCP tool coverage for the registered FastMCP tool
    bodies so local coverage measures the same public tool logic already
    exercised by stdio smoke tests.
  - Covered current MCP-facing behavior for server info, runtime config,
    session start/state, heat/fan controls, beans added, manual first crack,
    drop, cooling, export, audio-mode bootstrap safety reporting, missing active
    session errors, disabled manual override errors, and unknown session lookup.
  - Added export coverage proving automatic first-crack detector metadata is
    preserved in JSONL and CSV event exports while `summary.json` keeps the
    current timestamp and metrics surface until Epic 5 finalizes log schemas.
  - Reviewed the current MCP completion boundary: mock/session device control
    and manual first-crack MCP behavior are covered, live Hottop command wiring
    remains a deliberate future MCP integration story, and automatic
    first-crack detector startup is not yet a runtime MCP loop.
  - Added a stable `90%` package coverage floor in `pyproject.toml`; local
    branch-aware coverage is `91.73%`.
  - Ran `./.venv/bin/python -m pytest tests/test_exports.py tests/test_mcp_server.py tests/test_first_crack_integration.py tests/test_package.py`:
    27 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    241 passed, required coverage `90.0%` reached, total coverage `91.73%`.
- Validation run for E4.1-S1:
  - Wired `start_roast_session` to call the configured driver `connect()`
    before creating a session, preserving the default mock path and requiring
    explicit Hottop configuration for live hardware.
  - Wired MCP `set_heat`, `set_fan`, `drop_beans`, `start_cooling`, and
    `stop_cooling` to the configured `RoasterDriver` methods and mirrored the
    returned normalized heat, fan, and cooling state into the authoritative
    session snapshot.
  - Updated the mock driver drop path to match the normal operational
    drop/cooling transition: heat off, fan `100%`, cooling on.
  - Kept `drop_beans` as the normal agent/operator path for drop plus cooling
    transition; `start_cooling` remains available as an explicit advanced or
    recovery command and is idempotent after drop-triggered cooling has already
    been recorded.
  - Added pre-driver phase guards so invalid drop, cooling-start, and
    cooling-stop calls fail before any driver command is sent.
  - Added driver-double MCP coverage proving configured-driver calls happen,
    driver command failures do not mutate session state, and connect failures
    do not create a roast session.
  - PR review hardening added store-owned non-emergency driver command
    reservations. Driver I/O now runs outside the store lock, but completion
    must still own the active reservation before session state is updated.
  - Repeated `drop_beans` and `start_cooling` calls return the existing
    singleton event without resending driver commands. Emergency stop cancels
    pending non-emergency reservations before running the fail-closed driver
    safety call, and stale completed commands surface a lifecycle error instead
    of mutating the session. Stale-command emergency stop is reapplied only when
    no newer active session has replaced the command's owning session.
  - Added review hardening for remaining race and state-reporting edges:
    session startup is reserved before driver `connect()`, reserved
    `stop_cooling` uses the driver's returned cooling state before completing
    the session, and stale command fail-closed handling skips global emergency
    stop when a newer active session has replaced the reservation's session.
  - Added MCP regression coverage for concurrent start reservation,
    cooling-stop driver-state mismatch, and stale previous-session command
    completion after a newer session starts.
  - Kept automatic first-crack detector startup, ONNX detector runtime,
    auto-T0 detection, rolling telemetry metrics, final log schemas, model
    training, ONNX export, Hugging Face sync, real microphone validation, and
    broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py`: 11 passed.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py`: 38 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    250 passed, required coverage `90.0%` reached, total coverage `90.39%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4.1-S2:
  - Expanded `get_roast_state` with a current `device_state` snapshot from the
    configured `RoasterDriver.read_state()` boundary: driver id, connected
    status, bean/environment temperatures when available, heat/fan levels,
    cooling state, and flat safe raw diagnostics.
  - Added authoritative monotonic event timestamp fields alongside existing UTC
    fields for beans added, first crack, bean drop, cooling start, cooling stop,
    and faults.
  - Added structured first-crack status fields for operator decisions. Current
    MCP output reports disabled, manual, pending, detected, or faulted from
    configuration and the authoritative session timeline; the status enum leaves
    room for unavailable runtime failures when E4.1-S4 owns detector startup.
  - PR review hardening changed manual first-crack mode with
    `allow_manual_override: false` to report `status="unavailable"` instead of
    telling clients to wait for a rejected `mark_first_crack` override.
  - Driver state-read failures now surface as clear `get_roast_state` tool
    errors and do not mutate session history.
  - Kept rolling telemetry retention, RoR/60-second deltas, final log schemas,
    released-artifact ONNX runtime construction, detector startup, auto-T0
    detection, training/export/sync behavior, real microphone validation, and
    live Hottop validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py`: 14 passed.
  - Ran `./.venv/bin/python -m pytest tests/test_package.py`: 15 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    253 passed, required coverage `90.0%` reached, total coverage `90.56%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4.1-S3:
  - Added `OnnxFirstCrackDetectorBackend` and
    `build_released_onnx_first_crack_detector_adapter(...)` so
    `first_crack.mode: audio` can resolve the configured released Hugging Face
    detector artifacts, load the precision-specific
    `preprocessor_config.json`, construct an ONNX Runtime CPU session for the
    resolved ONNX model, and feed output through the existing detector adapter
    metadata path.
  - Added lazy, clearly failing runtime dependency boundaries for
    `onnxruntime` and `transformers.ASTFeatureExtractor`; default mock
    configuration still does not import or start either dependency.
  - Added fake-backed tests for INT8/FP32 artifact resolution, ONNX session
    construction, AST feature-extractor construction, confidence parsing,
    sample-rate validation, missing/invalid preprocessor config, missing model
    inputs, empty outputs, and dependency failures without requiring model
    downloads, real ONNX files, microphone input, Hottop hardware, or network.
  - Kept session-owned detector startup, audio capture lifecycle wiring,
    automatic T0, rolling metrics, final log schemas, model training, ONNX
    export, Hugging Face sync, real microphone validation, live Hottop
    validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_detector.py tests/test_artifacts.py`:
    43 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    272 passed, required coverage `90.0%` reached, total coverage `90.45%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4.1-S4:
  - Added `FirstCrackSessionRuntime` as the session-owned coordinator for audio
    capture and detector processing. `start_roast_session` now prepares the
    runtime in `first_crack.mode: audio`, while disabled and manual modes do not
    start audio or detector runtime.
  - Runtime processing drains queued audio windows only for the owning active
    session after authoritative T0 moves the session into `roasting`, then uses
    the existing detector adapter plus `RoastSessionStore` integration helper to
    record confirmed first crack exactly once.
  - `get_roast_state.first_crack_status` now reflects runtime-level
    `pending`, `detected`, `faulted`, and `unavailable` audio-mode status,
    including artifact, audio-capture, and detector failures, without crashing
    normal session control.
  - Runtime stop is wired to confirmed first crack, explicit
    `mark_first_crack` override, `drop_beans`, `stop_cooling`,
    `emergency_stop`, and MCP process shutdown. No automatic T0 implementation,
    rolling telemetry metrics, final log schemas, model training/export/sync,
    real microphone validation, live Hottop validation, or broad release
    validation was added.
  - Added fake-backed tests for disabled/manual no-start behavior, audio-mode
    preparation, activation after beans-added, no-confirmation pending status,
    duplicate confirmation handling, artifact unavailability, capture faults,
    detector faults, and terminal runtime stop behavior.
  - Ran `./.venv/bin/python -m pytest tests/test_first_crack_runtime.py tests/test_mcp_server.py tests/test_first_crack_integration.py`:
    29 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    280 passed, required coverage `90.0%` reached, total coverage `90.15%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4.1-S5:
  - Strengthened the public stdio MCP mock-roast test to assert the operational
    Claude/operator flow through public tools: start session, set heat and fan,
    record explicit beans-added and first-crack overrides, use `drop_beans` as
    the normal drop/cooling command, stop cooling, read state, export snapshot
    logs, and start a later session after completion or fault.
  - Added schema assertions for `get_roast_state`, nested `device_state`, and
    nested `first_crack_status`, including lifecycle timestamp fields for beans
    added, first crack, bean drop, cooling started, and cooling stopped.
  - Updated README operator docs for the current MCP flow, first-crack status
    values, override tool semantics, `drop_beans` as the normal cooling
    transition, and gated optional live Hottop/real microphone validation
    evidence.
  - Kept automatic T0 implementation, rolling telemetry metrics, final log
    schemas, model training/export/sync, real microphone validation, live
    Hottop validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_package.py tests/test_mcp_server.py`:
    31 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    280 passed, required coverage `90.0%` reached, total coverage `90.15%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E4.1-S6:
  - Added `session.auto_t0_drop_threshold_c`, defaulting to `25.0`, while
    keeping `session.auto_t0_detection_enabled` disabled by default.
  - Added session-store automatic T0 processing that tracks max preheat/charge
    bean temperature before T0, requires at least one prior valid baseline
    reading, records `beans_added` when the current bean temperature drops from
    that max by the configured threshold, and stores charge temperature,
    detected bean temperature, drop, threshold, and `auto_t0` source in the
    event payload.
  - Wired `get_roast_state` to process automatic T0 only after a successful
    configured-driver `read_state()` call and before first-crack runtime window
    processing, preserving driver-read failure no-mutation behavior.
  - Added `get_roast_state.t0_status` with enabled/disabled status, pending or
    detected state, charge temperature, current drop, threshold, and detected
    bean-temperature diagnostics.
  - Preserved `mark_beans_added` as an explicit idempotent override and kept
    rolling telemetry metrics, final log schemas, model training/export/sync,
    real microphone validation, live Hottop validation, end-to-end agent roast
    validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_config.py tests/test_session.py tests/test_mcp_server.py`:
    76 passed.
  - Ran `./.venv/bin/python -m pytest tests/test_package.py`: 15 passed.
  - Ran `./.venv/bin/python -m pytest --cov=coffee_roaster_mcp --cov-report=term-missing:skip-covered --cov-report=json:coverage.json --cov-report=html:htmlcov`:
    289 passed, required coverage `90.0%` reached, total coverage `90.06%`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
- Validation run for E5-S1:
  - Added store-owned normalized telemetry capture through
    `RoastSessionStore.record_telemetry_sample(...)`, using the authoritative
    session UTC and monotonic clocks and preserving retained samples in read
    snapshots for later Epic 5 metrics.
  - Wired successful operational `get_roast_state` driver reads to append one
    telemetry sample for the latest active session while keeping driver-read
    failures, stopped sessions, and non-latest sessions from mutating the
    telemetry buffer.
  - Enforced monotonic timestamp ordering for appended telemetry samples and
    retained the existing per-session rolling buffer limit.
  - Kept RoR calculations, development percent changes, final log schemas,
    append-only telemetry log files, CSV/summary schema changes, model
    training/export/sync, real microphone validation, live Hottop validation,
    end-to-end agent roast validation, and broad release validation out of
    scope.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py tests/test_mcp_server.py`:
    69 passed.
  - Ran `./.venv/bin/python -m pytest`: 301 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S2:
  - Added `compute_roast_elapsed_seconds(...)` as the explicit helper for
    `roast_elapsed_seconds` from authoritative T0 to current session clock or
    authoritative drop time.
  - Wired `compute_roast_metrics(...)` to use the helper so existing
    `get_roast_state` and snapshot summary metric surfaces share the E5-S2
    elapsed-time contract.
  - Added elapsed-time tests for `None` before beans are added, active elapsed
    time before drop, and frozen elapsed time after drop even when the current
    clock advances.
  - Kept development time/percent behavior, 60-second deltas, RoR,
    append-only telemetry log files, final JSONL/CSV/summary schemas, model
    training/export/sync, real microphone validation, live Hottop validation,
    end-to-end agent roast validation, and broad release validation out of
    scope.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py`: 49 passed.
  - Ran `./.venv/bin/python -m pytest`: 305 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S3:
  - Added `compute_development_time_seconds(...)` as the explicit helper for
    `development_time_seconds` from authoritative first crack to current
    session clock or authoritative drop time.
  - Added `compute_development_percent(...)` as the explicit helper for
    `development_time_seconds / roast_elapsed_seconds * 100`, using the E5-S2
    `compute_roast_elapsed_seconds(...)` helper for the denominator.
  - Wired `compute_roast_metrics(...)` to use the explicit development helpers
    so existing `get_roast_state` and snapshot summary metric surfaces keep the
    same public metric fields with the E5-S3 contract pinned in code.
  - Added development metric tests for `None` before first crack, active
    development time before drop, frozen development time after drop even when
    the current clock advances, and development percent denominator behavior.
  - Kept 60-second deltas, RoR, append-only telemetry log files, final
    JSONL/CSV/summary schemas, model training/export/sync, real microphone
    validation, live Hottop validation, end-to-end agent roast validation, and
    broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py`: 53 passed.
  - Ran `./.venv/bin/python -m pytest`: 309 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S4:
  - Added `compute_bean_temp_delta_60s_c(...)` and
    `compute_env_temp_delta_60s_c(...)` as explicit helpers for 60-second
    temperature deltas from the E5-S1 rolling telemetry buffer.
  - Wired `compute_roast_metrics(...)` to expose `bean_temp_delta_60s_c` and
    `env_temp_delta_60s_c` through the existing `get_roast_state` and snapshot
    summary metric surfaces.
  - Added delta tests for regular 60-second sample intervals, irregular sample
    intervals anchored to the latest retained sample, and per-sensor missing
    temperature values.
  - Kept RoR, append-only telemetry log files, final JSONL/CSV/summary schemas,
    model training/export/sync, real microphone validation, live Hottop
    validation, end-to-end agent roast validation, and broad release validation
    out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py tests/test_package.py tests/test_exports.py`:
    72 passed.
  - Ran `./.venv/bin/python -m pytest`: 312 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S5:
  - Added `compute_bean_ror_c_per_min(...)` and
    `compute_env_ror_c_per_min(...)` as explicit helpers for bean and
    environment rate of rise from the E5-S1 rolling telemetry buffer.
  - Wired `compute_roast_metrics(...)` to expose `bean_ror_c_per_min` and
    `env_ror_c_per_min` through the existing `get_roast_state` and snapshot
    summary metric surfaces.
  - Added RoR tests for regular 60-second sample intervals, irregular sample
    spans normalized to C/min, per-sensor missing temperature values, the
    minimum 10-second sample-span guard, and configured window/minimum span
    values.
  - Kept append-only telemetry log files, final JSONL/CSV/summary schemas,
    model training/export/sync, real microphone validation, live Hottop
    validation, end-to-end agent roast validation, and broad release validation
    out of scope.
  - Ran `./.venv/bin/python -m pytest`: 318 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S6:
  - Added append-only `roast.jsonl` runtime writes behind the authoritative
    `RoastSessionStore` mutation boundary.
  - Event rows are written immediately when new session timeline events are
    recorded, including automatic first-crack and emergency fault paths.
  - Telemetry rows are written from the existing E5-S1 polling sample path at
    `logging.sample_interval_seconds`, defaulting to 5 seconds, while still
    preserving the rolling telemetry buffer for derived metrics.
  - Snapshot export still writes CSV and `summary.json`, but does not overwrite
    an existing append-only JSONL runtime log.
  - Kept final CSV schema work, final summary schema work, model
    training/export/sync, real microphone validation, live Hottop validation,
    end-to-end agent roast validation, and broad release validation out of
    scope.
  - Ran `./.venv/bin/python -m pytest`: 325 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S7:
  - Added the plan-required CSV roast log columns to snapshot `roast.csv`
    export: timestamp, elapsed seconds, phase, temperatures, controls, cooling
    state, event marker, event flags, development percent, RoR/delta metrics,
    and first-crack model metadata.
  - CSV export now writes both retained telemetry samples and session timeline
    events in monotonic order, inferring phase and event flags at each row while
    preserving the existing append-only JSONL runtime writer and `summary.json`
    behavior.
  - Added CSV schema tests for telemetry and event rows plus first-crack model
    metadata fields.
  - Kept final `summary.json` schema work, model training/export/sync, real
    microphone validation, live Hottop validation, end-to-end agent roast
    validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_exports.py`: 4 passed.
  - Ran `./.venv/bin/python -m pytest`: 328 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S8:
  - Added the planned `summary.json` session-level schema fields to snapshot
    export: `started_at_utc`, lifecycle timestamps, `total_roast_seconds`,
    development seconds/percent, `roaster_driver`, and `first_crack_model`.
  - Passed the configured roaster driver from the MCP `export_roast_log` path
    into summary export while preserving the existing direct-export mock
    default.
  - Kept append-only JSONL runtime logging, the E5-S7 CSV schema, one-session
    store ownership, existing metric helpers, mock-safe CI behavior, first-crack
    artifact/audio boundaries, Hottop validation boundary, and release
    validation scope unchanged.
  - Ran `./.venv/bin/python -m pytest tests/test_exports.py`: 10 passed.
  - Ran `./.venv/bin/python -m pytest`: 335 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S9:
  - Added exact key-set coverage for append-only JSONL runtime telemetry and
    event rows.
  - Pinned `summary.json` schema completeness for top-level fields, nested
    metrics, and first-crack model metadata fields.
  - Kept CSV schema completeness pinned to the E5-S7 field order and preserved
    append-only JSONL runtime logging, the CSV schema, the summary schema,
    one-session store ownership, existing metric helpers, mock-safe CI behavior,
    first-crack artifact/audio boundaries, Hottop validation boundary, and
    release validation scope unchanged.
  - Ran `./.venv/bin/python -m pytest tests/test_session.py tests/test_exports.py`:
    81 passed.
  - Ran `./.venv/bin/python -m pytest`: 337 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E5-S10:
  - Added a session-owned autonomous telemetry sampler to the MCP runtime.
  - `start_roast_session` starts the sampler for the new session; MCP lifespan
    shutdown stops it, and cooling completion or emergency stop stop the owning
    sampler explicitly.
  - The sampler polls the configured `RoasterDriver.read_state()` boundary at
    `logging.sample_interval_seconds`, defaulting to 5 seconds, and appends
    samples through `RoastSessionStore.record_active_telemetry_sample(...)`.
  - Successful sampler reads also run the existing automatic T0 and
    session-owned first-crack processing paths so those runtime paths no longer
    depend only on `get_roast_state` polling.
  - MCP `get_roast_state` continues to refresh telemetry opportunistically.
  - Driver read failures fail closed through the existing emergency-stop safety
    payload path, record a diagnosable fault event, stop first-crack processing,
    and stop the sampler without issuing unrelated hardware commands.
  - Added mock-safe MCP/runtime tests for no-client-poll telemetry logging,
    opportunistic state-read refresh, sampler shutdown, driver-read failure, and
    background-worker lifecycle behavior.
  - Kept append-only JSONL runtime logging, the CSV schema, the summary schema,
    the one-session store boundary, configured-driver control/state wiring,
    automatic T0 behavior, session-owned first-crack runtime behavior, Hottop
    validation boundaries, first-crack artifact/audio boundaries, and all
    E5-S1 through E5-S9 metric/log/export helpers unchanged.
  - Ran `./.venv/bin/python -m pytest tests/test_config.py tests/test_session.py tests/test_package.py`:
    108 passed.
  - Ran `./.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_package.py`:
    43 passed.
  - Ran `./.venv/bin/python -m pytest`: 341 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S1:
  - Completed PyPI package metadata for `coffee-roaster-mcp` while keeping
    PyPI publishing, MCP Registry metadata, live hardware validation, model
    training/export/sync, real microphone validation, and broad release
    validation out of scope.
  - Added maintainer metadata, package keywords, project classifiers,
    documentation URL metadata, and a `py.typed` marker for the typed package.
  - Added installed-distribution metadata coverage for package identity,
    `RoastPilot` summary text, Python requirement, author/maintainer metadata,
    keywords, classifiers, project URLs, and the console script entry point.
  - Ran `./.venv/bin/python -m pytest tests/test_package_metadata.py tests/test_package.py`:
    21 passed.
  - Ran `./.venv/bin/python -m build`: built
    `coffee_roaster_mcp-0.1.0.tar.gz` and
    `coffee_roaster_mcp-0.1.0-py3-none-any.whl`.
  - Inspected built wheel metadata and confirmed package name, `RoastPilot`
    summary, Python requirement, project URLs, and classifiers.
  - Ran `./.venv/bin/python -m pytest`: 343 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S2:
  - Added the exact MCP Registry README verification string:
    `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
  - Added focused README coverage proving the verification string appears
    exactly once.
  - Kept `server.json`, PyPI publishing, MCP Registry publishing, release
    workflow behavior, live hardware validation, model training/export/sync,
    real microphone validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_readme.py`: 1 passed.
  - Ran `./.venv/bin/python -m ruff check README.md tests/test_readme.py`:
    passed.
  - Ran `./.venv/bin/python -m ruff format --check tests/test_readme.py`:
    passed.
  - Ran `./.venv/bin/python -m pytest`: 344 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S3:
  - Added root `server.json` with MCP Registry metadata for
    `io.github.syamaner/coffee-roaster-mcp`.
  - Declared title `RoastPilot`, PyPI package `coffee-roaster-mcp`, runtime hint
    `uvx`, and stdio transport.
  - Added focused schema and acceptance coverage in `tests/test_server_json.py`.
  - Declared `jsonschema` in the dev dependency group for the schema validation
    test.
  - Kept version alignment automation, PyPI publishing, MCP Registry
    publishing, release workflow behavior, live hardware validation, model
    training/export/sync, real microphone validation, and broad release
    validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_server_json.py`: 3 passed.
  - Ran `./.venv/bin/python -m ruff check tests/test_server_json.py pyproject.toml`:
    passed.
  - Ran `./.venv/bin/python -m ruff format --check tests/test_server_json.py`:
    passed.
  - Ran `./.venv/bin/python -m pyright tests/test_server_json.py`: 0 errors.
  - Ran `./.venv/bin/python -m pytest`: 347 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S5:
  - Added `.github/workflows/release.yml` with a manual dry-run path, tag-based
    live release path, checks, release metadata validation, package build, PyPI
    Trusted Publishing, and MCP Registry publishing after PyPI succeeds.
    Review hardening pins GitHub Actions refs to commit SHAs, disables checkout
    credential persistence, and verifies the pinned `mcp-publisher` v1.7.9
    Linux amd64 asset SHA-256 before execution. Follow-up metadata-validation
    hardening gives explicit release-operator errors for missing `__version__`,
    missing or empty `server.json.packages`, and malformed first package
    entries.
  - Added `docs/release.md` documenting PyPI owner prerequisites, 2FA and
    recovery-code setup, Trusted Publishing configuration for
    `release.yml`/`release`/`publish-pypi`, protected `v*` tag rules, TestPyPI
    status, and the `PYPI_API_TOKEN` fallback secret name.
  - Added focused release workflow coverage in `tests/test_release_workflow.py`,
    including pinned action refs, checkout credential persistence, and
    `mcp-publisher` checksum verification. Follow-up coverage pins the clear
    metadata-validation failure messages.
  - Kept live PyPI upload, live MCP Registry publish, TestPyPI rehearsal,
    hardware validation, model training/export/sync, real microphone
    validation, and broad release validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_release_workflow.py`:
    7 passed.
  - Ran `./.venv/bin/python -m pytest`: 355 passed.
  - Ran `./.venv/bin/python -m build`: built
    `coffee_roaster_mcp-0.1.0.tar.gz` and
    `coffee_roaster_mcp-0.1.0-py3-none-any.whl`.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
- Validation run for E6-S6:
  - Confirmed official MCP Registry docs still mark the Registry as preview,
    use schema URI
    `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`,
    require PyPI packages to use `registryType: pypi`, and verify PyPI
    ownership through an `mcp-name: $SERVER_NAME` README marker.
  - Validated `server.json` against the downloaded `2025-12-11` JSON schema.
  - Ran `./mcp-publisher validate server.json` with pinned `mcp-publisher`
    v1.7.9: passed against the preview Registry API.
  - Verified the workflow's pinned Linux amd64 `mcp-publisher` asset SHA-256:
    `ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac`.
  - Confirmed `mcp-publisher login github-oidc` fails outside GitHub Actions
    without `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, so live auth must run from the
    release job with `id-token: write`.
  - Confirmed production PyPI currently returns `Not Found` for
    `coffee-roaster-mcp` and the Registry search API returns no current listing
    for `io.github.syamaner/coffee-roaster-mcp`; live publish remains the first
    destructive decision point after production PyPI publication.
- Validation run for E6-S7:
  - Added `docs/install-and-hardware-setup.md` with setup-focused coverage for
    mock install, `coffee-roaster-mcp.yaml`, Hottop configuration, Hugging Face
    model configuration, offline model directory layout, and log output paths.
  - Updated README to point operators at the setup guide and corrected current
    log-export wording now that append-only JSONL, CSV, and summary schemas
    exist.
  - Updated `docs/release.md` so release operators review the setup guide
    before live release and before hardware-ready labeling.
  - Added focused documentation coverage in `tests/test_readme.py` to pin the
    E6-S7 required topics and cross-references.
  - Kept live PyPI publish, live MCP Registry publish, hardware validation,
    model training/export/sync, and real microphone validation out of scope.
  - Ran `./.venv/bin/python -m pytest tests/test_readme.py tests/test_release_workflow.py`:
    9 passed.
  - Ran `./.venv/bin/python -m pytest`: 356 passed.
  - Ran `./.venv/bin/python -m ruff check .`: passed.
  - Ran `./.venv/bin/python -m ruff format --check .`: passed.
  - Ran `./.venv/bin/python -m pyright`: 0 errors.
  - Ran `./.venv/bin/coffee-roaster-mcp --help`: passed.
  - Ran `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.
