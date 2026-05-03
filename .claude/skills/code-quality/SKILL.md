---
name: code-quality
description: Run RoastPilot quality gates for tests, linting, formatting, type checking, and CLI smoke checks. Use before marking a story complete or before opening a PR.
---

# Code Quality - RoastPilot

Use this skill before marking implementation work complete.

## Prerequisites

- Work from the repository root.
- Use Python 3.11+.
- Dependencies must be installed from project metadata:

```bash
python -m pip install -e . --group dev
```

## Required Checks

Run:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
coffee-roaster-mcp --help
coffee-roaster-mcp --version
```

## Acceptance

- `pytest` passes.
- `ruff check .` passes.
- `ruff format --check .` passes.
- `pyright` reports 0 errors.
- CLI help and version commands exit successfully.

## If The Environment Is Incomplete

- Do not silently skip checks.
- Create a temporary virtual environment if needed.
- Install dependencies from `pyproject.toml`.
- Record exactly which checks passed and which could not be run.

## Do Not

- Add production test fakes to make tests easier.
- Install dependencies without declaring them in `pyproject.toml`.
- Mark hardware stories complete from unit tests alone.

