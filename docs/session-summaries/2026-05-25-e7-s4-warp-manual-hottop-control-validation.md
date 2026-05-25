# E7-S4 Warp Manual Hottop MCP Control Validation

This summary captures the E7-S4 start, safety boundary, preflight evidence,
current manual-validation status, and restart context.

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
  port: /dev/cu.usbserial-DN016OJ3
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3

first_crack:
  mode: disabled

session:
  auto_t0_detection_enabled: false
```

The `roaster.port` value was set to the actual Hottop serial device visible on
the validation host during preflight:

- `/dev/cu.usbserial-DN016OJ3`

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

Initial preflight did not show a Hottop adapter. After the operator reconnected
the device, `/dev/cu.usbserial-DN016OJ3` appeared.

Config creation and local non-hardware verification:

- Created `/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml` with
  `roaster.driver: hottop_kn8828b_2k_plus`,
  `roaster.port: /dev/cu.usbserial-DN016OJ3`,
  `first_crack.mode: disabled`, and
  `session.auto_t0_detection_enabled: false`.
- Ran local config-load verification without connecting to hardware:
  `./.venv/bin/python -c "..."` returned
  `hottop_kn8828b_2k_plus /dev/cu.usbserial-DN016OJ3 disabled False`.

Required repo checks:

- Targeted recovery tests after Warp emergency-stop finding:
  `./.venv/bin/python -m pytest tests/test_mcp_server.py::test_stop_cooling_recovers_after_emergency_stop_leaves_cooling_on tests/test_mcp_server.py::test_stop_cooling_still_rejects_completed_inactive_session tests/test_mcp_server.py::test_stop_cooling_uses_driver_cooling_state_before_completing`:
  3 passed.
- `./.venv/bin/python -m pytest`: 358 passed.
- `./.venv/bin/python -m ruff check .`: passed.
- `./.venv/bin/python -m ruff format --check .`: 31 files already formatted.
- `./.venv/bin/python -m pyright`: 0 errors, 0 warnings, 0 informations.
- `./.venv/bin/coffee-roaster-mcp --help`: passed.
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.

## Current Status

Status: live Warp Hottop validation surfaced a recovery bug after emergency
stop; a code fix is now on the branch.

Observed Warp behavior:

- `drop_beans` from `pre_roast` was rejected before the driver boundary:
  `beans_dropped cannot be recorded while phase is pre_roast; allowed phases:
  roasting, development`. This is expected lifecycle-guard behavior, not a bug.
- After emergency stop, Warp reported `get_roast_state` with `phase: fault`,
  `active: false`, `cooling_on: true`, `fan_level_percent: 100`, and
  `cooling_motor_on: true`.
- A subsequent `stop_cooling` attempt failed with
  `No active roast session exists.`

Bug conclusion:

- Emergency stop intentionally leaves cooling on as the fail-closed hardware
  state.
- The MCP `stop_cooling` tool incorrectly required an active session, so the
  operator could not stop cooling through MCP after emergency stop faulted and
  stopped the session.

Fix:

- `stop_cooling` now has a narrow recovery path for the latest stopped
  `fault` session when cooling is still on.
- The recovery path sends only the configured driver `stop_cooling` command,
  records a `cooling_stopped` event with `recovery_after_fault: true`, sets
  cooling off in session state, and preserves `phase: fault` / `active: false`.
- Completed inactive sessions still reject `stop_cooling`, so the fix does not
  reopen normal controls after completion.

Actions deliberately not taken:

- did not mark E7-S4 complete from partial/faulted Warp evidence
- did not export or claim hardware validation artifacts

Hardware-ready release-label decision: still blocked. The emergency-stop
recovery bug must be validated through Warp before hardware-ready status can be
considered.

## Resume Checklist

Before resuming hardware validation:

- confirm the Hottop USB serial adapter still appears with `ls /dev/cu.*`
- keep the physical stop plan ready
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
- specifically rerun post-emergency `stop_cooling` recovery through Warp and
  confirm cooling turns off while the session remains faulted and inactive
- stop immediately on unexpected telemetry, command-loop errors, unsafe roaster
  behavior, or uncertainty

## Restart Prompt

Resume in `/Users/sertanyamaner/git/coffee-roaster-mcp` on branch
`feature/59-warp-manual-hottop-control-validation`. E7-S4 is started and the
Hottop adapter is now visible at `/dev/cu.usbserial-DN016OJ3`. The config file
`/tmp/roastpilot-warp-hottop/coffee-roaster-mcp.yaml` was created with driver
`hottop_kn8828b_2k_plus`, port `/dev/cu.usbserial-DN016OJ3`,
`first_crack.mode: disabled`, and
`session.auto_t0_detection_enabled: false`; local config-load verification
returned `hottop_kn8828b_2k_plus /dev/cu.usbserial-DN016OJ3 disabled False`.
PR #140 is merged, issue #58 is closed, `main` was fast-forwarded to `c480afc`,
and `uvx` is available at `/opt/homebrew/bin/uvx` with version `0.11.16`. Read
`docs/state/registry.md`,
`docs/state/epics/coffee-roaster-mcp-v0.1.md`,
`docs/session-summaries/2026-05-24-e7-s3-warp-mcp-client-connection.md`, this
summary, and issue #59 including
`https://github.com/syamaner/coffee-roaster-mcp/issues/59#issuecomment-4529994261`.
Warp hardware validation surfaced a recovery bug: after emergency stop,
`get_roast_state` showed `phase: fault`, `active: false`, `cooling_on: true`,
`fan_level_percent: 100`, and `cooling_motor_on: true`, but `stop_cooling`
failed with `No active roast session exists.` The branch now includes a narrow
post-emergency recovery fix allowing `stop_cooling` only for the latest stopped
fault session with cooling still on, while preserving `phase: fault` and
`active: false`. Next, run the updated branch locally or publish/install an
updated package build before retesting in Warp, then specifically validate
post-emergency `stop_cooling` recovery with explicit operator approval and
before/after `get_roast_state`. Do not run autonomous roasting, ChatGPT MCP
validation, model training/export/sync, real microphone or ONNX audio-path
validation, live PyPI/MCP Registry publishing, or full end-to-end agent roast
validation unless separately selected.
