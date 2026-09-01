---
name: pi5-validation
description: Prepare or audit an operator-controlled Raspberry Pi 5 validation session for coffee-roaster-mcp. Use when a human needs the D183 read-only capture, acceptance, and private-evidence boundary documented without operating hardware.
---

# Pi 5 Validation

Prepare a human-owned session plan and audit its resulting manifest. This skill
does not operate a Pi, recording device, microphone, Hottop, serial device, or
MCP server. Do not start, stop, configure, or delete anything.

## Gate the session

Require a named human operator and an explicit pre-ratified session record.
Stop if any required identity, threshold, resource limit, or safety authority
is missing. The human alone performs physical setup, Hottop commands, aborts,
and emergency stops; collection remains read-only and must not actuate hardware.

Freeze before the session begins:

- Pi image/host identity, package/runtime identity, config identity, model and
  artefact identities, and the sole primary-stream device identity.
- D183 provisional resource limits exactly as pre-ratified for the session.
  Do not invent, raise, or tune them during or after collection.
- Separately pre-ratified `N` (minimum valid-observation count) and `X`
  (maximum integrity or resource-limit exception count). Do not substitute one
  for the other or relax either after collection starts.

## Keep capture bounded

Keep recording default-off. When the human explicitly enables evidence
collection, admit exactly one primary stream and no additional recording device
or auxiliary stream. Preserve the frozen identities, collect observations
read-only, and do not send actuator, driver, Hottop, or MCP write commands.

The human conducts two separate 30-minute stages: first a supervised
characterisation stage, then a supervised confirmation stage. Final acceptance
may use only a continuous final-acceptance interval of at least 20 minutes from
the confirmation stage. A pause, identity change, extra stream, threshold
change, resource-limit breach, or human abort fails the affected stage closed.

## Accept and retain evidence

Use all-or-nothing acceptance: accept only if both full 30-minute stages, the
continuous 20-minute final interval, `N`, `X`, frozen identities, and the D183
provisional limits all pass. Otherwise record a non-acceptance result; never
claim partial acceptance or infer missing evidence.

Keep evidence private. The human retains it with a private SHA-256 manifest
that identifies the frozen inputs and resulting files. Do not commit, publish,
upload, auto-delete, or expose recordings, device identifiers, logs, hashes, or
other private evidence. Escalate retention, publication, threshold, hardware,
or safety decisions to the human operator.
