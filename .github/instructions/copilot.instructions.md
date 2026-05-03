# Copilot Code Review Instructions

## Project Overview

RoastPilot is a spec-driven Python MCP server for autonomous coffee roasting. The server will own roaster control, telemetry, first-crack event integration, roast timing, derived metrics, event logging, and export in one local stdio process.

Full project rules are in `AGENTS.md` at the repo root.

## Key Conventions

### Runtime Boundary

- v0.1 is a local stdio MCP server.
- Agent orchestration and n8n are out of scope.
- One `RoastSession` runtime will own timing, telemetry, events, metrics, and logs.
- `beans_added_at` is T0. Auto-T0 detection is disabled by default.

### Configuration

- Config defaults must allow a local mock run with no config file.
- Default roaster driver is `mock`.
- Default first-crack mode is `disabled`.
- YAML config is loaded from `coffee-roaster-mcp.yaml`.
- Environment overrides must be applied after file config.
- Use explicit `is None` checks where falsy values such as `0`, `0.0`, or empty strings have distinct meaning.

### Hugging Face Models

- This repo consumes released artifacts from `syamaner/coffee-first-crack-detection`.
- Do not add model training, model export, Hugging Face sync, model card, or dataset card publishing here.
- INT8 ONNX is the default runtime precision.
- FP32 ONNX is supported by config.

### Roaster Safety

- Hardware behavior must be conservative.
- Hottop command-loop behavior, packet handling, temperature units, drop, cooling, and emergency stop need explicit tests or manual validation notes.
- Do not accept mock-only validation for hardware-ready claims.

### Code Style

- Python 3.11+ with full type hints on public functions and methods.
- Google-style docstrings for public APIs.
- `ruff check .`, `ruff format --check .`, `pyright`, and `pytest` should pass.
- Dependencies must be declared in `pyproject.toml`.

### Generated And Local Files

- Do not commit model weights, ONNX files, audio recordings, roast logs, serial captures, `.env` files, or IDE folders.
- Generated roast logs will belong under ignored log directories once logging lands.

### Testing

- Tests live under `tests/`.
- Avoid production-only fakes created solely for tests.
- Config tests should cover defaults, YAML overrides, environment overrides, and validation failures.

