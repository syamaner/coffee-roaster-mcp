# E6-S8 Live PyPI And MCP Registry Publish

This summary captures the controlled E6-S8 live publish, validation state, and
restart context.

## Scope

Story: `E6-S8` / issue `#135`, execute live PyPI and MCP Registry publish.

Branch: `feature/135-live-pypi-and-mcp-registry-publish`

The implementation stayed inside the live distribution boundary:

- verify release prerequisites before publishing
- publish production PyPI package `coffee-roaster-mcp` `0.1.0`
- confirm the published PyPI long description includes the exact
  `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->` marker
- run clean install and CLI smokes from the production PyPI package
- validate `server.json` with `mcp-publisher`
- publish MCP Registry metadata only after PyPI succeeds
- confirm Registry search returns the expected listing, PyPI package, version,
  runtime hint, and stdio transport
- record commands, links, outcomes, risks, and retry/rollback notes

No hardware validation, model training/export/sync, or real microphone
validation was performed.

## Publish Summary

Prerequisite gate:

- PR #136 was merged.
- Issue #55 was closed.
- `main` was fast-forwarded to
  `276ec81056e05ed8a863c5e5bb9bf28e45308383`.
- Branch
  `feature/135-live-pypi-and-mcp-registry-publish` was created from updated
  `main`.

Pre-publish checks:

- No local or remote `v0.1.0` tag existed.
- `server.json.version`, `server.json.packages[0].version`, and
  `coffee_roaster_mcp.__version__` were aligned at `0.1.0`.
- Production PyPI returned `Not Found` for `coffee-roaster-mcp` before release.
- Registry search returned no listing for
  `io.github.syamaner/coffee-roaster-mcp` before release.
- `/tmp/mcp-publisher validate server.json` returned `server.json is valid`.

Live publish:

- Created and pushed tag `v0.1.0`.
- GitHub reported a protected-tag creation rule was bypassed for this tag.
- GitHub Actions release run:
  `https://github.com/syamaner/coffee-roaster-mcp/actions/runs/26367482422`
- Run result: success.
- `Publish PyPI` succeeded after `release` environment approval.
- `Publish MCP Registry` succeeded after `Publish PyPI`.

Live links:

- PyPI project: `https://pypi.org/project/coffee-roaster-mcp/`
- PyPI release: `https://pypi.org/project/coffee-roaster-mcp/0.1.0/`
- Registry search:
  `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.syamaner/coffee-roaster-mcp`

Confirmed PyPI artifacts:

- `coffee_roaster_mcp-0.1.0-py3-none-any.whl`
  - SHA-256:
    `d8cd00257bf30ddf89b98eff07d2b3d93369e3b441d9ef60f99b825e45436f33`
- `coffee_roaster_mcp-0.1.0.tar.gz`
  - SHA-256:
    `8c6ea87f4ccbae4654ac6df2c1588b86f79bdf1e54e19ec301aa7ef87b283e0c`

Confirmed Registry listing:

- Name: `io.github.syamaner/coffee-roaster-mcp`
- Package registry: PyPI
- Package identifier: `coffee-roaster-mcp`
- Version: `0.1.0`
- Runtime hint: `uvx`
- Transport: `stdio`

## Validation

Local prerelease validation:

- `./.venv/bin/python -m pytest`: 356 passed
- `./.venv/bin/python -m ruff check .`: passed
- `./.venv/bin/python -m ruff format --check .`: passed
- `./.venv/bin/python -m pyright`: 0 errors
- `./.venv/bin/python -m build`: built source distribution and wheel
- `./.venv/bin/coffee-roaster-mcp --help`: passed
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`
- `/tmp/mcp-publisher validate server.json`: `server.json is valid`

Release workflow validation:

- `Validate Release Metadata`: success
- `Checks`: success
- `Build Package`: success
- `Publish PyPI`: success
- `Publish MCP Registry`: success
- `Release Dry Run`: skipped as expected for a tag push

Post-release PyPI validation:

- `curl -L https://pypi.org/pypi/coffee-roaster-mcp/json`: returned version
  `0.1.0`.
- The PyPI long description contains the exact verification marker:
  `<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->`.
- Clean install command:
  `/tmp/coffee-roaster-mcp-pypi-smoke/bin/python -m pip install --no-cache-dir coffee-roaster-mcp==0.1.0`
  succeeded.
- Installed CLI help smoke passed from `/tmp`.
- Installed CLI version smoke returned `coffee-roaster-mcp 0.1.0`.
- Installed config smoke from an empty temporary directory returned
  `mock disabled int8`.

Post-release Registry validation:

- `/tmp/mcp-publisher validate server.json`: `server.json is valid`.
- Registry search returned one listing for
  `io.github.syamaner/coffee-roaster-mcp` with PyPI package
  `coffee-roaster-mcp` and stdio transport.

Final documentation and repo validation after updating release/state docs:

- `./.venv/bin/python -m pytest tests/test_readme.py tests/test_release_workflow.py`:
  9 passed.
- `./.venv/bin/python -m pytest`: 356 passed.
- `./.venv/bin/python -m ruff check .`: passed.
- `./.venv/bin/python -m ruff format --check .`: passed.
- `./.venv/bin/python -m pyright`: 0 errors.
- `./.venv/bin/coffee-roaster-mcp --help`: passed.
- `./.venv/bin/coffee-roaster-mcp --version`: `coffee-roaster-mcp 0.1.0`.

## Risks And Retry Notes

- PyPI packages cannot be overwritten. If the `0.1.0` package has a defect,
  fix forward with a new version; yank only if the release is actively harmful.
- The MCP Registry is still preview. If listing data resets or drifts, rerun
  `mcp-publisher validate server.json`, confirm PyPI still exposes the matching
  package and marker, then retry the Registry publish path.
- The `v0.1.0` tag push reported a protected-tag creation rule bypass. Review
  tag protection before the next live release.
- The published `0.1.0` long description came from the pre-release README and
  still says PyPI publication was planned. The marker and metadata are correct;
  README wording was corrected after release for future publications.

## Usage Snapshot

- Token usage: `448K used`

## Restart Prompt

Resume in the local clone of `syamaner/coffee-roaster-mcp`. E6-S8 live PyPI and
MCP Registry publish has completed successfully for `0.1.0`. Before the next
story, verify the E6-S8 PR is merged and issue #135 is closed, then sync
`main`. Route next work to Epic 7 broad mock-safe release validation unless the
operator explicitly selects a different story. Do not run hardware validation,
model training/export/sync, or real microphone validation unless the selected
story explicitly requires it.
