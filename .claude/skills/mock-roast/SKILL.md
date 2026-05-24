---
name: mock-roast
description: Validate the current mock-first RoastPilot path without roaster hardware or model download. Use for bootstrap checks now and for the early MCP tool flow while the full vertical slice is still landing.
---

# Mock Roast - RoastPilot

Use this skill for the mock-first local workflow.

## Current Scope

- `E2-S1` provides a real stdio MCP server entrypoint.
- `E2-S4` now provides the first roast-session MCP tool surface on the mock path.
- This workflow validates the mock-safe bootstrap path and the in-process tool
  flow without roaster hardware, microphone access, model download, or network.
  For full operator setup details, use `docs/install-and-hardware-setup.md`.

## Current Validation

Run from the repository root:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
coffee-roaster-mcp --help
coffee-roaster-mcp --version
coffee-roaster-mcp serve
```

Expected bootstrap output:

```text
mock disabled int8
```

## What This Confirms

- The default roaster driver stays on `mock`.
- First-crack detection stays `disabled` by default.
- Default precision stays `int8`.
- Local bootstrap does not require roaster hardware, microphone access, or model download.
- The MCP server now exposes the first session-control tool surface for the mock path.

## Do Not Claim Here

- Do not claim final end-to-end release readiness before `E7-S1`.
- Do not add model download, model export, or Hugging Face sync steps here. Those stay in `coffee-first-crack-detection`.

## Current MCP Tool Flow

The current mock-path tools are:

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

`export_roast_log` writes `roast.jsonl`, `roast.csv`, and `summary.json` under
the configured log directory for the active session.

## Extend Later

Extend this workflow with:

- full mock roast start-to-export smoke checks
- log file content validation
- MCP client fixture reuse across runtime stories
