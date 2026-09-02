# #194 instrumentation software repair

## Scope

This software-only repair documents and deterministically proves the existing
`first_crack_status` instrumentation: the current capture-run maximum
consecutive overflow streak, latest and maximum single-attempt inference
duration in milliseconds, and inference attempts at or beyond the effective
audio hop.

## Deterministic evidence at this repair head

Focused hardware-free tests prove that the maximum/inference metrics survive
stopped and faulted frozen snapshots and repeated reads, while the existing
trailing-60-second rolling overflow fields still decay. They also prove every
runtime-bearing status serialisation branch carries non-default sentinels, and
that a capture restart clears the current fatal overflow streak without changing
the within-run threshold.

## Explicit non-actions

No Pi, microphone, audio-device, serial, Hottop, threshold, detector, release,
publication, hardware-readiness, operator-characterisation, or acceptance work
occurred. #194 and #157 remain open for later operator characterisation and
acceptance.
