# RoastPilot Release Workflow

This runbook documents the operator prerequisites and CI release path for
`coffee-roaster-mcp`.

The release workflow is `.github/workflows/release.yml`. It supports two paths:

- Manual dry run through `workflow_dispatch` with `dry_run: true`.
- Live release through a pushed version tag such as `v0.1.3`.

## Changelog

### 0.1.6

- fix(#163): `drop_beans` keeps the drum running so beans eject; `stop_cooling`
  stops the drum. On a real Hottop the previous drop stopped the drum the instant
  the chute opened, trapping roughly half the charge. The rotating drum now
  tumbles beans out through the open chute, and `stop_cooling` (the end-of-roast
  action) is what finally stops the drum. `emergency_stop` is unchanged: it keeps
  the drum stopped and the solenoid closed to cool the beans in place.

## Operator Prerequisites

Before enabling a live release, the release owner must confirm:

- A PyPI account exists for the release owner.
- The PyPI project name `coffee-roaster-mcp` is owned or reserved by the
  release owner.
- `docs/install-and-hardware-setup.md` has been reviewed for the target release
  install mode, Hottop configuration, Hugging Face model configuration, offline
  model path, and log output paths.
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
  version `0.1.3` must use tag `v0.1.3`.
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

## v0.1 Release Checklist

Use this checklist for the next v0.1 release candidate from updated `main`.
The current published package and registry metadata are `0.1.2`; the latest
E7-S5a first-crack replay evidence uses released Hugging Face INT8 artifacts
from `syamaner/coffee-first-crack-detection` pinned to revision
`b349a919c34b6130472da97c01817be404e4f629`.

### Required Tests And Checks

Run the normal local gate before tagging:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
coffee-roaster-mcp --help
coffee-roaster-mcp --version
```

Confirm CI passes on the release-candidate PR:

- PR `Checks` job: tests with coverage, lint, format check, typecheck, and CLI
  smoke checks.
- PR `Build Package` job: distribution build, built-wheel install smoke, and
  artifact upload.

Run release dry-run validation before pushing a live tag when the candidate
contains release metadata or workflow changes:

```bash
gh workflow run release.yml -f dry_run=true
```

### Package Build Validation

Build and smoke-test the local distributions before tagging:

```bash
python -m build
python .github/scripts/smoke_install_built_wheel.py
```

The smoke install must use a clean virtual environment, install the built
wheel from `dist/`, run installed `coffee-roaster-mcp --help`, run installed
`coffee-roaster-mcp --version`, and assert the installed default config is
`mock disabled int8`.

### Version Alignment

Before tagging, confirm these values all match the release version:

- `src/coffee_roaster_mcp/__init__.py` `__version__`
- `server.json.version`
- `server.json.packages[0].version`
- the pushed tag name, using `v{version}`
- installed CLI output from `coffee-roaster-mcp --version`

The current release candidate aligns all package and registry metadata at
`0.1.6`. A later release candidate must update all three checked-in version
fields in the same PR before tagging.

### Hugging Face First-Crack Artifact Pin

For the v0.1 release candidate, record the first-crack artifact pin in the
release notes or release PR:

- repo: `syamaner/coffee-first-crack-detection`
- revision: `b349a919c34b6130472da97c01817be404e4f629`
- precision: `int8`
- required artifacts:
  - `onnx/int8/model_quantized.onnx`
  - `onnx/int8/preprocessor_config.json`

Do not select a newer model revision during release unless the change is
deliberate, documented in the release PR, and revalidated with the E7-S5a
labelled WAV replay path. Model training, ONNX export, Hugging Face sync,
model cards, and dataset cards remain in `coffee-first-crack-detection`.

### PyPI Publish Steps

1. Merge the release-candidate PR to `main`.
2. Update and verify local `main`:

   ```bash
   git checkout main
   git pull --ff-only origin main
   ```

3. Confirm the tag does not already exist locally or remotely.
4. Create and push the matching protected version tag:

   ```bash
   git tag v0.1.3
   git push origin v0.1.3
   ```

5. Approve the GitHub `release` environment deployment for `Publish PyPI`.
6. Confirm production PyPI exposes the expected `coffee-roaster-mcp` version,
   wheel, sdist, README, and project URLs.
7. Run a published-package smoke after the package index exposes the version:

   ```bash
   uvx --refresh-package coffee-roaster-mcp --from coffee-roaster-mcp==0.1.3 coffee-roaster-mcp --version
   ```

Use the actual candidate version in tag and smoke commands. The `0.1.3`
commands above document the next release candidate.

### MCP Registry Publish Steps

The release workflow publishes the MCP Registry entry only after PyPI publish
succeeds.

1. Confirm `server.json` contains the expected schema URI, Registry name
   `io.github.syamaner/coffee-roaster-mcp`, PyPI identifier
   `coffee-roaster-mcp`, package version, runtime hint `uvx`, and stdio
   transport.
2. Confirm README contains exactly one verification marker:
   `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
3. Confirm the workflow validates `server.json` with the pinned
   `mcp-publisher` before authenticating.
4. Approve the GitHub `release` environment deployment for
   `Publish MCP Registry`.
5. Confirm the Registry search endpoint returns
   `io.github.syamaner/coffee-roaster-mcp` with the expected package version,
   `runtimeHint: uvx`, `transport.type: stdio`, and `isLatest: true`.

Do not run a manual live Registry publish from a laptop. The intended live path
uses GitHub Actions OIDC from the release workflow.

### GitHub Release Steps

After PyPI and MCP Registry publishing pass:

1. Create or update the GitHub Release for the tag.
2. Summarize the user-visible release contents, validation status, and known
   boundaries.
3. Include links to the PyPI release, MCP Registry search result, release
   workflow run, and relevant validation/session summary.
4. Mention the pinned Hugging Face revision used for first-crack artifacts.
5. State whether the release is mock-safe only, hardware-validated, or
   hardware-ready according to the policy below.

### Hardware-Ready Labeling Policy

Do not apply a hardware-ready release label from release checklist completion
alone.

A release may be described as mock-safe when default install, package smoke,
MCP client, and mock roast validation pass without hardware or model download.
The current `v0.1.2` state is mock-safe by default and includes E7-S5a
labelled WAV replay evidence for the released first-crack artifact pin.

A release may be described as hardware-validated only when the release
candidate has current evidence for:

- Warp or another real MCP client connected to the Hottop-configured server.
- Operator-approved heat, fan, drop, cooling, stop-cooling, emergency-stop,
  telemetry, and exported-log review.
- Explicit config, device path, model/audio settings, workflow run or local
  command evidence, and exported log paths.

A hardware-ready label additionally requires all hardware-validated evidence
plus current real microphone or audio-path first-crack validation, full
end-to-end agent roast validation evidence, and release-owner approval that the
documented Hottop command-loop, drop/cooling, emergency-stop, audio, and log
export behavior is acceptable for the target release. E7-S6 owns the full
end-to-end agent roast validation; this checklist story does not run it and
does not apply the label.

## Live Release

After all prerequisites are confirmed:

1. Ensure `server.json.version`, `server.json.packages[0].version`, and
   `coffee_roaster_mcp.__version__` are aligned.
2. Push the matching version tag:

   ```bash
   git tag v0.1.2
   git push origin v0.1.2
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
7. Run the install smoke and setup checks from
   `docs/install-and-hardware-setup.md` for the intended deployment mode before
   any hardware-ready labeling.

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
   `coffee-roaster-mcp`, the release-candidate package version, runtime hint
   `uvx`, and stdio transport.
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

## v0.1.1 Release Prep

The `v0.1.1` release is the fix-forward package release for the E7-S4 Warp
manual Hottop validation recovery fixes. It should be tagged only after the
version PR is merged to `main`.

Pre-tag checklist:

1. Confirm `server.json.version`, `server.json.packages[0].version`, and
   `coffee_roaster_mcp.__version__` are all `0.1.1`.
2. Confirm the local gates pass:
   `pytest`, `ruff check .`, `ruff format --check .`, `pyright`,
   `coffee-roaster-mcp --help`, and `coffee-roaster-mcp --version`.
3. Build the wheel and source distribution with `python -m build`.
4. Smoke install the built wheel from `dist/` before tagging.
5. Merge the version PR, then tag from updated `main`:

   ```bash
   git checkout main
   git pull --ff-only origin main
   git tag v0.1.1
   git push origin v0.1.1
   ```

6. Approve the GitHub `release` environment deployment.
7. Verify production PyPI and the MCP Registry show package version `0.1.1`.
8. Run the published-package smoke only after PyPI exposes `0.1.1`:

   ```bash
   uvx --from coffee-roaster-mcp==0.1.1 coffee-roaster-mcp --version
   ```

## v0.1.1 Live Publish Outcome

The fix-forward release completed on 2026-05-25 through GitHub Actions run
`26402310473`:

- Tag: `v0.1.1`
- Commit: `810318519899e662204b78671657bd9bc7222a73`
- Workflow: `https://github.com/syamaner/coffee-roaster-mcp/actions/runs/26402310473`
- PyPI release: `https://pypi.org/project/coffee-roaster-mcp/0.1.1/`
- MCP Registry search:
  `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`

Release job outcomes:

- `Validate Release Metadata`: success
- `Checks`: success
- `Build Package`: success
- `Publish PyPI`: success
- `Publish MCP Registry`: success after PyPI publish
- `Release Dry Run`: skipped, as expected for a tag push

Confirmed outcomes:

- Production PyPI exposes `coffee-roaster-mcp` version `0.1.1`.
- PyPI artifacts:
  - `coffee_roaster_mcp-0.1.1-py3-none-any.whl`, SHA-256
    `a6c68ec7dbea3c428922fce525fbf0d6dc451d549dff36a56bcb731aaf5ad395`
  - `coffee_roaster_mcp-0.1.1.tar.gz`, SHA-256
    `744bfaa34584173158c98bfebf80b574e25b7f793ac390d61ee1688862ca73df`
- Registry search returns `io.github.syamaner/coffee-roaster-mcp` with PyPI
  package `coffee-roaster-mcp`, version `0.1.1`, runtime hint `uvx`, stdio
  transport, and `isLatest: true`.
- Published-package smoke passed after refreshing the local `uvx` cache:
  `uvx --refresh --from coffee-roaster-mcp==0.1.1 coffee-roaster-mcp --version`
  returned `coffee-roaster-mcp 0.1.1`.
- The `v0.1.1` tag creation reported the same protected-tag creation rule
  bypass as `v0.1.0`; keep tag protection and release environment ownership
  under review before the next release.

## v0.1.2 Release Prep

The `v0.1.2` release is a metadata-only package release to expose related
project resources on PyPI and through the README reached from the MCP Registry
listing.

Planned change set:

- Bump `coffee_roaster_mcp.__version__`, `server.json.version`, and
  `server.json.packages[0].version` from `0.1.1` to `0.1.2`.
- Add package `Project-URL` metadata for the architecture article, original
  prototype posts, Hugging Face first-crack model, Hugging Face dataset, and
  Gradio demo Space.
- Add the same links to the README under Related Project Artifacts.
- Clarify that the current MCP package is the consolidated deterministic
  rebuild of the prototype.
- Keep `server.json.websiteUrl` pointed at the README instead of adding
  non-schema registry metadata fields.

After the PR merges:

```bash
git checkout main
git pull --ff-only origin main
git tag v0.1.2
git push origin v0.1.2
```

Then approve the `release` environment deployment and verify:

```bash
uvx --refresh --from coffee-roaster-mcp==0.1.2 coffee-roaster-mcp --version
```

## v0.1.2 Live Publish Outcome

The metadata-only release completed on 2026-05-25 through GitHub Actions run
`26403620501`:

- Tag: `v0.1.2`
- Commit: `3c19d6a677cf40c769dc8394d2e2ac53308446b6`
- Workflow: `https://github.com/syamaner/coffee-roaster-mcp/actions/runs/26403620501`
- PyPI release: `https://pypi.org/project/coffee-roaster-mcp/0.1.2/`
- MCP Registry search:
  `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`

Release job outcomes:

- `Validate Release Metadata`: success
- `Checks`: success
- `Build Package`: success
- `Publish PyPI`: success after `release` environment approval
- `Publish MCP Registry`: success after PyPI publish
- `Release Dry Run`: skipped, as expected for a tag push

Confirmed outcomes:

- Production PyPI exposes `coffee-roaster-mcp` version `0.1.2`.
- PyPI project URLs include the architecture article, original prototype intro,
  original prototype MCP post, Hugging Face first-crack model, Hugging Face
  dataset, and Gradio demo Space.
- PyPI artifacts:
  - `coffee_roaster_mcp-0.1.2-py3-none-any.whl`, SHA-256
    `c548967fc239cd93786cc23287c4c55cd67dac398a61b82021bc0022bd4926db`
  - `coffee_roaster_mcp-0.1.2.tar.gz`, SHA-256
    `25d554bef5f7477256fac63a1c277c224de116d7251f9cd34ff11fdb42a9ef77`
- Registry search returns `io.github.syamaner/coffee-roaster-mcp` with PyPI
  package `coffee-roaster-mcp`, version `0.1.2`, runtime hint `uvx`, stdio
  transport, and `isLatest: true`.
- Published-package smoke passed after refreshing the local `uvx` package
  cache:
  `uvx --refresh-package coffee-roaster-mcp --from coffee-roaster-mcp==0.1.2 coffee-roaster-mcp --version`
  returned `coffee-roaster-mcp 0.1.2`.
- The initial `uvx --refresh --from coffee-roaster-mcp==0.1.2 ...` smoke saw
  package-index lag and reported no available `0.1.2` version; the
  package-specific refresh succeeded immediately after.
- The `v0.1.2` tag creation reported the same protected-tag creation rule
  bypass as prior releases; keep tag protection and release environment
  ownership under review.

## v0.1.0 Live Publish Outcome

The first live release completed on 2026-05-24 through GitHub Actions run
`26367482422`:

- Tag: `v0.1.0`
- Commit: `276ec81056e05ed8a863c5e5bb9bf28e45308383`
- Workflow: `https://github.com/syamaner/coffee-roaster-mcp/actions/runs/26367482422`
- PyPI project: `https://pypi.org/project/coffee-roaster-mcp/`
- PyPI release: `https://pypi.org/project/coffee-roaster-mcp/0.1.0/`
- MCP Registry search:
  `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`

Release job outcomes:

- `Validate Release Metadata`: success
- `Checks`: success
- `Build Package`: success
- `Publish PyPI`: success after `release` environment approval
- `Publish MCP Registry`: success after PyPI publish
- `Release Dry Run`: skipped, as expected for a tag push

Verification commands and outcomes:

```bash
git tag v0.1.0
git push origin v0.1.0
curl -L https://pypi.org/pypi/coffee-roaster-mcp/json
curl -L https://pypi.org/pypi/coffee-roaster-mcp/json | jq -r '.info.version, (.info.description | contains("<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->")), .info.package_url, .info.release_url, (.urls[] | [.filename, .packagetype, .digests.sha256] | @tsv)'
/tmp/mcp-publisher validate server.json
curl -L 'https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp'
python3.11 -m venv /tmp/coffee-roaster-mcp-pypi-smoke
/tmp/coffee-roaster-mcp-pypi-smoke/bin/python -m pip install --no-cache-dir coffee-roaster-mcp==0.1.0
/tmp/coffee-roaster-mcp-pypi-smoke/bin/coffee-roaster-mcp --help
/tmp/coffee-roaster-mcp-pypi-smoke/bin/coffee-roaster-mcp --version
/tmp/coffee-roaster-mcp-pypi-smoke/bin/python -c "import os, tempfile; from coffee_roaster_mcp.config import load_config; tmp = tempfile.TemporaryDirectory(); os.chdir(tmp.name); c = load_config(environ={}); print(c.roaster.driver, c.first_crack.mode, c.first_crack.precision); tmp.cleanup()"
```

Confirmed outcomes:

- Production PyPI exposes `coffee-roaster-mcp` version `0.1.0`.
- The published PyPI long description contains
  `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
- PyPI artifacts:
  - `coffee_roaster_mcp-0.1.0-py3-none-any.whl`, SHA-256
    `d8cd00257bf30ddf89b98eff07d2b3d93369e3b441d9ef60f99b825e45436f33`
  - `coffee_roaster_mcp-0.1.0.tar.gz`, SHA-256
    `8c6ea87f4ccbae4654ac6df2c1588b86f79bdf1e54e19ec301aa7ef87b283e0c`
- `mcp-publisher validate server.json` returned `server.json is valid`.
- Registry search returns `io.github.syamaner/coffee-roaster-mcp` with
  PyPI package `coffee-roaster-mcp`, version `0.1.0`, runtime hint `uvx`, and
  stdio transport.
- Clean production-PyPI install succeeded in
  `/tmp/coffee-roaster-mcp-pypi-smoke`.
- Installed-package smokes returned `coffee-roaster-mcp 0.1.0` and
  mock-safe defaults `mock disabled int8`.

Retry and rollback notes:

- The `v0.1.0` tag creation bypassed a protected-tag creation rule, as reported
  by GitHub during `git push origin v0.1.0`; keep tag protection and release
  environment ownership under review before the next release.
- PyPI releases cannot be overwritten. If a package problem is found, fix
  forward with a new version and document the issue; yank `0.1.0` only if the
  package is actively harmful.
- If the Registry listing drifts or resets while the preview Registry is still
  mutable, rerun `mcp-publisher validate server.json`, confirm PyPI still
  exposes the matching package and marker, then retry the Registry publish path.
- The published `0.1.0` long description came from the pre-release README text
  and still says PyPI publication was planned. The verification marker and
  package metadata are correct; README wording was corrected after release for
  future publications.
