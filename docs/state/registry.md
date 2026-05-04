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

E2-S8 is complete. RoastPilot pull-request CI now runs tests with coverage for `coffee_roaster_mcp`, writes a readable Markdown coverage summary in GitHub Actions, and uploads an HTML coverage artifact for file-by-file drill-down.

The next story is E3-S1: define the broader roaster driver interface and capabilities model.

The first implementation milestone is now complete. The mock vertical slice can start the MCP server with the mock driver, run a simulated roast through MCP tools, and export JSONL, CSV, and summary logs without roaster hardware or model download.

Epic 2 is complete enough to move into Epic 3 driver contract work. Coverage output is visible in GitHub Actions through a concise Markdown job summary and an `html-coverage-report` artifact.

For Epic 2 implementation, the old `coffee-roasting` repository is a behavioral reference only. Reuse proven roast-session and stdio MCP patterns, but do not recreate the old two-server, Auth0, SSE, or `n8n` architecture.
