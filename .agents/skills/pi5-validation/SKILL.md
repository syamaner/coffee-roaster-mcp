---
name: pi5-validation
description: Prepare or audit an operator-controlled Raspberry Pi 5 validation session for coffee-roaster-mcp. Use when a human needs the D183 read-only capture, acceptance, and private-evidence boundary documented without operating hardware.
---

# Pi 5 Validation

Prepare a human-owned D183 plan or audit supplied evidence. This skill never
operates a Pi, recording device, microphone, Hottop, serial device, or MCP
server: the collector is read-only and no hardware action is authorised.
Multi-stream Pi capture is out of #157/#194 and requires a later decision. Any
collector alarm requires human-operator action; the collector and this skill do
not actuate or direct a response.

## Characterise before accepting

One supervised, no-actuation 60-minute characterisation session is required:
the first 30 minutes run real ONNX inference with recording off; the next 30
minutes enable primary recording against the warmer system. Hottop serial
telemetry is read-only. Recording is default-off; validation explicitly sets
`recording.enabled` and `recording.autocapture` true, uses exactly one primary
stream, and configures no additional recording device.

Only after characterisation may the human ratify `N` and `X`, which both lock
before final acceptance. `N` is the non-negative permitted maximum consecutive
audio-overflow count and is below the unchanged fatal streak limit of 30. `X`
is permitted peak trailing-60-second lost-audio milliseconds. They are not
valid-observation or exception counts and cannot be loosened from the run
outcome.

Freeze this D183 identity set; any material change restarts characterisation:
Pi board/RAM; 64-bit OS/kernel/firmware; PSU/cooling; storage; Python;
candidate wheel SHA-256; dependency inventory; ONNX/model/config hashes; ALSA
primary-device identity; and Hottop serial identity.

## Accept separately and fail closed

Final acceptance is a separate supervised live session of at least 20
continuous minutes, not an interval inside characterisation. It is
all-or-nothing for separate #157/#194 matrices: one immutable run may evidence
both only when both pass; failure of either closes neither.

Evidence must include capture-run-lifetime `max_consecutive_overflow_count`,
aggregated with `max()` across active inputs and reset with total overflow
count; a frozen final snapshot; no unexpected capture restart; and monotonic
last/max inference duration plus inference-overrun count. Acceptance requires:

- no fatal or microphone error; zero dropped windows and inference overruns;
  maximum inference below the seven-second hop; and no growing queue;
- complete primary WAV and sidecar; temperature abort/alarm at 80 C; clean
  current and sticky under-voltage/capping/throttling bits from validation boot;
- `MemAvailable >=512 MiB`, disk `>=2 GiB` before and `>=1 GiB` throughout.

Any resource-limit change after characterisation requires fresh qualifying
evidence. Stop and report non-acceptance on any missing or failing condition;
do not infer partial acceptance.

## Private evidence and retention

Keep full evidence private, SHA-256-manifested, and copy it after the session
to `/Users/sertanyamaner/Documents/RoastPilot/validation-evidence/pi5/<run-id>/`.
Keep the Pi copy until laptop verification; auto-delete neither. Retain both
through #157/#194 closure, verified MCP minor release, and
roastpilot-agent#137 completion. Git may contain only a sanitised report and
the private-manifest hash. Human operators retain all physical, threshold,
retention, and release authority.
