---
name: release-registry
description: Review the staged PyPI and MCP Registry release workflow for RoastPilot. Use when preparing distribution work and keep current prereqs explicit until release stories land.
---

# Release Registry - RoastPilot

Use this skill for package and registry release preparation.

## Current Scope

- This is a staged runbook for future release work.
- `server.json`, registry publishing automation, and full release verification land in later Epic 6 stories.
- Do not present this workflow as fully runnable until those stories are complete.

## Release Targets

- Package name: `coffee-roaster-mcp`
- MCP Registry name: `io.github.syamaner/coffee-roaster-mcp`
- Display title: `RoastPilot`

## Planned Release Flow

1. Confirm the package version is intentional.
2. Build and validate sdist plus wheel artifacts.
3. Confirm README contains the MCP verification string when the release story adds it.
4. Confirm `server.json` exists and matches the package version when the registry metadata story lands.
5. Publish the package to PyPI.
6. Install the published package in a clean environment.
7. Run mock-safe smoke checks against the published package.
8. Publish MCP Registry metadata.
9. Verify the registry listing renders the expected package and install metadata.

## Current Review Checklist

Until Epic 6 lands, use this skill to review readiness only:

- package name is still `coffee-roaster-mcp`
- registry name is still `io.github.syamaner/coffee-roaster-mcp`
- release docs do not imply hardware-ready support without Hottop validation
- default first-crack mode remains `disabled` so package install smoke does not require audio or model download
- version alignment across package metadata, tags, and future `server.json` remains part of the release plan

## Mock-Safe Published Smoke Target

When published package verification becomes available, the minimum smoke target remains:

```bash
coffee-roaster-mcp --help
coffee-roaster-mcp --version
python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

Expected bootstrap output:

```text
mock disabled int8
```

## Do Not

- Do not add Hugging Face model sync, model export, model cards, or dataset cards to this release workflow.
- Do not claim MCP Registry publishing is verified before `server.json`, PyPI verification, and `mcp-publisher` stories are complete.
- Do not label a release hardware-ready before the Hottop manual validation path passes.
