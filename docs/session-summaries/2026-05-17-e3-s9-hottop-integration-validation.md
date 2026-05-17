# E3-S9 Hottop Integration Validation Session

## Scope

This session resumed after `PR #89` for `E3-S8` was merged and issue `#30` was closed. Work started from updated `main` on branch `feature/31-hottop-integration-verification` for issue `#31`, `E3-S9: Run Hottop integration verification spike`.

The story goal was to verify the Hottop driver boundary on real hardware, not redesign the driver or change MCP session semantics. The implementation preserved the existing one-session store and MCP tool behavior. Live hardware validation stayed at the `HottopRoasterDriver` boundary.

## Context Usage

Session usage snapshot supplied by the operator near the end of the story:

- Context window: `29% left (186K used / 258K)`
- 5h limit: `99% left`, resets `17:12`
- Weekly limit: `100% left`, resets `12:12 on 24 May`
- GPT-5.3-Codex-Spark 5h limit: `100% left`, resets `17:47`
- GPT-5.3-Codex-Spark weekly limit: `100% left`, resets `12:47 on 24 May`

This was a high-context hardware validation story because it depended on the accumulated Epic 3 driver decisions from E3-S4 through E3-S8, prior Hottop review fixes, and live operator observations.

## Implementation

Added a guarded validation harness:

- `src/coffee_roaster_mcp/hottop_validation.py`
- CLI entrypoint: `coffee-roaster-mcp hottop-validate`
- Tests: `tests/test_hottop_validation.py`

The command requires `--i-understand-this-controls-hardware`, validates that config uses `hottop_kn8828b_2k_plus` with an explicit port, records JSON evidence, and keeps irreversible or safety-action steps behind explicit flags:

- `--include-drop`
- `--include-emergency-stop`

The command can run a non-destructive pass first, then a full validation pass. It writes structured step evidence including command-loop counters, status-packet counters, raw temperatures, resolved temperature unit, heat/fan/cooling state, solenoid state, and drum state.

## Documentation And State

Updated `.claude/skills/hottop-validation/SKILL.md` from a cautionary checklist into an executable operator runbook. It now includes:

- ordered pre-validation gates
- hard abort conditions
- guarded validation commands
- tabular pass/fail/needs-review criteria
- troubleshooting paths for serial, command-loop, packet/unit, drop/cooling, and emergency-stop failures
- source artifact references
- a consistent report template

The skill was revised after review feedback flagged cognitive-load and ambiguity risks. The useful changes were:

- pre-validation gates were split into story/source readiness, operator/hardware readiness, and run readiness
- pass/fail criteria were converted from many nested sections into one table
- ambiguous wording about sanitized evidence was changed to "sanitized to remove sensitive data and formatted for long-term storage"

Updated `README.md` with Hottop validation commands and clarified that driver-level hardware validation does not mean normal MCP roast-session tools are live Hottop controls yet.

Updated durable state:

- `docs/state/registry.md` now says Epic 3 is complete and points next at `E4-S1`
- `docs/state/epics/coffee-roaster-mcp-v0.1.md` marks `E3-S9` complete and records validation evidence

## Hardware Validation

Detected connected Hottop serial adapter:

- `/dev/cu.usbserial-DN016OJ3`

Temporary config used for validation:

- `/tmp/coffee-roaster-mcp-hottop.yaml`

### Non-Destructive Run

Command:

```bash
./.venv/bin/coffee-roaster-mcp hottop-validate --config /tmp/coffee-roaster-mcp-hottop.yaml --output /tmp/hottop-e3-s9-non-destructive.json --i-understand-this-controls-hardware
```

Evidence:

- `/tmp/hottop-e3-s9-non-destructive.json`
- SHA-256: `eafc565eb11b8db4bc9b813894714f67732b4d57867a970fc2a7dd64a40571e0`

Result:

- connection passed
- stable telemetry passed
- heat `10%` passed, then heat-off returned heat to `0%`
- fan `30%` passed
- cooling start and cooling stop passed
- drop and emergency stop were intentionally skipped
- command loop and status reads had zero errors

### Full Run

Command:

```bash
./.venv/bin/coffee-roaster-mcp hottop-validate --config /tmp/coffee-roaster-mcp-hottop.yaml --output /tmp/hottop-e3-s9-full.json --i-understand-this-controls-hardware --heat-percent 100 --fan-percent 100 --include-drop --include-emergency-stop
```

Evidence:

- `/tmp/hottop-e3-s9-full.json`
- SHA-256: `3756dc9a3481d3859f0767b10940ae481cbef5a4e3544357bd76121d5e0a22a1`

Result:

- connection passed
- stable telemetry passed
- heat `100%` passed, then heat-off returned heat to `0%`
- fan `100%` passed
- drop passed: heat `0%`, drum off, solenoid open, cooling on, fan high
- cooling stop passed: cooling off, solenoid closed, fan `0%`, heat `0%`, drum off
- emergency stop passed: heat `0%`, drum off, solenoid closed, cooling on, fan high
- command loop had `62` successful writes and `0` errors by the emergency-stop step
- telemetry had `191` status packets, `0` ignored temperature packets, `0` status-read errors, and `auto` resolved to `celsius`
- the report set `hardware_ready_release_label_allowed` to `true` for the Hottop driver boundary

### 60-Second Stability Run

The operator reported that the old prototype had a control bug where the machine randomly stopped and started. A supervised stability test was run:

- fan held at `10%`
- heat held at `40%` for 30 seconds
- heat then set to `100%` for 30 seconds
- after one minute, heat and fan were set to zero

Evidence:

- `/tmp/hottop-e3-s9-60s-stability.json`
- SHA-256: `2887c42c301ce08f01b353b40c8ed8ab96137e21baef7f734708c10539e4a4cf`

Result:

- no evidence of the old random stop/start behavior in this one-minute run
- at the 60-second sample: `197` command-loop iterations, `197` send attempts, `197` successful writes, last write size `36`
- `0` command-loop errors
- `607` status packets
- `0` status-read errors
- `auto` resolved to `celsius`

Important operational nuance found during this test:

- plain `set_heat(0)` plus `set_fan(0)` leaves `drum_motor_on: true` after prior nonzero heat
- a follow-up safe-stop sequence using emergency stop, then cooling stop and zero heat/fan, ended with heat `0%`, fan `0%`, cooling off, solenoid closed, drum off, and zero errors

This nuance was recorded in durable epic state because operational stop procedures should use explicit emergency-stop or drop/cooling paths when drum-off is required.

## Review Value

The first useful review input came from the operator and local diagnostics:

- Operator feedback improved the Hottop validation skill from a safety warning into an actionable runbook with evidence criteria and recovery paths.
- Cognitive-load diagnostics caught that the first runbook hardening had too many independent constraints and nested decision paths.
- Ambiguity diagnostics caught vague evidence-sanitization wording.
- Live hardware testing caught the drum-state nuance that mock tests and static review would not have exposed.

After PR `#90` opened, automated GitHub review added a second review round.

Codex review `4305393897` produced two focused code-quality findings:

- failure evidence was not persisted when validation aborted before the final report object was built
- stable telemetry status and evidence could be internally inconsistent because two concurrent `read_state()` calls were used

Copilot review `4305396411` was broader and found operational safety gaps:

- telemetry `needs_review` did not abort before heat, fan, drop, or emergency-stop commands
- invalid heat or fan CLI percentages were validated too late, after some hardware actuation could already have happened
- disconnect failures could mask earlier validation failures and prevent partial evidence from being written
- output-path writability was not preflighted before hardware commands ran
- drop validation was partly masked by starting cooling before drop
- readiness was based on step labels rather than raw diagnostics such as command-loop errors, partial writes, status-read errors, or zero write counts
- the non-destructive path could finish with drum command state still on after prior heat
- the reusable Hottop skill still said E3-S9 must be active even though this PR marks E3-S9 complete
- registry text still said Epic 3 had started after the same PR marked Epic 3 complete
- evidence paths were ephemeral `/tmp` paths without durable checksums

Review quality comparison:

- Codex was concise and high-signal. Its two comments both identified real correctness risks in the validation evidence model.
- Copilot had broader coverage and found more hardware-safety edge cases. Several comments overlapped with Codex on evidence reliability, but Copilot added important independent issues around input validation, output preflight, readiness criteria, drop masking, and state-doc drift.
- The overlap was strongest on evidence integrity: both reviews noticed that the validation record could become unreliable exactly when hardware validation most needs auditability.
- Copilot's suppressed low-confidence notes about `/tmp` evidence were still useful after classification. Rather than committing raw hardware JSON, the durable docs now record SHA-256 checksums for the local evidence files.
- Codex review `4305417406` on the first review-fix commit found two additional high-value issues: the skipped emergency-stop path still sent a hidden emergency-stop command, and command-step readiness accepted stale `command_write_count` values rather than proving fresh write progress per step.

Review response:

- validation now preflights output-path writability before connecting to hardware
- heat and fan percentages plus durations are validated before connecting
- durations must be finite and non-negative
- stable telemetry uses one state snapshot and aborts before control commands unless telemetry passes
- partial failure reports are written before re-raising validation errors
- disconnect failures are recorded without silently losing the original failure context
- full validation now drops before cooling-stop validation so drop behavior is not masked by prior cooling
- non-destructive validation now honors the skipped emergency-stop contract and does not send a hidden emergency-stop command
- each control step now requires `command_write_count` to increase after that specific command before the step can pass
- hardware readiness now requires required steps to pass and no failed steps to appear
- durable state and session summary now include SHA-256 checksums for the hardware evidence files
- skill and registry stale-state wording was corrected

These were high-value fixes because this story is about reducing real hardware uncertainty, not just making code paths pass tests.

## Validation

Local checks after implementation and state updates:

- `./.venv/bin/python -m pytest`: `170 passed`
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: `0 errors`

Live hardware validation:

- non-destructive Hottop run passed
- full Hottop run passed
- 60-second stability run passed

## Handoff

`E3-S9` is complete at the Hottop driver boundary. Epic 3 is complete. The next story is `E4-S1`, issue `#32`: add the Hugging Face artifact resolver.

The key residual boundary is intentional: normal MCP heat, fan, drop, and cooling tools still preserve current one-session store semantics and are not yet wired to live Hottop driver commands. That should be a deliberate future story, not an incidental side effect of E3-S9.
