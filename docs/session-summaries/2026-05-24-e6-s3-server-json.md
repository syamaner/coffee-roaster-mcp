# E6-S3 Server JSON

This summary captures the E6-S3 implementation, validation state, and restart
context.

## Scope

Story: `E6-S3` / issue `#51`, add `server.json`.

Branch: `feature/51-add-server-json`

The implementation stayed inside the E6-S3 boundary:

- add root `server.json`
- declare MCP Registry name `io.github.syamaner/coffee-roaster-mcp`
- declare title `RoastPilot`
- declare PyPI package `coffee-roaster-mcp`
- declare stdio transport
- add a focused schema validation check

No version alignment automation, PyPI publishing, MCP Registry publishing,
release workflow behavior, live hardware validation, model training/export/sync,
real microphone validation, or broad release validation was added.

## Implementation Summary

`server.json` now declares the current MCP Registry schema URI, server metadata,
repository metadata, and one PyPI package entry:

- `name`: `io.github.syamaner/coffee-roaster-mcp`
- `title`: `RoastPilot`
- `package`: `coffee-roaster-mcp`
- `runtimeHint`: `uvx`
- `transport.type`: `stdio`

`tests/test_server_json.py` loads the repository `server.json`, validates it
against the relevant MCP Registry schema constraints, and pins the E6-S3
acceptance fields. `jsonschema` is declared in the dev dependency group for the
schema validation test.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E6-S3` complete and sets
  the active story to `E6-S4`.
- `docs/state/registry.md` says the next story is `E6-S4: add a version
  alignment check`.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_server_json.py`: 2 passed
- `./.venv/bin/python -m ruff check tests/test_server_json.py pyproject.toml`:
  passed
- `./.venv/bin/python -m ruff format --check tests/test_server_json.py`: passed
- `./.venv/bin/python -m pyright tests/test_server_json.py`: 0 errors

Full validation:

- `./.venv/bin/python -m pytest`: 346 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E6-S3 should
be checked first. If it has merged, verify issue #51 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S4 from updated
main on the appropriate `feature/52-...` branch after reading the registry,
active epic, this summary, and the GitHub issue for E6-S4. Keep E6-S4 scoped to
version alignment unless the issue explicitly expands the work.
