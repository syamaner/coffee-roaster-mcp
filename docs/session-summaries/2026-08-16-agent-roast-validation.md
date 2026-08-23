# Agent Roast Validation — 16 August 2026

## Verdict

Two supervised RoastPilot runs completed on connected Hottop hardware. The
component path covered telemetry and control, automatic T0, first-crack event
handling, advisor decisions, safety evaluation, advisor-triggered drop,
operator-controlled cooling completion, ambient readings, export, and
two-microphone recording.

This is operational evidence from two roasts. It is not a claim that every
hardware configuration is validated, that every roast outcome succeeds, or
that the system is production-ready or fully autonomous.

## Version Boundary

The RoastPilot agent environment pins `coffee-roaster-mcp==0.1.13`; its lock
file records the same version and published artifact hashes. Releases 0.1.14
and 0.1.15 changed component-scope metadata and release infrastructure only.
Version 0.1.16 adds this report and the corresponding README update. None of
those releases change runtime or hardware-control behaviour.

## Evidence Summary

| Evidence | Roast 1 | Roast 2 |
| --- | --- | --- |
| Agent run | `09318196f41041d688d7039d5555c9c2` | `63b8bbdc678f461e92892686caefc9d9` |
| MCP session | `55ea96f2cb484344b921727072832e4f` | `f3aad1725eb74753ac036ad97a58b0cd` |
| Outcome | completed | completed |
| Agent interval (UTC) | 13:27:54–13:49:10 | 14:06:34–14:25:36 |
| Advisor decisions | 10/10 `ok` | 9/9 `ok` |
| Linked advisor safety verdicts | 10/10 `allow` | 9/9 `allow` |
| All safety evaluations | 1,308 `allow` | 1,164 `allow` |
| Executed command events | 12 | 7 |
| Failed command events | 0 | 0 |
| Safety-alert events | 0 | 0 |
| First-crack authority | MCP | operator |
| Drop authority | advisor | advisor |
| Cooling completion authority | operator | operator |
| Ambient captured | 27.04 °C, 37.9%, 1009.47 hPa | 27.82 °C, 36.6%, 1009.29 hPa |
| Audio capture | 2 independent 16 kHz mono streams, 1129.60/1129.75 s | 2 independent 16 kHz mono streams, 917.00/917.25 s |

## Authority Trace

The local agent ledger keeps the decision stages separate:

1. `advisor_decisions` records the provider decision and its status.
2. `advisor_decisions.safety_evaluation_id` identifies the safety evaluation
   that validated the decision.
3. `roast_events` records consequential commands that were actually executed,
   including the authority source.
4. `operator_actions` separately records accepted operator interventions.

In both runs, the last advisor decision set `should_drop: true`, its linked
safety evaluation returned `allow`, and the subsequent `command_executed`
event recorded `drop_beans` with `source: advisor`. Cooling was later stopped
through an accepted operator action. Roast 1 recorded first crack from MCP;
Roast 2 recorded the explicit operator mark instead. This distinction is part of
the evidence: the ledger does not collapse proposal, validation, execution,
and operator authority into one generic agent action.

## MCP Session And Capture Evidence

Each agent run points its exported log directory at the corresponding MCP
session identifier. The agent and MCP start timestamps align to within 50 ms.
The MCP JSONL files contain live Hottop temperature/control telemetry through
cooling completion. The recording manifests identify two independent USB
microphone streams per session and record their sample rate, frame count, and
duration.

Raw WAV files, roast logs, the live SQLite store, and local filesystem paths
remain private and are not committed. The source files used for this report are
identified below by session id and SHA-256 so the local evidence can be checked
without publishing ambient conversation or full operational traces.

| Session | Artifact | SHA-256 |
| --- | --- | --- |
| `55ea96f2cb484344b921727072832e4f` | recording manifest | `796ba5035f63cc9b448608529b274f9c82ebbed1b4c3ae10ebed45443d2cd13f` |
| `55ea96f2cb484344b921727072832e4f` | export summary | `e9283fee7244ef1e27ac2a4c2e283b50eb7f737b0906ac83441bd357bd3ed217` |
| `55ea96f2cb484344b921727072832e4f` | MCP JSONL log | `2b258c7d67fe352608c5059a8df7425e8afe67efaacedd776c1d3d6e1dfaf925` |
| `f3aad1725eb74753ac036ad97a58b0cd` | recording manifest | `4c41523b15b30ed9612cd4d8c9a637501099bf52d9d6b33512a698461b4bb570` |
| `f3aad1725eb74753ac036ad97a58b0cd` | export summary | `4c99ac9130edcb37ab0ffadfe27d1e81febcb7fa40ce8c4d586746886f2ed593` |
| `f3aad1725eb74753ac036ad97a58b0cd` | MCP JSONL log | `d6c126a3325960ada2adcc7a2450b972372de8640fcc1d636b51116e9597f986` |

## Provenance Limit

The current agent rows have a null `mcp_session_id`. This report correlates
each run to its MCP session using the session identifier in `log_dir` and the
aligned start timestamps. That is strong operational evidence, but it is not a
database foreign-key join. Claims that require exact cross-store provenance
should wait for a persisted run-to-session binding or another supervised
hardware run with that binding enabled.
