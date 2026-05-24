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
registry job validates `server.json` against the preview Registry API before
authenticating, authenticates with GitHub OIDC through `mcp-publisher login
github-oidc`, and then publishes `server.json`.

## MCP Registry Verification

The MCP Registry is preview. Before a live v0.1 release, use the current
official registry docs and schema, then repeat the non-destructive checks below:

1. Confirm `server.json` uses the current schema URI:
   `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`.
2. Confirm `server.json.name` is `io.github.syamaner/coffee-roaster-mcp`.
3. Confirm the PyPI package entry uses `registryType: pypi`, identifier
   `coffee-roaster-mcp`, package version `0.1.0`, runtime hint `uvx`, and
   stdio transport.
4. Confirm README contains exactly one PyPI ownership verification marker:
   `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
5. Download the pinned `mcp-publisher` release asset and verify its SHA-256
   before execution. The release workflow uses
   `mcp-publisher_linux_amd64.tar.gz` from `v1.7.9` with SHA-256
   `ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac`.
6. Run `./mcp-publisher validate server.json` before authenticating.

E6-S6 verified the template and flow non-destructively:

- `server.json` validated against the downloaded `2025-12-11` JSON schema.
- `./mcp-publisher validate server.json` returned
  `server.json is valid` against the preview Registry API.
- The pinned Linux workflow asset checksum matched the expected SHA-256.
- The same `mcp-publisher` version's Darwin arm64 build showed the publishing
  flow as `login github-oidc` followed by `publish server.json`.
- `mcp-publisher login github-oidc` fails outside GitHub Actions without
  `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, confirming this authentication path must
  run from a job with `id-token: write`.
- `https://pypi.org/pypi/coffee-roaster-mcp/json` returned `Not Found` before
  the first live release.
- The Registry search API returned no existing listing for
  `io.github.syamaner/coffee-roaster-mcp` before the first live release.

The first destructive registry operation is `./mcp-publisher publish server.json`.
Execute it only after:

- the matching package version exists on production PyPI
- the PyPI package README includes the exact `mcp-name` verification marker
- the GitHub `release` environment has been approved
- `./mcp-publisher validate server.json` has passed

Expected outcome: the Registry accepts the metadata and the search endpoint
`https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`
returns a listing whose package identifier is `coffee-roaster-mcp` and whose
transport is `stdio`.

Risk: Registry preview behavior, schema validation, package verification, or
listing data can change or reset before general availability. A failed publish
after PyPI succeeds leaves PyPI live without Registry discoverability and should
be retried only after checking Registry status and confirming no partial
version entry was created.
