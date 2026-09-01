---
name: release-registry
description: Prepare or audit the current PyPI and MCP Registry release workflow for RoastPilot without publishing.
---

# Release Registry - RoastPilot

Use this skill only for read-only package and registry release preparation or
audit. Agents do not tag, approve environments, publish, or perform live
verification.

## Current Scope

- `v0.1.16` is published on PyPI and in the MCP Registry.
- `docs/release.md` is the current release authority and preserves historical
  outcomes.
- Tags, release-environment approval, publication, and live verification are
  human-operator-only. The intended future `0.2.0` line is not authority to
  tag or publish.

## Release Targets

- Package name: `coffee-roaster-mcp`
- MCP Registry name: `io.github.syamaner/coffee-roaster-mcp`
- Display title: `RoastPilot`

## Human-Owned Release Flow

1. Confirm the package version is intentional.
2. Build and validate sdist plus wheel artifacts.
3. Confirm README contains the MCP verification string when the release story adds it.
4. Confirm `server.json` exists and matches the package version when the registry metadata story lands.
5. The human operator publishes the package to PyPI.
6. The human operator verifies it in a clean environment.
7. The human operator runs published-package mock-safe smoke checks.
8. The human operator publishes MCP Registry metadata.
9. The human operator verifies the registry listing.

## Current Review Checklist

Use this skill to review readiness only:

- package name is still `coffee-roaster-mcp`
- registry name is still `io.github.syamaner/coffee-roaster-mcp`
- release docs do not imply hardware-ready support without Hottop validation
- default first-crack mode remains `disabled` so package install smoke does not require audio or model download
- version alignment across package metadata, tags, and `server.json` remains part of the release plan

## Mock-Safe Published Smoke Target

For a human-owned published-package verification, the minimum smoke target is:

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
- Do not claim a future publication is verified without the human-owned
  `docs/release.md` checks and resulting live evidence.
- Do not label a release hardware-ready before the Hottop manual validation path passes.
