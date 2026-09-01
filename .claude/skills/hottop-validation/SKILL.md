---
name: hottop-validation
description: Human-only Hottop execution; agents prepare or audit a guarded manual validation plan and evidence without operating hardware.
---

# Hottop Validation - RoastPilot

Agents use this skill only to prepare or audit a human-owned plan or evidence.
Only a named human operator may execute commands or touch a Pi, serial device,
microphone, or Hottop. Agents must never execute commands or recommend
proceeding autonomously.

Root `AGENTS.md` is the current hardware-free gate authority and supersedes any
stale gate wording in this skill.

## Current Scope

- Hottop lifecycle, command-loop streaming, packet build/parse, control command state, and temperature-unit handling exist behind the `HottopRoasterDriver` boundary.
- The MCP roast-session tools call the configured `RoasterDriver` boundary
  while preserving the one-session store semantics and fail-closed behavior.
- The runnable validation entrypoint is `coffee-roaster-mcp hottop-validate`.
- Hardware stories are not complete from mock tests alone.
- For operator setup details, including the Hottop config block and log output
  paths, use `docs/install-and-hardware-setup.md`.

## Pre-Validation Gates

The named human operator completes these gates before any Hottop hardware
session; an agent may only audit the plan or resulting evidence.

### 1. Story And Source Readiness

- The named human operator confirms durable E3 history in
  `docs/state/epics/coffee-roaster-mcp-v0.1.md` and the current human-owned
  validation task; no E3 story is implied active.
- The named human operator confirms unit and integration coverage exists in
  `tests/test_drivers.py` and `tests/test_hottop_validation.py`.
- The named human operator confirms fail-closed behavior in
  `src/coffee_roaster_mcp/drivers.py`.

### 2. Operator And Hardware Readiness

- The named human operator confirms the roaster is supervised for the full run.
- The named human operator confirms understanding of emergency stop, bean drop,
  cooling, and physical power-off expectations.
- The named human operator confirms the serial port is known and the config
  explicitly sets `roaster.driver: hottop_kn8828b_2k_plus`.
- The named human operator confirms that `--include-drop` is irreversible for
  loaded beans.

### 3. Run Readiness

- The named human operator runs non-destructive validation before any full
  validation.
- The named human operator proceeds to `--include-drop` only when the roaster
  is ready for an actual drop check, and to `--include-emergency-stop` only when
  ready to verify that safety action.

The human operator confirms these source artifacts before running hardware:

- `src/coffee_roaster_mcp/drivers.py`: `HottopRoasterDriver`, command-loop lifecycle, command state, packet build/parse, status read, temperature normalization, and emergency stop.
- `src/coffee_roaster_mcp/hottop_validation.py`: guarded `hottop-validate` runner, JSON evidence shape, skipped-step behavior, and release-label decision.
- `src/coffee_roaster_mcp/cli.py`: `hottop-validate` CLI options and acknowledgement flag.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`: E3-S4 through E3-S9 status and validation notes.

## Hard Abort Conditions

The named human operator stops the validation session immediately if any of
these occur:

- The serial port cannot be identified confidently or opens the wrong device.
- The command reports repeated serial write, read, checksum, or command-loop errors.
- The roaster heats when the current step expects heat off.
- Heat, fan, drop, cooling, or emergency stop causes a physical action inconsistent with the expected state.
- Temperature readings are absent, implausible, or jump unexpectedly after the telemetry wait.
- The command loop does not stop cleanly on disconnect.
- The operator loses direct supervision of the roaster.
- Smoke, electrical smell, uncontrolled heat, jammed drop, or unexpected mechanical behavior appears.

Human-operator abort procedure:

1. The named human operator runs or triggers emergency stop if safe.
2. The named human operator physically powers off the roaster if software
   control is uncertain.
3. The named human operator preserves the JSON evidence file and terminal output.
4. The named human operator does not continue to later steps in the same run.
5. The named human operator records the failed step, observed behaviour, and
   whether the roaster was physically powered off.

## Guarded Validation Command

Agents stop at plan/evidence preparation here. Only the named human operator
may run the following guarded hardware commands or touch the connected devices.

The human operator uses a local config file with an explicit Hottop driver and
serial port:

```yaml
roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/cu.usbserial-XXXX
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3
```

The human operator runs the non-destructive portion first:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-non-destructive.json \
  --i-understand-this-controls-hardware
```

The human operator runs the full validation only when the roaster is supervised
and ready for drop and emergency-stop checks:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-full.json \
  --i-understand-this-controls-hardware \
  --include-drop \
  --include-emergency-stop
```

The named human operator does not commit generated validation JSON unless it is
sanitised for long-term storage, and never commits raw serial captures.

## Pass/Fail Criteria

The human operator uses this table with the JSON evidence from
`hottop-validate` plus direct observation; agents audit supplied evidence only.

| Area | Pass | Needs Review Or Skipped | Fail |
| --- | --- | --- | --- |
| Connection and cleanup | `connect` is `passed`, `raw.command_loop_running` is true while connected, command writes increase, and disconnect exits cleanly. | Not applicable. | Serial open fails, writes do not occur after connection, command-loop errors increase, or disconnect reports the loop did not stop. |
| Startup safe state | First connected state shows heat `0`, fan `0` unless cooling is commanded, cooling false, solenoid false, and drum false before heat is requested. | Not applicable. | Heat, cooling, drop/solenoid, or unexpected fan behavior starts without an explicit validation step. |
| Packet parsing and telemetry | `stable_telemetry` is `passed`, `raw.status_packet_count` is greater than zero, `bean_temp_c` and `env_temp_c` are plausible, and `raw.status_read_error_count` is zero. | `stable_telemetry` is `needs_review`, startup zero readings are ignored, or temperatures are missing while the command loop otherwise runs. | Packet counts never increase, checksum/read errors repeat, command echoes are parsed as telemetry, or normalized temperatures are physically impossible. |
| Temperature units | Configured `temperature_unit` matches the run plan, `raw.resolved_temperature_unit` is stable after plausible telemetry, and Celsius-normalized values are plausible. | `auto` changes resolved unit during warmup or after ignored packets. | Fahrenheit readings are exposed as Celsius, Celsius readings are double-converted, or unit resolution remains absent after stable telemetry. |
| Heat | Heat step sets `heat_level_percent` to the requested conservative value, drum is on when heat is nonzero, then `heat_off` returns heat to `0`. | Not applicable. | Heat remains on after `heat_off`, heat changes without command, or drum behavior contradicts the heat command. |
| Fan | Fan step sets `fan_level_percent` to the requested value and the Hottop main fan responds consistently. | Not applicable. | Fan value is outside the requested range, fan does not respond, or fan remains high after cooling stop except as part of emergency stop. |
| Drop | In a full run, `drop` is `passed`, heat is `0`, drum is off, solenoid/drop path is active, cooling is on, and fan is high. | `drop` is skipped in a non-destructive run. This blocks hardware-ready release approval. | Drop is triggered unintentionally, fails to trigger when requested, or does not force heat off and cooling/fan on. |
| Cooling stop | `cooling_start` turns cooling on with high fan, and `cooling_stop` clears cooling, solenoid/drop path, and fan. | Not applicable. | Cooling does not start, does not stop, or leaves drop/solenoid state active. |
| Emergency stop | In a full run, emergency stop sets heat `0`, drum off, solenoid closed, cooling on, fan high, and evidence preserves diagnostic state. | Emergency stop is skipped in a non-destructive run. This blocks hardware-ready release approval. | Emergency stop does not force heat off, does not leave cooling/fan in a safe state, or loses diagnostic evidence. |

## Troubleshooting

### Serial Connection Fails

- The named human operator checks the port with `ls /dev/cu.*` before and after
  plugging in the USB adapter, confirms the exact `roaster.port`, and confirms
  no other process has it open.
- The named human operator re-runs only the non-destructive command until
  connection and cleanup pass.

### Command Loop Or Write Counters Do Not Advance

- The named human operator checks `raw.command_loop_running`,
  `raw.command_loop_iterations`, `raw.command_send_attempts`,
  `raw.command_write_count`, `raw.command_loop_error_count`, and
  `raw.last_command_write_size`.
- The named human operator treats repeated errors or partial writes as a hard
  abort and reviews the durable epic history before any later code task.

### Packet Or Temperature Problems

- The named human operator checks `raw.status_packet_count`,
  `raw.ignored_temperature_packet_count`, `raw.status_read_error_count`,
  `raw.raw_bean_temperature`, `raw.raw_env_temperature`, and
  `raw.resolved_temperature_unit`.
- The named human operator records plausible post-startup readings as acceptable
  warmup behaviour; for consistently implausible values, stops and reviews the
  packet offsets, checksum behaviour, and configured `temperature_unit`.

### Drop Or Cooling Problems

- The named human operator stops immediately if drop, solenoid, cooling, or fan
  behaviour differs from the expected compound state; does not retry until the
  physical state is understood and safe; and records the mismatch scope.

### Emergency Stop Problems

- The named human operator physically powers off the roaster if emergency stop
  does not force heat off, preserves evidence, does not continue the run, and
  treats the result as release-blocking until fixed and revalidated.

## Report Template

Use this structure in issue comments, PR descriptions, or durable validation notes:

```markdown
## Hottop Validation Report

- Date/time:
- Operator:
- Roaster model:
- Firmware/context if known:
- Serial port:
- Baudrate:
- Configured temperature unit:
- Command interval seconds:
- Command:
- Evidence file:
- Non-destructive run or full run:

## Source State

- Branch/commit:
- E3-S4 through E3-S8 marked complete in epic state: yes/no
- Complete current root `AGENTS.md` hardware-free gate before the run, with
  each command and result recorded here:

## Results

- Connection and cleanup: pass/fail/needs review
- Startup safe state: pass/fail/needs review
- Packet parsing and telemetry: pass/fail/needs review
- Temperature units: pass/fail/needs review
- Heat: pass/fail/needs review
- Fan: pass/fail/needs review
- Drop: pass/fail/skipped
- Cooling stop: pass/fail/needs review
- Emergency stop: pass/fail/skipped

## Observations

- Observed temperatures:
- Observed command counters:
- Physical roaster behavior:
- Deviations:
- Abort conditions encountered:

## Decision

- Hardware-ready release label allowed: yes/no
- Follow-up fixes required:
- Final driver decision:
```

## Required Notes

For every manual validation run, record:

- roaster model and firmware context if known
- serial port and configured temperature unit
- what commands were exercised
- whether heat, fan, drop, cooling, and emergency stop behaved as expected
- any uncertainty that keeps the hardware path from being release-ready
- the JSON evidence path, or a note explaining why evidence was not retained

## Do Not

- Agents do not mark Hottop stories complete from mock-only validation or
  improvise control commands against real hardware.
- The named human operator does not run full-validation flags unless intending
  to exercise drop and emergency stop on the connected roaster.
- Agents do not add training, ONNX export, or Hugging Face sync steps here;
  those stay in `coffee-first-crack-detection`.
