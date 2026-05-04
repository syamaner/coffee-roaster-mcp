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

E2-S6 is complete. RoastPilot emergency stop now routes through a driver-owned mock safety method, records a fault event with driver-call evidence, and keeps fault state visible through the existing MCP session state response.

The next story is E2-S7: complete the thin vertical slice spike for one-process mock roast flow and export readiness.

The first implementation milestone remains a thin mock vertical slice: install the package, start the MCP server with the mock driver, run a simulated roast through MCP tools, and export JSONL, CSV, and summary logs without roaster hardware or model download.

For Epic 2 implementation, the old `coffee-roasting` repository is a behavioral reference only. Reuse proven roast-session and stdio MCP patterns, but do not recreate the old two-server, Auth0, SSE, or `n8n` architecture.
