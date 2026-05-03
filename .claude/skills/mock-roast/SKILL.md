---
name: mock-roast
description: Validate the current mock-first RoastPilot path without roaster hardware or model download. Use when checking bootstrap defaults now, and extend it later when the MCP runtime lands.
---

# Mock Roast - RoastPilot

Use this skill for the mock-first local workflow.

## Current Scope

- `E2-S1` provides a real stdio MCP server entrypoint.
- A full mock roast through MCP tools is not implemented until later runtime stories.
- This workflow validates the current mock-safe bootstrap path without claiming a real roast session exists yet.

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

## Do Not Claim Yet

- Do not claim a start-to-export mock roast before `E2-S7` and `E7-S1`.
- Do not add model download, model export, or Hugging Face sync steps here. Those stay in `coffee-first-crack-detection`.

## Extend Later

Once the MCP runtime exists, extend this workflow with:

- mock roast session start
- state polling
- manual first-crack injection if needed
- roast log export smoke checks
