# RoastPilot Release Workflow

This runbook documents the operator prerequisites and CI release path for
`coffee-roaster-mcp`.

The release workflow is `.github/workflows/release.yml`. It supports two paths:

- Manual dry run through `workflow_dispatch` with `dry_run: true`.
- Live release through a pushed version tag such as `v0.1.0`.

## Operator Prerequisites

Before enabling a live release, the release owner must confirm:

- A PyPI account exists for the release owner.
- The PyPI project name `coffee-roaster-mcp` is owned or reserved by the
  release owner.
- PyPI two-factor authentication is enabled and recovery codes are stored in
  the project owner password manager.
- PyPI Trusted Publishing is configured for:
  - owner: `syamaner`
  - repository: `coffee-roaster-mcp`
  - workflow filename: `release.yml`
  - environment: `release`
  - tag-triggered job: `publish-pypi`
- The GitHub environment named `release` exists and requires manual approval by
  the release owner before deployment jobs can run.
- Protected tag rules block unapproved creation or update of `v*` tags.
- The release tag matches the package version exactly, for example package
  version `0.1.0` must use tag `v0.1.0`.
- No model weights, audio files, roast logs, serial captures, `.env` files, or
  local IDE folders are included in the release artifact.

TestPyPI rehearsal is not enabled in the workflow. If a later story adds it,
the release owner must create a TestPyPI account and configure matching Trusted
Publishing or token fallback before the TestPyPI job is enabled.

## Token Fallback

Trusted Publishing is the intended PyPI publishing path. Token publishing should
only be enabled if Trusted Publishing is unavailable for a release.

If token fallback is required:

- Create a scoped PyPI project token for `coffee-roaster-mcp`.
- Store it as the GitHub environment secret `PYPI_API_TOKEN` on the `release`
  environment, not as a repository-wide secret.
- Use username `__token__` and the `PYPI_API_TOKEN` value only in the PyPI
  publish job.
- Rotate the token immediately after any failed, interrupted, or suspected
  exposed release attempt.
- Delete the token fallback secret after Trusted Publishing is restored.

The checked-in workflow does not reference `PYPI_API_TOKEN`; enabling fallback
requires a deliberate workflow change.

## Dry Run

Run the workflow manually with `dry_run: true`.

The dry run:

- Runs the full test, lint, format, typecheck, and CLI smoke gate.
- Validates release tag, package version, and `server.json` version alignment.
- Builds the wheel and source distribution.
- Confirms both distribution artifacts exist.
- Does not publish to PyPI or the MCP Registry.

## Live Release

After all prerequisites are confirmed:

1. Ensure `server.json.version`, `server.json.packages[0].version`, and
   `coffee_roaster_mcp.__version__` are aligned.
2. Push the matching version tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. Approve the `release` environment deployment in GitHub Actions.
4. Confirm the workflow completes in this order:
   - `checks`
   - `validate-release-metadata`
   - `build-package`
   - `publish-pypi`
   - `publish-mcp-registry`
5. Confirm PyPI shows the expected `coffee-roaster-mcp` version.
6. Confirm the MCP Registry entry for
   `io.github.syamaner/coffee-roaster-mcp` shows the expected PyPI package and
   stdio transport.

MCP Registry publishing runs only after the PyPI publish job succeeds. The
registry job authenticates with GitHub OIDC through `mcp-publisher login
github-oidc` and then publishes `server.json`.
