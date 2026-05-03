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
```

## Config Smoke

Default config should require no roaster hardware, no microphone, and no model download:

```bash
python -c "from coffee_roaster_mcp.config import load_config; c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision)"
```

Expected output:

```text
mock disabled int8
```

## Notes

- The full MCP server and mock roast flow are not implemented yet.
- Once E2 stories land, extend this skill with mock MCP server startup and basic tool-call smoke tests.

