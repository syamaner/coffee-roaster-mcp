---
name: hottop-validation
description: Review the guarded manual validation path for Hottop hardware work. Use when planning or verifying hardware-facing changes without overstating what the repo can currently run.
---

# Hottop Validation - RoastPilot

Use this skill for Hottop-facing work and release-readiness review.

## Current Scope

- Hottop lifecycle, command-loop streaming, packet build/parse, control command state, and temperature-unit handling exist behind the `HottopRoasterDriver` boundary.
- The MCP roast-session tools still preserve their current one-session store semantics; do not assume MCP heat, fan, drop, or cooling tools are wired to live Hottop hardware until a later story explicitly does that work.
- The runnable validation entrypoint is `coffee-roaster-mcp hottop-validate`.
- Hardware stories are not complete from mock tests alone.

## Pre-Validation Gates

Complete these gates in order before any Hottop hardware session.

### 1. Story And Source Readiness

- Confirm E3-S4 through E3-S8 are complete in `docs/state/epics/coffee-roaster-mcp-v0.1.md`.
- Confirm the current Hottop validation story or release-readiness task is active, not a broader driver redesign.
- Confirm unit and integration coverage exists in `tests/test_drivers.py` and `tests/test_hottop_validation.py`.
- Confirm fail-closed behavior in `src/coffee_roaster_mcp/drivers.py`.

### 2. Operator And Hardware Readiness

- Confirm the roaster is supervised for the full run.
- Confirm the operator understands emergency stop, bean drop, cooling, and physical power-off expectations.
- Confirm the serial port is known.
- Confirm the config file explicitly sets `roaster.driver: hottop_kn8828b_2k_plus`.
- Confirm the operator accepts that `--include-drop` is irreversible for loaded beans.

### 3. Run Readiness

- Run the non-destructive validation before any full validation.
- Proceed to `--include-drop` only when the roaster is ready for an actual drop check.
- Proceed to `--include-emergency-stop` only when the operator is ready to verify the safety action.

Confirm these source artifacts before running hardware:

- `src/coffee_roaster_mcp/drivers.py`: `HottopRoasterDriver`, command-loop lifecycle, command state, packet build/parse, status read, temperature normalization, and emergency stop.
- `src/coffee_roaster_mcp/hottop_validation.py`: guarded `hottop-validate` runner, JSON evidence shape, skipped-step behavior, and release-label decision.
- `src/coffee_roaster_mcp/cli.py`: `hottop-validate` CLI options and acknowledgement flag.
- `docs/state/epics/coffee-roaster-mcp-v0.1.md`: E3-S4 through E3-S9 status and validation notes.

## Hard Abort Conditions

Stop the validation session immediately if any of these occur:

- The serial port cannot be identified confidently or opens the wrong device.
- The command reports repeated serial write, read, checksum, or command-loop errors.
- The roaster heats when the current step expects heat off.
- Heat, fan, drop, cooling, or emergency stop causes a physical action inconsistent with the expected state.
- Temperature readings are absent, implausible, or jump unexpectedly after the telemetry wait.
- The command loop does not stop cleanly on disconnect.
- The operator loses direct supervision of the roaster.
- Smoke, electrical smell, uncontrolled heat, jammed drop, or unexpected mechanical behavior appears.

Abort procedure:

1. Run or trigger emergency stop if it is safe to do so.
2. Physically power off the roaster if software control is uncertain.
3. Preserve the JSON evidence file and terminal output.
4. Do not continue to later steps in the same run.
5. Record the failed step, observed behavior, and whether the roaster was physically powered off.

## Guarded Validation Command

Use a local config file with an explicit Hottop driver and serial port:

```yaml
roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/cu.usbserial-XXXX
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3
```

Run the non-destructive portion first:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-non-destructive.json \
  --i-understand-this-controls-hardware
```

Run the full validation only when the roaster is supervised and ready for drop and emergency-stop checks:

```bash
coffee-roaster-mcp hottop-validate \
  --config coffee-roaster-mcp.yaml \
  --output docs/validation/hottop-e3-s9-full.json \
  --i-understand-this-controls-hardware \
  --include-drop \
  --include-emergency-stop
```

Do not commit generated validation JSON unless the file is sanitized to remove sensitive data and formatted for long-term storage. Never commit raw serial captures.

## Pass/Fail Criteria

Use this table with the JSON evidence from `hottop-validate` plus direct operator observation.

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

- Check the port with `ls /dev/cu.*` before and after plugging in the USB adapter.
- Confirm the config uses that exact `roaster.port`.
- Confirm no other process has the serial port open.
- Re-run only the non-destructive command until connection and cleanup pass.

### Command Loop Or Write Counters Do Not Advance

- Check `raw.command_loop_running`, `raw.command_loop_iterations`, `raw.command_send_attempts`, `raw.command_write_count`, `raw.command_loop_error_count`, and `raw.last_command_write_size`.
- Treat repeated errors or partial writes as a hard abort.
- Review `HottopRoasterDriver._send_command_frame` and disconnect/write review notes in the active epic before changing code.

### Packet Or Temperature Problems

- Check `raw.status_packet_count`, `raw.ignored_temperature_packet_count`, `raw.status_read_error_count`, `raw.raw_bean_temperature`, `raw.raw_env_temperature`, and `raw.resolved_temperature_unit`.
- If readings are zero during startup but later become plausible, record it as acceptable warmup behavior.
- If values are consistently implausible, stop and review packet offsets, checksum behavior, and configured `temperature_unit`.

### Drop Or Cooling Problems

- Stop immediately if drop, solenoid, cooling, or fan behavior differs from the expected compound state.
- Do not retry full validation until the physical state is understood and the roaster is safe.
- Record whether the mismatch was software state only, physical behavior only, or both.

### Emergency Stop Problems

- Physically power off the roaster if emergency stop does not force heat off.
- Preserve evidence and do not continue the run.
- Treat this as release-blocking until fixed and revalidated.

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
- Required tests before hardware run:
  - pytest:
  - ruff check:
  - ruff format --check:
  - pyright:

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

- Do not mark Hottop stories complete from mock-only validation.
- Do not improvise control commands against real hardware.
- Do not run the full validation flags unless the operator intends to exercise drop and emergency stop on the connected roaster.
- Do not add training, ONNX export, or Hugging Face sync steps here. Those stay in `coffee-first-crack-detection`.
