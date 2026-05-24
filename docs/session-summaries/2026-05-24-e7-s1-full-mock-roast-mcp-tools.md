# E7-S1 Full Mock Roast MCP Tools

This summary captures the E7-S1 mock-safe validation story, implementation
scope, validation evidence, and restart context.

## Scope

Story: `E7-S1` / issue `#56`, test a full mock roast through MCP tools.

Branch: `feature/56-full-mock-roast-mcp-tools`

The work stayed inside the public stdio MCP mock-roast validation boundary:

- start the MCP server with default config from an empty temporary directory
- confirm the runtime config uses roaster driver `mock`
- confirm first-crack mode stays `disabled`
- confirm automatic T0 detection stays disabled
- run a mock roast through public MCP tools from session start through export
- verify exported `roast.jsonl`, `roast.csv`, and `summary.json` outputs
- add focused end-to-end mock roast coverage
- update durable state and handoff notes

No Hottop hardware validation, model training/export/sync, real microphone
validation, or live release publishing was performed.

## Implementation Summary

- Updated `tests/test_package.py` so
  `test_stdio_server_supports_basic_mock_roast_tool_flow` now verifies the
  default runtime config before driving the mock roast.
- The stdio MCP flow now verifies:
  - `roaster_driver` is `mock`
  - `first_crack_mode` is `disabled`
  - `auto_t0_detection_enabled` is `False`
  - the mock roast reaches `complete`
  - the device state comes from the mock driver
  - first crack is present only because the public manual override tool was
    called
  - `export_roast_log` returns absolute paths for `roast.jsonl`, `roast.csv`,
    and `summary.json`
- The export assertions now read all three files produced by the MCP export:
  - `roast.jsonl` contains the expected event order:
    `beans_added`, `first_crack_detected`, `beans_dropped`,
    `cooling_started`, `cooling_stopped`
  - `roast.csv` contains the same event order with lifecycle phases
    `roasting`, `development`, `dropped`, `cooling`, `complete`
  - `summary.json` records phase `complete`, event count `5`, roaster driver
    `mock`, empty first-crack model metadata for disabled mode, and populated
    roast/development metrics

## Validation

Preflight:

- PR #137 was merged.
- Issue #135 was closed.
- `main` was fast-forwarded to
  `5052ab29ec142cfe6e28bfb3e5bf17d529d006c3`.
- Branch `feature/56-full-mock-roast-mcp-tools` was created from updated
  `main`.

Commands run:

- `./.venv/bin/python -m pytest tests/test_package.py::test_stdio_server_supports_basic_mock_roast_tool_flow`:
  1 passed.
- `./.venv/bin/python -m pytest tests/test_package.py`: 19 passed.
- `./.venv/bin/python -m pytest`: 356 passed.
- `./.venv/bin/python -m ruff check .`: passed.
- `./.venv/bin/python -m ruff format --check .`: 30 files already formatted.
- `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations.
- `./.venv/bin/coffee-roaster-mcp --help`: passed.
- `./.venv/bin/coffee-roaster-mcp --version`:
  `coffee-roaster-mcp 0.1.0`.

## Risks And Notes

- This story proves the default mock-safe stdio MCP path only. It does not
  prove package installation from PyPI or a built wheel; that is E7-S2.
- This story does not prove discovery through an external MCP client beyond the
  stdio test client harness; that is E7-S3.
- This story does not exercise Hottop hardware, real microphone input, or the
  released Hugging Face ONNX audio path. Those remain later Epic 7 validation
  stories.
- The first-crack event in this story is intentionally produced through the
  public `mark_first_crack` override while default first-crack mode remains
  `disabled`.

## Usage Snapshot

- Token usage: `275K used`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. E7-S1 is complete
on branch `feature/56-full-mock-roast-mcp-tools`: the public stdio MCP mock
roast flow now verifies default mock/disabled configuration and exported JSONL,
CSV, and summary outputs. After the E7-S1 PR merges and issue #56 closes, sync
`main` and route next work to E7-S2 package install smoke validation unless the
operator selects a different story. Do not run hardware validation, model
training/export/sync, real microphone validation, or live release publishing
unless the selected story explicitly requires it.
