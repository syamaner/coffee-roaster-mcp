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

E4-S1 is complete. The first-crack path now has a narrow Hugging Face Hub
artifact resolver that downloads released files from the configured repository
and revision without adding model training, export, sync, detector startup, or
MCP session behavior.

The next story is E4-S2: load INT8 ONNX by default.

The first implementation milestone is now complete. The mock vertical slice can start the MCP server with the mock driver, run a simulated roast through MCP tools, and export JSONL, CSV, and summary logs without roaster hardware or model download.

Epic 2 and Epic 3 are complete. Coverage output is visible in GitHub Actions through a concise Markdown job summary and an `html-coverage-report` artifact.

For Epic 2 implementation, the old `coffee-roasting` repository is a behavioral reference only. Reuse proven roast-session and stdio MCP patterns, but do not recreate the old two-server, Auth0, SSE, or `n8n` architecture.
