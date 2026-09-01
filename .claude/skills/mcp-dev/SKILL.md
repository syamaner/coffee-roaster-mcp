---
name: mcp-dev
description: Set up the RoastPilot development environment and run current hardware-free validation commands.
---

# MCP Dev - RoastPilot

Use this skill for local development setup and hardware-free validation. Root
`AGENTS.md` is the current gate authority.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

## Current Validation

```bash
python -m pytest --cov=coffee_roaster_mcp --cov-branch --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m pyright
coffee-roaster-mcp --help
coffee-roaster-mcp --version
python -m build
python .github/scripts/smoke_install_built_wheel.py
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

- All validation remains hardware-free unless a separate human-owned contract
  authorises otherwise.
