# RoastPilot Project State Registry

## Active Epic

- Epic file: `docs/state/epics/coffee-roaster-mcp-v0.1.md`
- GitHub issue index: `docs/state/github-issues.md`
- Project: RoastPilot
- Repository: `syamaner/coffee-roaster-mcp`
- Package: `coffee-roaster-mcp`
- MCP Registry name: `io.github.syamaner/coffee-roaster-mcp`
- Current phase: Bootstrap

## Working Rules

- Before starting implementation, read this registry, then the active epic, then the GitHub issue for the story.
- Each story should have acceptance criteria before code starts.
- Risky stories require a short implementation plan before code.
- Keep model training, ONNX export, and Hugging Face sync in the `coffee-first-crack-detection` model repo.
- This repo consumes released Hugging Face model artifacts only.

## Active Context

RoastPilot is being bootstrapped as a standalone Python MCP server that owns roaster control, first-crack detection integration, roast timing, metrics, and log export in one local stdio process.

Epic 3 is complete. The Hottop driver now has validated lifecycle, command-loop,
packet, temperature-unit, heat, fan, drop, cooling, cleanup, and emergency-stop
behavior at the driver boundary. The full connected-Hottop E3-S9 validation run
passed on `/dev/cu.usbserial-DN016OJ3` using 100% heat and 100% fan checks, with
drop and emergency stop included. A follow-up 60-second stability test also held
fan at `10%`, heat at `40%` for 30 seconds, then heat at `100%` for 30 seconds
with continuous command streaming and no command-loop or status-read errors.

Epic 4 is complete. The first-crack path now resolves the configured ONNX model
artifact for both supported real-model precisions: `int8` selects
`onnx/int8/model_quantized.onnx`, and `fp32` selects `onnx/fp32/model.onnx`
through the artifact resolver. When `first_crack.local_model_dir` is configured,
the same repository-relative artifact names resolve from that local directory
without Hugging Face network access, and missing local files fail with a clear
artifact resolution error. Detector artifact validation now resolves both the
selected ONNX model and the precision-specific
`onnx/{precision}/preprocessor_config.json` feature extractor config before
audio detection begins. The audio capture path now has an injectable background
pipeline that reads from the configured audio input, frames complete one-second
mono detector windows at the configured sample rate, and hands windows to a
bounded non-blocking queue for the detector adapter. The detector adapter now
turns injected backend decisions into confirmed first-crack event candidates
with monotonic timestamp, precision, revision, artifact, and optional confidence
metadata for later session integration. This does not add model training,
export, sync, local directory sync, ONNX runtime inference, or MCP session
behavior. Configured microphone and recorded WAV sources now feed the same
E4-S6 `AudioInput` boundary: microphone capture uses a lazy PortAudio-backed
`sounddevice` stream with configured device and sample rate, while WAV replay
uses stdlib PCM decoding, channel-to-mono conversion, and the same mono float
sample contract as live capture. Real microphone validation remains optional
and gated; normal CI uses mocked microphone backends and generated WAV fixtures.
Confirmed detector output in `first_crack.mode: audio` now writes one
`first_crack_detected` event into the authoritative `RoastSessionStore`
timeline at the detector-provided monotonic timestamp with detector metadata
payload. Adapter-inferred default detector timestamps that land slightly ahead
of the integration clock are accepted within the active detector-window
tolerance and recorded at the current elapsed time instead of failing the
automatic path, while explicit future detector timestamps still fail fast.
Confirmed detector output before beans are added is ignored so early false
positives cannot break the detection loop. Detector output is also ignored once
the session leaves active `roasting`, including after first crack, drop,
cooling, completion, fault, or stop, so late confirmations cannot raise session
lifecycle errors. Disabled and manual modes do not let detector output mutate
the session, and automatic detection does not require manual override
permission.

E4-S10 hardened the first-crack and MCP test surface before Epic 5. Direct
in-process MCP tool tests now exercise the current mock-safe device/session
tool surface, manual first-crack behavior, audio-mode bootstrap reporting,
error propagation, and snapshot export through the registered FastMCP tool
bodies. Export tests now prove automatic first-crack detector metadata is
preserved in the current JSONL and CSV event export surfaces; `summary.json`
continues to expose first-crack timestamp and metrics only until Epic 5 final
schemas land. Coverage now has a stable `90%` package floor, with local
branch-aware coverage at `91.73%`. Real microphone validation remains optional
and gated; normal CI requires no audio hardware, model download, Hottop
hardware, or network access. E4-S10 did not wire live Hottop command behavior
or automatic first-crack detector startup into the MCP runtime; those gaps moved
into Epic 4.1.

Epic 4.1 is now complete and closed the operational MCP runtime gaps before
Epic 5. The target user flow is: install the MCP server locally in Claude, start a
roast, adjust the configured roaster through MCP tools, read current device and
session state, and know whether first crack has happened. E4.1 covers
driver-backed MCP control tools, current roaster state exposure, released ONNX
detector backend construction, session-owned first-crack detector lifecycle, and
operational MCP readiness tests/docs. E4.1 now also explicitly owns the
automatic T0 runtime path so a fully agent-driven roast does not depend on
`mark_beans_added` as the primary T0 path when auto-T0 is enabled. T0 is beans
added: the automatic path should track the max preheat/charge bean temperature
and record `beans_added` when current bean temperature drops from that max by
the configured threshold; the pre-drop max is the charge temperature. E4.1-S1
wires `start_roast_session`, `set_heat`, `set_fan`, `drop_beans`,
`start_cooling`, and `stop_cooling` to the configured `RoasterDriver` boundary
while preserving the default mock path and one-session store semantics.
`drop_beans` is now the normal MCP path for drop and cooling transition, and
invalid phase calls are rejected before driver commands are sent.
`mark_beans_added` and `mark_first_crack` remain explicit override tools, while
automatic T0 and first-crack detection are internal runtime paths. `drop_beans`
is the normal agent/operator command for drop and cooling transition;
`start_cooling` is an advanced recovery/manual control rather than the normal
roast flow. E4.1-S2
expands `get_roast_state` with the current configured-device state from
`RoasterDriver.read_state()`, authoritative UTC and monotonic lifecycle event
timestamps, and a structured first-crack status for operator decisions. Driver
state-read failures surface clearly and do not mutate session history. Epic 5
remains focused on telemetry buffering, derived metrics, and final log/export
schemas.

E4.1-S3 added the released-artifact ONNX first-crack detector backend without
starting any session-owned detector lifecycle. `first_crack.mode: audio` can
now construct an ONNX detector adapter from the existing released-artifact
resolver boundary: configured INT8/FP32 artifacts resolve from Hugging Face or
`local_model_dir`, the precision-specific `preprocessor_config.json` is loaded
through `transformers.ASTFeatureExtractor`, and the resolved ONNX model is
opened through an ONNX Runtime CPU session using configured thread limits.
Backend output is adapted into the existing detector confidence metadata path.
Normal CI remains mock-safe through fake artifact paths, fake feature
extraction, and fake ONNX sessions.

E4.1-S4 starts and stops a session-owned first-crack runtime when
`first_crack.mode: audio` is configured. Starting a roast session now prepares
the configured audio capture pipeline and released-artifact ONNX detector
adapter; preparation failures such as missing artifacts, unavailable audio
capture, or detector dependency failures are reflected through
`get_roast_state.first_crack_status` as `unavailable` instead of crashing normal
session control. During active `roasting` after T0, queued detector windows are
processed through the existing detector/session integration helper and confirmed
output records exactly one authoritative `first_crack_detected` event. Runtime
capture or detector errors surface as `faulted` first-crack status. The runtime
stops on first-crack confirmation, explicit manual first-crack override, drop,
cooling completion, emergency stop, and process shutdown. Disabled and manual
modes still do not start audio or detector runtime.

E4.1-S5 added operational MCP readiness coverage and docs before Epic 5. The
stdio MCP test path now asserts the public mock-safe Claude/operator flow:
start a roast, set heat and fan, use explicit override tools when needed, drop
beans through the normal drop/cooling command, stop cooling, read device/session
state, and export current snapshot logs. The same test now pins
`get_roast_state` response schemas for lifecycle timestamps, configured-device
state, and first-crack status so accidental tool-shape drift is caught. README
docs now explain the normal operational MCP flow, first-crack status meanings,
explicit override semantics for `mark_beans_added` and `mark_first_crack`,
`drop_beans` as the normal cooling transition, and gated optional live
Hottop/real microphone validation evidence. Normal CI remains mock-safe.

E4.1-S6 added the automatic T0 runtime path. Automatic T0 remains disabled by
default, but when `session.auto_t0_detection_enabled` is configured, successful
`get_roast_state` driver reads process the current bean temperature before
first-crack runtime windows. The session store tracks the max preheat/charge
bean temperature before T0, records the authoritative `beans_added` event when
the current bean temperature drops from that max by
`session.auto_t0_drop_threshold_c`, and preserves charge temperature,
detected bean temperature, drop, and threshold diagnostics in the event payload
and `get_roast_state.t0_status`. The explicit `mark_beans_added` override
remains available and idempotent. Driver read failures still surface without
session mutation, and normal CI remains mock-safe.

E5-S1 added rolling telemetry capture owned by the authoritative
`RoastSessionStore`. Successful operational `get_roast_state` polling now
appends normalized samples from the configured `RoasterDriver.read_state()` for
the latest active session, retaining ordered UTC/monotonic timestamps,
bean/environment temperatures, heat/fan levels, and cooling state for later
Epic 5 metrics. Driver read failures, stopped sessions, and non-latest sessions
do not mutate the telemetry buffer. RoR, development percent, final log
schemas, append-only telemetry writers, CSV/summary schema changes, and broad
release validation remain later Epic 5/E7 work.

E5-S2 added explicit roast elapsed time computation from the authoritative
session clock. `roast_elapsed_seconds` is now computed through
`compute_roast_elapsed_seconds(...)`: it is `None` before beans are added,
counts from authoritative T0 to the current session clock before drop, and
freezes at authoritative drop time after `beans_dropped`. The existing MCP
state and snapshot summary metrics use this helper through
`compute_roast_metrics(...)`.

E5-S3 added explicit development time and development percent computation from
the authoritative session clock. `development_time_seconds` is now computed
through `compute_development_time_seconds(...)`: it is `None` before first
crack, counts from authoritative first crack to the current session clock before
drop, and freezes at authoritative drop time after `beans_dropped`.
`development_percent` is now computed through `compute_development_percent(...)`
as `development_time_seconds / roast_elapsed_seconds * 100`, using the E5-S2
roast elapsed helper for the denominator. Existing MCP state and snapshot
summary metrics use these helpers through `compute_roast_metrics(...)`.

E5-S4 added explicit 60-second bean and environment temperature deltas from the
E5-S1 rolling telemetry buffer. `bean_temp_delta_60s_c` and
`env_temp_delta_60s_c` are computed as latest minus oldest retained
temperature sample inside the inclusive 60-second window ending at the latest
telemetry sample. Missing sensor values are skipped per sensor, and the metric
returns `None` when no retained temperature value is available for that sensor.
Existing MCP state and snapshot summary metric surfaces use these helpers
through `compute_roast_metrics(...)`. RoR, append-only telemetry writers, final
JSONL/CSV/summary schemas, and broad release validation remain later Epic 5/E7
work.

E5-S5 added explicit bean and environment rate-of-rise metrics from the E5-S1
rolling telemetry buffer. `bean_ror_c_per_min` and `env_ror_c_per_min` compute
latest minus oldest retained temperature sample in the rolling RoR window,
normalized by the actual valid sample span to Celsius per minute. The helpers
skip missing sensor values per sensor and return `None` until the relevant
sensor has at least the configured minimum sample span, defaulting to 10
seconds. Existing MCP state and snapshot summary metric surfaces use these
helpers through `compute_roast_metrics(...)`. Append-only telemetry writers,
final JSONL/CSV/summary schemas, and broad release validation remain later Epic
5/E7 work.

E5-S6 added the append-only runtime JSONL roast log. `RoastSessionStore` now
writes event rows to each session's `roast.jsonl` immediately when new
authoritative timeline events are recorded, and writes telemetry rows from the
existing E5-S1 driver polling path no more often than
`logging.sample_interval_seconds`, defaulting to 5 seconds. The existing rolling
telemetry buffer, metric helpers, one-session store boundary, mock-safe MCP
flow, and final CSV/summary schema boundaries remained unchanged. Snapshot export
continues to write CSV and `summary.json`, but no longer overwrites an existing
append-only `roast.jsonl` file.

E5-S7 added the planned CSV roast log export schema to snapshot
`export_roast_log` output. `roast.csv` now includes telemetry and event rows
using the plan-required columns for timestamps, elapsed seconds, inferred phase,
temperatures, controls, cooling state, event markers, event flags, development
percent, RoR/delta metrics, and first-crack model metadata. Append-only JSONL
runtime logging, the one-session `RoastSessionStore` mutation boundary, existing
metric helpers, and `summary.json` behavior remain unchanged.

E5-S8 added the planned `summary.json` session-level schema. Snapshot summary
export now includes session timestamps, total roast seconds, development
seconds/percent, the configured roaster driver, and first-crack model metadata
from the authoritative first-crack event payload while preserving append-only
JSONL runtime logging, the CSV schema, the one-session store boundary, and
existing metric helpers.

E5-S9 added narrow log schema completeness tests without changing runtime
behavior. Append-only JSONL telemetry and event rows now have exact key-set
coverage, CSV export remains pinned to the E5-S7 field order, and `summary.json`
now has exact top-level, nested metrics, and first-crack model metadata key-set
coverage. Epic 5 metric/log/export helper behavior remains unchanged.

E5-S10 completed the autonomous telemetry sampling gap. Starting a roast
session now starts a session-owned background sampler that polls the configured
driver at `logging.sample_interval_seconds`, defaulting to 5 seconds, and
appends telemetry through the existing `RoastSessionStore` path. Append-only
JSONL telemetry plus RoR/delta metrics now advance without client polling, while
MCP tool calls may still refresh telemetry opportunistically. Successful sampler
reads also run existing automatic T0 and first-crack runtime processing. Driver
read failures fail closed with a diagnosable fault event and stop the sampler.

E6-S1 completed PyPI package metadata for `coffee-roaster-mcp` while keeping
publishing and MCP Registry work out of scope. Project metadata now includes
maintainer metadata, a fuller keyword set, PyPI classifiers for console usage,
Apache licensing, OS independence, hardware/utilities topics, typed-package
status, and a documentation project URL. `RoastPilot` remains the human-facing
title in the package summary. The distribution includes a `py.typed` marker,
and package metadata tests inspect the installed distribution metadata plus the
console script entry point.

E6-S2 added the exact MCP Registry README verification string
`<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->` and focused README
coverage that verifies the string appears once. `server.json`, PyPI publishing,
MCP Registry publishing, release workflow behavior, live hardware validation,
model training/export/sync, real microphone validation, and broad release
validation remain out of scope for E6-S2.

E6-S3 added the root MCP Registry `server.json` with metadata for
`io.github.syamaner/coffee-roaster-mcp`: title `RoastPilot`, PyPI package
`coffee-roaster-mcp`, runtime hint `uvx`, stdio transport, repository metadata,
and the current MCP schema URI. Focused schema and acceptance coverage now pins
the story fields, including URI format validation for registry URL fields.
Version alignment automation, PyPI publishing, MCP Registry publishing, release
workflow behavior, live hardware validation, model training/export/sync, real
microphone validation, and broad release validation remain later stories.

E6-S4 added the version alignment check only. Focused `server.json` coverage
now compares both top-level `server.json.version` and the PyPI package entry
version against the package `coffee_roaster_mcp.__version__`, so registry
metadata and package metadata cannot drift unnoticed. PyPI publishing, MCP
Registry publishing, release workflow behavior, live hardware validation, model
training/export/sync, real microphone validation, and broad release validation
remain later stories.

E6-S5 added the guarded release workflow and operator prerequisite runbook.
`.github/workflows/release.yml` now runs checks, validates release tag/version
alignment, builds package artifacts, supports a manual dry run that does not
upload, publishes to PyPI through Trusted Publishing after `release`
environment approval, and publishes MCP Registry metadata with `mcp-publisher`
GitHub OIDC only after the PyPI publish job succeeds. Review hardening pins
GitHub Actions refs to commit SHAs, disables checkout credential persistence,
and pins the `mcp-publisher` v1.7.9 Linux amd64 asset with SHA-256
verification before execution. Follow-up metadata-validation hardening gives
explicit release-operator errors for missing `__version__` and missing or empty
`server.json.packages`, plus malformed first package entries. `docs/release.md`
documents PyPI account ownership, 2FA/recovery codes, Trusted Publishing setup
for `release.yml`/`release`/`publish-pypi`, protected `v*` tag rules, TestPyPI
status, and the exact `PYPI_API_TOKEN` fallback secret name. Focused workflow
tests pin the trigger, job ordering, environment/OIDC permissions, immutable
action refs, publisher verification, release metadata failure messages, publish
actions, and prerequisite runbook text. Live publishing is not executed by this
story.

E6-S6 completed the MCP Registry publishing verification spike without a live
PyPI release or live Registry publish. `server.json` validated against the
downloaded official `2025-12-11` Registry JSON schema and the preview Registry
API through `mcp-publisher validate server.json`. The pinned
`mcp-publisher` v1.7.9 Linux amd64 workflow asset checksum matched the expected
SHA-256, and the workflow now validates `server.json` before GitHub OIDC
authentication and publish. `docs/release.md` documents the PyPI README
verification marker, non-destructive validation commands, exact live-publish
stop point, prerequisites, expected outcome, and preview Registry risk.
Production PyPI still returns `Not Found` for `coffee-roaster-mcp`, and the
Registry search API returns no current listing for
`io.github.syamaner/coffee-roaster-mcp`; the first destructive step remains the
tag-triggered live release path after PyPI publication succeeds.

Epic 6 now includes follow-up issue #135, `E6-S8: Execute live PyPI and MCP
Registry publish`, for the controlled live publish after PyPI contains the
matching package version and the published long description exposes the exact
`mcp-name` verification marker.

E6-S7 added `docs/install-and-hardware-setup.md` as the setup runbook for mock
install, Hottop configuration, Hugging Face model configuration, offline model
paths, and log output paths. README and the release runbook now cross-reference
that setup guide so release operators can distinguish mock-safe setup from
guarded Hottop operation, audio-mode model setup, and later live validation.
This documentation story did not execute live PyPI publish, live MCP Registry
publish, hardware validation, model training/export/sync, or real microphone
validation.

Epic 7 now includes a final end-to-end agent roast validation story that uses a
real MCP client or agent, configured Hottop hardware, released Hugging Face ONNX
first-crack artifacts, real microphone/audio input, and the Epic 5 stat/log
surface to prove the release candidate can support full roasts with recorded
evidence.

The next story is E6-S8: execute live PyPI and MCP Registry publish after
production PyPI exposes the matching package version and README verification
marker.

The first implementation milestone is now complete. The mock vertical slice can start the MCP server with the mock driver, run a simulated roast through MCP tools, and export JSONL, CSV, and summary logs without roaster hardware or model download.

Epic 2 and Epic 3 are complete. Coverage output is visible in GitHub Actions through a concise Markdown job summary and an `html-coverage-report` artifact.

For Epic 2 implementation, the old `coffee-roasting` repository is a behavioral reference only. Reuse proven roast-session and stdio MCP patterns, but do not recreate the old two-server, Auth0, SSE, or `n8n` architecture.
