---
name: mcp-dev
description: Set up the RoastPilot development environment and run current scaffold-level validation commands. Use when bootstrapping local development or validating early MCP scaffold work.
---

# MCP Dev - RoastPilot

Use this skill for local development setup and scaffold validation.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

## Current Validation

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
coffee-roaster-mcp --help
coffee-roaster-mcp --version
coffee-roaster-mcp serve
```

## Mock-Safe Bootstrap Smoke

For bootstrap work, confirm the default config still requires no roaster hardware, no microphone, and no model download from a guaranteed-empty temporary directory:

```bash
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

Expected output:

```text
mock disabled int8
```

## Notes

- `E2-S1` adds the first stdio MCP server entrypoint with bootstrap-safe introspection tools only.
- The full mock roast flow is still pending later Epic 2 stories.
