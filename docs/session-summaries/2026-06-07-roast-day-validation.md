# 2026-06-07 Roast-Day End-To-End Validation

This session captures the first live end-to-end production-path validation:
the published `coffee-roaster-mcp==0.1.3` package installed through the MCP
Registry `uvx` runtime hint into Warp, driving a connected Hottop
KN-8828B-2K+ with live audio first-crack detection during a real supervised
manual roast.

This complements, and does not replace, the E3-S9 driver-boundary validation
recorded in `docs/state/epics/coffee-roaster-mcp-v0.1.md`.

## Environment

- Date/time: 2026-06-07
- Operator: syamaner (supervised at the roaster for the full run)
- Roaster model: Hottop KN-8828B-2K+
- Serial port: `/dev/cu.usbserial-DN016OJ3`
- Baudrate: 115200
- Configured temperature unit: `auto`
- Command interval seconds: 0.3
- MCP client: Warp agent (stdio)
- Install path under test: `uvx coffee-roaster-mcp==0.1.3 serve` (PyPI,
  matching MCP Registry entry `io.github.syamaner/coffee-roaster-mcp` v0.1.3)
- Configuration: resolved from `coffee-roaster-mcp.yaml` in the Warp
  `working_directory` (`~/roasts`) — confirmed by the `config_source` field
  of the live `get_runtime_config` response — with matching
  environment-variable overrides also present in the Warp MCP server entry
- Microphone: `USB PnP Audio Device` (selected by sounddevice name substring
  `"USB PnP"`); pre-roast check opened the device at 16 kHz mono with live
  signal
- First-crack model: `syamaner/coffee-first-crack-detection`, INT8 ONNX,
  pinned revision `b349a919c34b6130472da97c01817be404e4f629`, pre-cached in
  the local Hugging Face cache before the roast (no mid-roast network
  dependency)

## Source State

- Repository branch/commit at validation time: `1b998dd` (working tree clean)
- Note: the live roast exercises the published 0.1.3 PyPI artifact; the
  quality gates below were run on the repository working tree at the commit
  above.
- E3-S4 through E3-S9 marked complete in epic state: yes (E3-S9 full guarded
  validation, including drop and emergency stop, previously passed on the
  same serial port)
- Quality gates at commit `1b998dd`:
  - pytest: pass (all tests)
  - ruff check: pass ("All checks passed!")
  - ruff format --check: pass (33 files already formatted)
  - pyright: pass (0 errors, 0 warnings, 0 informations)
  - CLI smoke: pass (`coffee-roaster-mcp --version` -> 0.1.3)

## Pre-Roast Guarded Driver Validation (today, published package)

- Command: `uvx coffee-roaster-mcp==0.1.3 hottop-validate
  --config ~/roasts/coffee-roaster-mcp.yaml
  --output ~/roasts/evidence/hottop-2026-06-07-roast-day.json
  --i-understand-this-controls-hardware --include-drop
  --include-emergency-stop`
- Run type: full (drop and emergency stop included; empty drum, supervised)
- Run window: 2026-06-07T11:49:26Z to 2026-06-07T11:49:45Z
- Evidence file: [`docs/validation/2026-06-07-live-roast/hottop-2026-06-07-roast-day.json`](../validation/2026-06-07-live-roast/hottop-2026-06-07-roast-day.json)
  (committed by explicit operator decision for this validation story)
- Evidence file SHA-256:
  `c04ebabd72e4d2f848571f239d4459406d1bee7bbf7d63c7784dc361266bb73a`
- Result summary: all 8 steps `passed` (connect, stable_telemetry, heat 10%,
  heat_off, fan 30%, drop, cooling_stop, emergency_stop);
  `hardware_ready_release_label_allowed: true`; 191 status packets with 0
  read errors and 0 command-loop errors; `auto` temperature unit resolved to
  `celsius` with plausible 24.0 C bean/env readings; drop produced the
  expected compound state (heat 0, drum off, solenoid open, cooling on, fan
  100); emergency stop forced the safe state (heat 0, drum off, cooling on,
  fan 100). This is the first full guarded validation executed against the
  published PyPI artifact rather than a development checkout.

## Live Roast Through Warp (MCP end-to-end)

- Session id: `c570768137504d30b6a917b0cba42085`
- Verification step (get_server_info / get_runtime_config matched expected
  driver, port, unit, first-crack mode, input device): yes
- First-crack runtime status at preheat: `pending` (audio capture running and
  active on the configured microphone; not `unavailable`); screenshot
  committed at [`docs/validation/2026-06-07-live-roast/warp-preheat-fc-pending.png`](../validation/2026-06-07-live-roast/warp-preheat-fc-pending.png)
- Charge (mark_beans_added) recorded: yes — `beans_added` event row written
  at 2026-06-07T12:09:10Z; charge turnaround visible in telemetry (BT 129 C
  reading at charge descending toward turnaround with ET ~221 C); screenshot
  committed at [`docs/validation/2026-06-07-live-roast/warp-beans-in-t0.png`](../validation/2026-06-07-live-roast/warp-beans-in-t0.png)
- Heat/fan control via set_heat / set_fan during roast: yes — preheat
  set_heat 100 / set_fan 10 confirmed in Warp and reflected in sampled
  telemetry; physical heat and drum response observed, with bean/env
  temperatures climbing from 24 C within ~2 minutes of the heat command
  (normal Hottop sensor lag). Note: after start_roast_session the server
  correctly streamed safe-zero control packets until the first explicit
  set_heat call — heat is never implied by session start. The operator
  prompt was updated to supply default preheat percentages so "preheat"
  alone cannot be mistaken for a heat command.
- First crack: AUDIO-DETECTED by the released ONNX detector during the live
  roast — no manual override used. `first_crack_detected` event at
  2026-06-07T12:18:11Z (9m 01s after T0) with payload
  `source: first_crack_detector`, confidence 0.9066 against threshold 0.9,
  window sequence 1175, model `onnx/int8/model_quantized.onnx` at pinned
  revision `b349a919c34b6130472da97c01817be404e4f629`. The detection time
  matched the operator's by-ear expectation and Warp announced it from a
  routine get_roast_state poll. Screenshot retained at
  [`docs/validation/2026-06-07-live-roast/warp-first-crack-detected.png`](../validation/2026-06-07-live-roast/warp-first-crack-detected.png).
- Development phase: heat 100->60->20 and fan 30->60->100 steps applied via
  set_heat/set_fan and confirmed; session phase transitioned to
  `development` after FC.
- Drop (drop_beans) and cooling behavior: yes — `beans_dropped` and
  `cooling_started` events at 2026-06-07T12:19:47Z with the expected
  compound state (heat 0, fan 100, cooling on); drop at 10:37 elapsed,
  drop temp 198 C, development 01:35, DTR 15.0%. Post-drop set_heat 0 /
  set_fan 0 honored while the cooling cycle stayed active.
- Cooling stop: yes — `cooling_stopped` event at 2026-06-07T12:25:50Z after
  6m 03s of cooling.
- Export (export_roast_log): exercised post-session against the still
  in-process session; produced `roast.csv` and `summary.json` with complete
  first-crack model metadata (repo, pinned revision, precision, confidence
  0.9066, artifact filenames), all lifecycle timestamps, final metrics, and
  `phase: complete`. The append-only `roast.jsonl` (273 telemetry rows, 5
  event rows) was unchanged by the export.
- Exported artifacts (committed by explicit operator decision under
  `docs/validation/2026-06-07-live-roast/session/`; original local paths
  below):
  - `roast.jsonl`:
    `~/roasts/logs/roasts/c570768137504d30b6a917b0cba42085/roast.jsonl`,
    SHA-256
    `30ccb0a7151fc12185602de26c06e47846f375e5523a2ed687997ecfc6701044`
  - `roast.csv`: same directory, SHA-256
    `95b38e90865385536f2a9564170a8b8770e21e67ca7cd9d6cf3e0476e05a7278`
  - `summary.json`: same directory, SHA-256
    `2ce436efbb32d3acaeaa0b65c0636cad5931befcf0a7349f4a1608e2f282efb0`
- Roast metrics (from event/telemetry log and Warp agent summary): total
  roast 10:37, charge-to-FC 09:01, development 01:35, DTR 15.0%, drop temp
  198 C, cooling 06:03.
- Warp transcript retained: screenshots committed under
  `docs/validation/2026-06-07-live-roast/` (preheat, charge, drying,
  first-crack detection, development controls, drop/cooling, roast summary)
  and embedded in the live-roast test summary; curated transcript excerpts
  with expanded tool results (runtime config, FC payload, audio counters,
  export response) at
  [`docs/validation/2026-06-07-live-roast/warp-transcript-excerpts.md`](../validation/2026-06-07-live-roast/warp-transcript-excerpts.md).
- Full timeline: see
  `docs/session-summaries/2026-06-07-live-roast-test-summary.md`.

## Roast #2 — E7-S6 Prescribed Auto Validation (same day)

A second supervised roast (session `f97fc99b24e948e79954361364257b0e`,
13:05–13:28 UTC) completed the E7-S6 evidence exactly as prescribed in issue
#112, with zero manual overrides:

- Configuration solely from the prescribed YAML via
  `COFFEE_ROASTER_MCP_CONFIG` (confirmed by `config_source`); YAML-enabled
  auto-T0; sliding-window detector profile (threshold 0.6,
  min_positive_windows 5, confirmation 20.0 s, window 10.0 s, overlap 0.7).
- Auto-T0 recorded `beans_added` with `source: auto_t0` at
  2026-06-07T13:13:25Z (charge 186 C, detected 156 C, drop 30 C over the
  25 C threshold); `mark_beans_added` never called. The runtime discarded
  154 queued pre-T0 detector windows with the designed reason.
- First crack audio-detected at 2026-06-07T13:22:21Z (+08:56) with
  confidence 0.9074 over the 0.6 threshold and exactly 5 positive windows
  (window sequence 337 confirmed by 343); `mark_first_crack` never called.
- Full lifecycle: drop at +10:39 (BT 193 C, dev 01:43, DTR 16.2%), cooling
  stop at +14:58, phase `complete`; export produced all three artifacts;
  14,122 status packets, 0 errors of any kind.
- Full detail: `docs/session-summaries/2026-06-07-roast-2-auto-validation.md`
  and
  [`docs/validation/2026-06-07-live-roast/warp-transcript-excerpts-roast2.md`](../validation/2026-06-07-live-roast/warp-transcript-excerpts-roast2.md).
- Committed artifacts under `docs/validation/2026-06-07-live-roast/session-2/`:
  - `roast.jsonl` SHA-256
    `b49ebdc3689cc4a3c7519ed727886cf051304ab8fa63675d8dcd891e9b15d1ef`
  - `roast.csv` SHA-256
    `71e8a9c4bd69d147a73736c5206cb14d9a83f9e7fb185963472ac648da989f8e`
  - `summary.json` SHA-256
    `c12cbfe7c9e85291f886020e8bd4bfd97094964c1befd0e728b8214209adf801`

## Observations

- Observed temperatures: plausible throughout — 24 C ambient at connect,
  charge reading 129 C descending to 92 C turnaround, dry end at BT 150 C
  around +05:51, FC at BT ~181 C, drop at BT 198 C; post-drop drum-probe
  residual peak 202 C is expected sensor behavior with beans in the cooling
  tray.
- Physical roaster behavior vs commanded state: consistent at every step —
  drum engaged with first nonzero heat, fan steps audible/visible, drop and
  cooling compound states matched telemetry, zeroed controls honored while
  cooling stayed active.
- Detector behavior: window sequence reached 1175 at detection; counters at
  FC were emitted 484 / processed 478 / dropped 698 / queued 4. The drops
  accumulated during the long preheat between sparse polls (queue
  backpressure: background capture queues windows, get_roast_state drains
  them) and had no adverse effect. Single confident positive (0.9066 >= 0.9
  threshold) at +09:01, within the operator's by-ear expectation; no false
  positives during preheat, drying, or Maillard despite drum/fan noise. The
  runtime stopped audio capture itself after detection
  (`audio_running: false`).
- Deviations: none affecting hardware safety. One operator-side message went
  to the shell instead of the Warp agent (`zsh: command not found: call`) —
  harness usability note only.
- Abort conditions encountered: none.

## Known Findings

- `server.json` (v0.1.3) declares the PyPI package without
  `packageArguments`, so a purely registry-driven client launch runs
  `uvx coffee-roaster-mcp` without the required `serve` subcommand and exits
  at argument parsing. Warp installs must add `serve` explicitly. Follow-up:
  add `packageArguments: [{"type": "positional", "value": "serve"}]` in the
  next release.
- `get_runtime_config` (`RuntimeConfigSnapshot`) does not expose the
  first-crack detector profile fields (`confidence_threshold`,
  `min_positive_windows`, `confirmation_window_seconds`), the pinned model
  `revision`, or the audio capture settings (`source`, `input_device`,
  `sample_rate`, `window_seconds`, `overlap`, `hop_seconds`). The E7-S6
  preflight checklist asks an MCP client to confirm exactly those values, so
  a client currently cannot verify them through the tool surface; the
  `first_crack_detected` event payload is the only MCP-visible record of the
  active profile. Follow-up: extend `RuntimeConfigSnapshot` with these
  fields in a future release.

## Decision

- End-to-end production path (registry install -> Warp -> live Hottop ->
  audio first crack -> export) validated: yes. The published 0.1.3 package,
  installed the way an end user would install it, completed a full
  supervised manual roast with audio-detected first crack, a successful
  post-session export, and zero control, serial, or telemetry faults.
- E7-S6 prescribed-profile coverage: CLOSED by roast #2 the same day. The
  second roast ran auto-T0 (YAML-configured) and the prescribed
  sliding-window detector profile end to end with zero manual overrides;
  see the Roast #2 section above. E7-S6 / issue #112 evidence is complete
  exactly as written.
- Follow-up fixes:
  1. `packageArguments: [{"type": "positional", "value": "serve"}]` added to
     `server.json` in-repo (ships with the next release) so purely
     registry-driven clients launch correctly.
  2. Extend `RuntimeConfigSnapshot` with detector-profile/revision/audio
     fields (recorded Known Finding; future release).
  3. None for the driver, session, detector, or logging paths from either
     session.
