---
name: code-quality
description: Run RoastPilot quality gates for tests, linting, formatting, type checking, and CLI smoke checks. Use before marking a story complete or before opening a PR.
---

# Code Quality - RoastPilot

Use this skill before marking implementation work complete. Root `AGENTS.md`
is the current gate authority.

## Prerequisites

- Work from the repository root.
- Use Python 3.11+.
- Dependencies must be installed from project metadata:

  The top-level parent alone performs venv/dependency installation where
  network or dependency resolution is needed. Write-capable leaves return
  affected offline-gate evidence and never request network or sandbox
  relaxation.

```bash
python -m pip install -e . --group dev
```

## Required Checks

Run:

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

The top-level parent alone runs build and clean-wheel smoke where dependency
resolution or network access is needed; leaves return affected offline-gate
evidence without requesting network or sandbox relaxation.

## Acceptance

- Coverage-and-branch `pytest` passes.
- `ruff check .` passes.
- `ruff format --check .` passes.
- `pyright` reports 0 errors.
- CLI help and version commands exit successfully.
- Package build and clean-wheel smoke pass.

## If The Environment Is Incomplete

- Do not silently skip checks.
- Create a temporary virtual environment if needed.
- Install dependencies from `pyproject.toml`.
- Record exactly which checks passed and which could not be run.

## Do Not

- Add production test fakes to make tests easier.
- Install dependencies without declaring them in `pyproject.toml`.
- Mark hardware stories complete from unit tests alone.
- Run hardware validation without a separate human-owned contract.
