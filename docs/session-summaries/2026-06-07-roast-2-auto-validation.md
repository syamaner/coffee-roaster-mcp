# 2026-06-07 Roast #2 — E7-S6 Prescribed Auto Validation

Second supervised live roast of the day, completing the E7-S6 / issue #112
evidence exactly as prescribed: the published PyPI `coffee-roaster-mcp==0.1.3`
package launched by Warp via `uvx coffee-roaster-mcp==0.1.3 serve` with
`COFFEE_ROASTER_MCP_CONFIG` pointing at the prescribed YAML — automatic T0
detection enabled in YAML and the sliding-window first-crack detector profile
(threshold 0.6, min_positive_windows 5, confirmation 20.0 s, window 10.0 s,
overlap 0.7, pinned INT8 revision
`b349a919c34b6130472da97c01817be404e4f629`).

Headline results — **zero manual overrides for the entire roast**:

1. **Auto-T0 detected the charge from telemetry**: `beans_added` recorded
   with `source: auto_t0` when the bean probe dropped 30 °C from the tracked
   186 °C preheat max (threshold 25 °C). `mark_beans_added` was never called.
2. **First crack audio-detected under the prescribed sliding-window
   profile**: confidence 0.9074 against the 0.6 threshold, with exactly
   5 positive windows (first positive at window sequence 337, confirmed at
   343, ~18 s of accumulating positives within the 20 s confirmation span).
   `mark_first_crack` was never called.

Companion documents:
`docs/session-summaries/2026-06-07-roast-day-validation.md` (formal report,
both roasts), `docs/session-summaries/2026-06-07-live-roast-test-summary.md`
(roast #1), and transcript excerpts at
[`docs/validation/2026-06-07-live-roast/warp-transcript-excerpts-roast2.md`](../validation/2026-06-07-live-roast/warp-transcript-excerpts-roast2.md).

## Setup Under Test

- Install path: `uvx coffee-roaster-mcp==0.1.3 serve`; configuration solely
  from `/Users/sertanyamaner/roasts/coffee-roaster-mcp.yaml` (single
  `COFFEE_ROASTER_MCP_CONFIG` env var; no per-setting env overrides),
  confirmed live via `get_runtime_config.config_source`
- Driver: `hottop_kn8828b_2k_plus` on `/dev/cu.usbserial-DN016OJ3`, 115200,
  `temperature_unit: auto` (resolved `celsius`)
- Auto-T0: `session.auto_t0_detection_enabled: true`,
  `auto_t0_drop_threshold_c: 25.0` (YAML, per issue #112)
- First crack: `mode: audio`, INT8 ONNX at pinned revision `b349a91…`,
  threshold 0.6, min_positive_windows 5, confirmation 20.0 s, onnx_threads 8
- Audio: microphone `"USB PnP"`, 16 kHz mono, window 10.0 s, overlap 0.7
  (3.0 s hop)
- Session id: `f97fc99b24e948e79954361364257b0e`

## Timeline (UTC, elapsed relative to T0 = auto-detected charge)

| Clock | Elapsed | Event | Detail |
| --- | --- | --- | --- |
| 13:05:09 | −08:16 | `start_roast_session` | FC runtime `pending`, audio live; auto-T0 tracker armed ("Waiting for a valid preheat bean-temperature reading") |
| 13:05:35 | −07:50 | `set_heat 100` / `set_fan 10` | Preheat from 38 °C (drum residual from roast #1); tracker followed preheat max 38 → 89 → 186 °C |
| 13:13:25 | 00:00 | **Auto-T0 — `beans_added`, `source: auto_t0`** | Operator charged beans, no MCP command; BT dropped 186 → 156 °C (30 ≥ 25 threshold); 154 queued pre-T0 detector windows discarded by design |
| 13:14:18 | +00:53 | Turnaround | BT bottomed at 87 °C |
| 13:19:31 | +06:06 | Dry end | BT crossed 150 °C (ET 208 °C) |
| 13:20:14 | +06:49 | `set_fan 30` | Drying/Maillard airflow step; BT 157 °C |
| 13:22:21 | +08:56 | **First crack — audio-detected, sliding-window** | Confidence 0.9074 ≥ 0.6; 5/5 positive windows; window 337 → confirmed 343; phase → `development`; audio runtime self-stopped |
| 13:23:05 | +09:39 | `set_heat 60` / `set_fan 70` | Development control |
| 13:23:15 | +09:49 | `set_heat 30` | RoR moderation |
| 13:23:45 | +10:19 | `set_fan 100` | Finish approach; BT 190 °C |
| 13:24:05 | +10:39 | `drop_beans` | `beans_dropped` + `cooling_started`; heat 0, fan 100, solenoid open, drum off, cooling motor on; drop BT 193 °C |
| 13:28:23 | +14:58 | `stop_cooling` | `cooling_stopped`; phase `complete`, `active: false`, all controls zero, solenoid closed |

## Roast Metrics

| Metric | Value |
| --- | --- |
| Total roast (charge → drop) | 10:39 |
| Charge → first crack | 08:56 |
| Development time | 01:43 |
| DTR | 16.2 % |
| Drop temperature (BT) | 193 °C |
| Cooling duration | 04:19 |
| Telemetry rows | 270 (5 s sampling) |
| Event rows | 5 (`beans_added`, `first_crack_detected`, `beans_dropped`, `cooling_started`, `cooling_stopped`) |
| Manual overrides | 0 |
| Serial/control/telemetry errors | 0 (14,122 status packets) |

## What This Roast Proved Beyond Roast #1

| Mechanism | Roast #1 | Roast #2 |
| --- | --- | --- |
| T0 | manual `mark_beans_added` (payload `{}`) | **auto-T0 from telemetry** (payload `source: auto_t0`, charge 186 °C, drop 30 °C) |
| Detector profile | default (1.0 s window, threshold 0.9, 1 window) | **prescribed sliding-window** (10.0 s / 0.7 overlap / 0.6 / 5 windows) |
| FC confirmation | single positive window | **5 accumulated positives across ~18 s** (337 → 343) |
| Pre-T0 window discard | n/a (manual T0) | **exercised live** (154 windows discarded with designed reason) |
| Config delivery | YAML + matching env overrides | **YAML only via `COFFEE_ROASTER_MCP_CONFIG`** (issue-prescribed) |
| Export timing | post-session | during cooling (Stage 3 of the prescribed flow) |

Both roasts detected first crack by audio at nearly the same elapsed time
(+09:01 and +08:56) and within the operator's by-ear expectation, with no
false positives in either session despite drum, fan, and environment noise —
under two different detector profiles.

## Evidence Artifacts

Committed under
[`docs/validation/2026-06-07-live-roast/session-2/`](../validation/2026-06-07-live-roast/session-2/)
(same explicit operator decision as roast #1): `roast.jsonl` (complete
five-event timeline plus 270 telemetry rows), `roast.csv`, and
`summary.json`. Note: `export_roast_log` ran during the cooling phase per
the prescribed Stage 3 flow, so `summary.json` is the during-cooling
snapshot (`phase: cooling`, cooling-stop timestamp not yet set); the
append-only `roast.jsonl` carries the complete timeline including
`cooling_stopped` and is the authoritative record. Checksums are recorded
in `docs/session-summaries/2026-06-07-roast-day-validation.md`.

## Conclusion

With roast #2, every E7-S6 "Done When" item is satisfied exactly as written
in issue #112. The published, registry-distributed package supports a fully
agent-driven roast in which the runtime itself detects the charge and first
crack, the operator only makes roast-profile decisions, and the complete
session is captured in exportable logs.
