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

Epic 4.1 is now active before Epic 5 to close operational MCP runtime gaps.
The target user flow is: install the MCP server locally in Claude, start a
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

Epic 7 now includes a final end-to-end agent roast validation story that uses a
real MCP client or agent, configured Hottop hardware, released Hugging Face ONNX
first-crack artifacts, real microphone/audio input, and the Epic 5 stat/log
surface to prove the release candidate can support full roasts with recorded
evidence.

The next story is E4.1-S4: start first-crack detection runtime with roast
sessions.

The first implementation milestone is now complete. The mock vertical slice can start the MCP server with the mock driver, run a simulated roast through MCP tools, and export JSONL, CSV, and summary logs without roaster hardware or model download.

Epic 2 and Epic 3 are complete. Coverage output is visible in GitHub Actions through a concise Markdown job summary and an `html-coverage-report` artifact.

For Epic 2 implementation, the old `coffee-roasting` repository is a behavioral reference only. Reuse proven roast-session and stdio MCP patterns, but do not recreate the old two-server, Auth0, SSE, or `n8n` architecture.
