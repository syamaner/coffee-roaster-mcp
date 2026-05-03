---
name: hottop-validation
description: Review the guarded manual validation path for Hottop hardware work. Use when planning or verifying hardware-facing changes without overstating what the repo can currently run.
---

# Hottop Validation - RoastPilot

Use this skill for Hottop-facing work and release-readiness review.

## Current Scope

- Hottop hardware support is not implemented yet.
- This workflow is a safety-first validation checklist, not a runnable hardware procedure.
- Hardware stories are not complete from mock tests alone.

## Pre-Validation Gates

Before any Hottop hardware session:

- Confirm the relevant Hottop implementation stories are complete.
- Confirm unit and integration coverage exists for the touched behavior.
- Confirm current defaults still fail closed when behavior is uncertain.
- Confirm the active operator understands emergency stop and cooling expectations.

Do not proceed on hardware if any of these remain unclear:

- command-loop cadence
- packet format or checksum behavior
- temperature unit interpretation
- drop behavior
- cooling behavior
- emergency stop behavior

## Manual Validation Checklist

Use this checklist once the Hottop driver exists:

1. Verify connection and disconnect lifecycle do not leave command loops running.
2. Verify startup state is conservative: heat off, expected fan state, no unintended cooling or drop action.
3. Verify packet parsing against known-good captures and implausible input cases.
4. Verify temperature readings are plausible and unit handling matches the configured mode.
5. Verify heat and fan changes respect supported ranges and safe defaults.
6. Verify drop behavior matches the intended compound action.
7. Verify cooling start and cooling stop leave the roaster in the expected state.
8. Verify emergency stop turns heat off, records a fault or stop event, and preserves enough state for diagnosis.
9. Record the exact roaster model, serial settings, observed temperatures, and any deviations from expected behavior.

## Required Notes

For every manual validation run, record:

- roaster model and firmware context if known
- serial port and configured temperature unit
- what commands were exercised
- whether heat, fan, drop, cooling, and emergency stop behaved as expected
- any uncertainty that keeps the hardware path from being release-ready

## Do Not

- Do not mark Hottop stories complete from mock-only validation.
- Do not improvise control commands against real hardware.
- Do not add training, ONNX export, or Hugging Face sync steps here. Those stay in `coffee-first-crack-detection`.
