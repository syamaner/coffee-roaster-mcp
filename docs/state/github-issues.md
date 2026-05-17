# RoastPilot GitHub Issue Index

Repository: `syamaner/coffee-roaster-mcp`

Milestone: `v0.1`

## Epics

- #1 Epic 1: Repo, Packaging, And Developer Workflow
- #2 Epic 2: MCP Runtime And Session Core
- #3 Epic 3: Roaster Abstraction And Hottop Driver
- #4 Epic 4: First-Crack Detection With HF Models
- #5 Epic 5: Roast Metrics And Log Export
- #6 Epic 6: Distribution And MCP Registry Publishing
- #7 Epic 7: End-To-End Validation And Release Readiness

## Epic 1 Stories

- #8 E1-S1: Create standalone repo state and project plan docs
- #9 E1-S2: Add Python package scaffold
- #10 E1-S3: Add CLI basics
- #11 E1-S4: Add config loading from YAML and environment variables
- #12 E1-S5: Add local dev commands
- #13 E1-S6: Add CI for tests and package build
- #14 E1-S7: Add initial README and install/run documentation
- #15 E1-S8: Add repo-local skills or runbooks

## Epic 2 Stories

- #16 E2-S1: Implement stdio MCP server entrypoint
- #17 E2-S2: Implement RoastSession lifecycle
- #18 E2-S3: Implement core event timeline
- #19 E2-S4: Implement core MCP tools
- #20 E2-S5: Implement phase transitions
- #21 E2-S6: Implement emergency stop and fault recording
- #22 E2-S7: Complete thin vertical slice spike
- #77 E2-S8: Add GitHub Actions code coverage reporting

## Epic 3 Stories

- #23 E3-S1: Define RoasterDriver interface and capabilities model
- #24 E3-S2: Implement mock driver
- #25 E3-S3: Implement normalized roaster state model
- #26 E3-S4: Implement Hottop serial connection lifecycle
- #27 E3-S5: Implement Hottop command loop
- #28 E3-S6: Implement Hottop packet build/parse
- #29 E3-S7: Implement Hottop heat, fan, drop, cooling, stop cooling, and emergency stop
- #30 E3-S8: Implement Hottop temperature unit handling
- #31 E3-S9: Run Hottop integration verification spike

## Epic 4 Stories

- #32 E4-S1: Add Hugging Face artifact resolver
- #33 E4-S2: Load INT8 ONNX by default
- #34 E4-S3: Load FP32 ONNX by config
- #35 E4-S4: Support local offline model directory
- #36 E4-S5: Validate required detector artifacts before detection starts
- #37 E4-S6: Add audio capture pipeline
- #38 E4-S7: Add detector adapter
- #97 E4-S8: Add microphone and WAV audio input adapters
- #39 E4-S9: Integrate first crack with session timeline

## Epic 5 Stories

- #40 E5-S1: Implement rolling telemetry buffer
- #41 E5-S2: Compute elapsed roast time
- #42 E5-S3: Compute development time and percent
- #43 E5-S4: Compute 60s bean/env deltas
- #44 E5-S5: Compute bean/env RoR
- #45 E5-S6: Write append-only JSONL roast log
- #46 E5-S7: Export CSV roast log
- #47 E5-S8: Export summary.json
- #48 E5-S9: Add log schema tests

## Epic 6 Stories

- #49 E6-S1: Add PyPI package metadata
- #50 E6-S2: Add README MCP verification string
- #51 E6-S3: Add server.json
- #52 E6-S4: Add version alignment check
- #53 E6-S5: Add release workflow
- #54 E6-S6: Run MCP Registry publishing verification spike
- #55 E6-S7: Document install and hardware setup

## Epic 7 Stories

- #56 E7-S1: Test full mock roast through MCP tools
- #57 E7-S2: Test package install smoke flow
- #58 E7-S3: Test MCP client connection
- #59 E7-S4: Run Hottop manual hardware validation
- #60 E7-S5: Produce v0.1 release checklist

## Standalone Spikes

- #61 SP1: Thin Vertical Slice
- #62 SP2: Hottop Integration Verification
- #63 SP3: MCP Registry Publishing Verification
