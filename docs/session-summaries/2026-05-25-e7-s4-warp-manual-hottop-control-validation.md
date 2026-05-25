# E7-S4 Warp Manual Hottop MCP Control Validation

This summary captures the E7-S4 start, safety boundary, preflight evidence,
current blocker, and restart context.

## Scope

Story: `E7-S4` / issue `#59`, run Warp manual Hottop MCP control validation.

Branch: `feature/59-warp-manual-hottop-control-validation`

The work is limited to Warp manual Hottop MCP control validation:

- use Warp as the MCP client surface
- configure the published `coffee-roaster-mcp==0.1.0` package for Hottop
- keep `first_crack.mode: disabled`
- keep `session.auto_t0_detection_enabled: false`
- require explicit operator approval before every hardware-affecting tool call
- record device state before and after each hardware-affecting command
- export and review `roast.jsonl`, `roast.csv`, and `summary.json` only after
  a supervised validation run

No autonomous roasting, ChatGPT MCP validation, model training/export/sync, real
microphone or ONNX audio-path validation, live PyPI/MCP Registry publishing, or
full end-to-end agent roast validation is in scope.

## Required Warp Configuration

Hottop working directory:

- `/tmp/roastpilot-warp-hottop`

Config file:

- `/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml`

Required config values:

```yaml
transport:
  type: stdio

roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/cu.usbserial-XXXX
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3

first_crack:
  mode: disabled

session:
  auto_t0_detection_enabled: false
```

The `roaster.port` value must be replaced with the actual Hottop serial device
visible on the validation host. Do not use a placeholder or stale port value for
live hardware validation.

Warp MCP server JSON:

```json
{
  "mcpServers": {
    "roastpilot-hottop": {
      "command": "uvx",
      "args": [
        "--from",
        "coffee-roaster-mcp==0.1.0",
        "coffee-roaster-mcp",
        "serve"
      ],
      "env": {
        "COFFEE_ROASTER_MCP_CONFIG": "/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml"
      },
      "working_directory": "/tmp/roastpilot-warp-hottop"
    }
  }
}
```

If Warp cannot find `uvx`, use:

```json
"command": "/opt/homebrew/bin/uvx"
```

## Preflight Evidence

GitHub and branch gate:

- PR #140 was merged.
- Issue #58 was closed.
- `git checkout main`: passed.
- `git pull --ff-only origin main`: fast-forwarded `main` from
  `de18572edf3ea69f15a9e60a04f049a20f96ae34` to `c480afc`.
- Created branch `feature/59-warp-manual-hottop-control-validation`.

State and issue context read before starting:

- `docs/state/registry.md`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`
- `docs/session-summaries/2026-05-24-e7-s3-warp-mcp-client-connection.md`
- GitHub issue #59 and configuration guidance comment
  `https://github.com/syamaner/coffee-roaster-mcp/issues/59#issuecomment-4529994261`

Local command preflight:

- `command -v uvx`: `/opt/homebrew/bin/uvx`
- `/opt/homebrew/bin/uvx --version`: `uvx 0.11.16`
- `ls /dev/cu.*`:
  - `/dev/cu.Bluetooth-Incoming-Port`
  - `/dev/cu.debug-console`

No `/dev/cu.usbserial-*` Hottop adapter was visible during this preflight.

Required repo checks:

- `./.venv/bin/python -m pytest`: 356 passed.
- `./.venv/bin/python -m ruff check .`: passed.
- `./.venv/bin/python -m ruff format --check .`: 31 files already formatted.
- `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations.
- `./.venv/bin/coffee-roaster-mcp --help`: passed.
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.

## Current Status

Status: blocked before live Warp hardware validation.

Reason: no actual Hottop USB serial adapter was visible, so the required
`roaster.port` could not be set to a verified device path.

Actions deliberately not taken:

- did not create `/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml` with a
  fake or stale serial port
- did not launch the Hottop-configured server through Warp
- did not call `start_roast_session`, `set_heat`, `set_fan`, `drop_beans`,
  `start_cooling`, `stop_cooling`, or `emergency_stop` against hardware
- did not export or claim hardware validation artifacts

Hardware-ready release-label decision: blocked. Partial preflight evidence does
not support a hardware-ready release label.

## Resume Checklist

Before resuming hardware validation:

- connect the Hottop USB serial adapter
- confirm the actual port appears with `ls /dev/cu.*`
- keep the physical stop plan ready
- create `/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml` with the actual
  `roaster.port`
- configure Warp with the `roastpilot-hottop` MCP JSON
- confirm Warp discovers the expected tools
- call `get_runtime_config` and confirm:
  - `roaster_driver`: `hottop_kn8828b_2k_plus`
  - `first_crack_mode`: `disabled`
  - `auto_t0_detection_enabled`: `false`
- call `get_roast_state` and record initial device state before controls
- require explicit operator approval before every hardware-affecting MCP call
- record device state before and after connect, heat, fan, drop, cooling,
  stop-cooling, and emergency-stop validation
- stop immediately on unexpected telemetry, command-loop errors, unsafe roaster
  behavior, or uncertainty

## Restart Prompt

Resume in `/Users/sertanyamaner/git/coffee-roaster-mcp` on branch
`feature/59-warp-manual-hottop-control-validation`. E7-S4 is started but blocked
before live Warp Hottop validation because no actual Hottop USB serial adapter
was visible: `ls /dev/cu.*` showed only `/dev/cu.Bluetooth-Incoming-Port` and
`/dev/cu.debug-console`. PR #140 is merged, issue #58 is closed, `main` was
fast-forwarded to `c480afc`, and `uvx` is available at `/opt/homebrew/bin/uvx`
with version `0.11.16`. Read `docs/state/registry.md`,
`docs/state/epics/coffee-roaster-mcp-v0.1.md`,
`docs/session-summaries/2026-05-24-e7-s3-warp-mcp-client-connection.md`, this
summary, and issue #59 including
`https://github.com/syamaner/coffee-roaster-mcp/issues/59#issuecomment-4529994261`.
After the Hottop adapter is connected, create
`/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml` with actual
`roaster.port`, keep `first_crack.mode: disabled` and
`session.auto_t0_detection_enabled: false`, launch through Warp using
`uvx --from coffee-roaster-mcp==0.1.0 coffee-roaster-mcp serve` or
`/opt/homebrew/bin/uvx`, and require explicit operator approval plus before/after
device-state evidence for every hardware-affecting tool call. Do not run
autonomous roasting, ChatGPT MCP validation, model training/export/sync, real
microphone or ONNX audio-path validation, live PyPI/MCP Registry publishing, or
full end-to-end agent roast validation unless separately selected.
