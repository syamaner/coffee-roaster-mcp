---
name: mock-roast
description: Prepare or audit the current mock-first RoastPilot path without roaster hardware or model download.
---

# Mock Roast - RoastPilot

Use this skill for the mock-first local workflow. Root `AGENTS.md` is the
current gate and tool authority; this skill is hardware-free planning/audit
guidance, not an alternative runtime or delivery contract.

## Current Scope

- This workflow validates the current mock-safe bootstrap path without roaster
  hardware, microphone access, model download, or network.
  For full operator setup details, use `docs/install-and-hardware-setup.md`.

## Current Validation

Run from the repository root:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
coffee-roaster-mcp --help
coffee-roaster-mcp --version
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
- The installed CLI can report its help and version without hardware.

## Do Not Claim Here

- Do not claim final end-to-end release readiness before `E7-S1`.
- Do not add model download, model export, or Hugging Face sync steps here. Those stay in `coffee-first-crack-detection`.

For current MCP tool and mock-roast behaviour, consult root `AGENTS.md`, the
active state authority, and the ratified contract rather than maintaining a
second tool inventory here.
