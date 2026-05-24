# E6-S2 README MCP Verification String

This summary captures the E6-S2 implementation, validation state, and restart
context.

## Scope

Story: `E6-S2` / issue `#50`, add README MCP verification string.

Branch: `feature/50-readme-mcp-verification-string`

The implementation stayed inside the E6-S2 boundary:

- add `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->` to `README.md`
- add a focused README verification string check
- preserve the existing package metadata, runtime behavior, registry metadata
  boundary, and release workflow boundary

No `server.json`, PyPI publishing, MCP Registry publishing, release workflow,
live hardware validation, model training/export/sync, real microphone
validation, or broad release validation was added.

## Implementation Summary

`README.md` now includes the hidden MCP Registry verification comment directly
under the `RoastPilot` heading:

```html
<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->
```

`tests/test_readme.py` reads the repository README and asserts that the exact
verification string appears once.

Durable state updates:

- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E6-S2` complete and sets
  the active story to `E6-S3`.
- `docs/state/registry.md` says the next story is `E6-S3: add server.json`.

## Validation

Focused validation:

- `./.venv/bin/python -m pytest tests/test_readme.py`: 1 passed
- `./.venv/bin/python -m ruff check README.md tests/test_readme.py`: passed
- `./.venv/bin/python -m ruff format --check tests/test_readme.py`: passed

Full validation:

- `./.venv/bin/python -m pytest`: 344 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. PR for E6-S2 should
be checked first. If it has merged, verify issue #50 is closed, check out
`main`, run `git pull --ff-only origin main`, then begin E6-S3 from updated
main on the appropriate `feature/51-...` branch after reading the registry,
active epic, this summary, and GitHub issue #51. Keep E6-S3 scoped to
`server.json` unless issue #51 explicitly expands the work.
