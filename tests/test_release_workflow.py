"""Release workflow coverage for PyPI and MCP Registry publishing."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def test_release_workflow_runs_on_tags_and_manual_dry_run() -> None:
    """Check release workflow trigger coverage for dry run and live tag releases."""
    workflow = _load_release_workflow()

    trigger = workflow["on"]
    assert trigger["push"]["tags"] == ["v*"]
    assert trigger["workflow_dispatch"]["inputs"]["dry_run"] == {
        "description": "Build and validate without publishing to PyPI or the MCP Registry.",
        "required": True,
        "type": "boolean",
        "default": True,
    }


def test_release_workflow_builds_before_publishing() -> None:
    """Check release workflow orders checks, package build, PyPI, and registry publish."""
    jobs = _load_release_workflow()["jobs"]

    assert set(jobs) == {
        "checks",
        "validate-release-metadata",
        "build-package",
        "release-dry-run",
        "publish-pypi",
        "publish-mcp-registry",
    }
    assert jobs["build-package"]["needs"] == ["checks", "validate-release-metadata"]
    assert jobs["release-dry-run"]["needs"] == "build-package"
    assert jobs["publish-pypi"]["needs"] == "build-package"
    assert jobs["publish-mcp-registry"]["needs"] == "publish-pypi"


def test_release_workflow_uses_trusted_publishing_and_release_environment() -> None:
    """Check live publish jobs use the protected release environment and OIDC."""
    jobs = _load_release_workflow()["jobs"]
    pypi_job = jobs["publish-pypi"]
    registry_job = jobs["publish-mcp-registry"]

    assert pypi_job["environment"] == "release"
    assert pypi_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert _step_uses(pypi_job, "pypa/gh-action-pypi-publish@release/v1")

    assert registry_job["environment"] == "release"
    assert registry_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert _step_runs(registry_job, "./mcp-publisher login github-oidc")
    assert _step_runs(registry_job, "./mcp-publisher publish --file=server.json")


def test_release_runbook_documents_operator_prerequisites() -> None:
    """Check the release runbook documents the operator setup required by E6-S5."""
    runbook = (Path(__file__).resolve().parents[1] / "docs" / "release.md").read_text(
        encoding="utf-8"
    )

    expected_phrases = [
        "workflow filename: `release.yml`",
        "environment: `release`",
        "tag-triggered job: `publish-pypi`",
        "Protected tag rules block unapproved creation or update of `v*` tags.",
        "GitHub environment named `release` exists and requires manual approval",
        "GitHub environment secret `PYPI_API_TOKEN`",
        "TestPyPI rehearsal is not enabled in the workflow.",
        "MCP Registry publishing runs only after the PyPI publish job succeeds.",
    ]

    for phrase in expected_phrases:
        assert phrase in runbook


def _load_release_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _step_uses(job: dict[str, Any], action: str) -> bool:
    return any(step.get("uses") == action for step in job["steps"])


def _step_runs(job: dict[str, Any], command: str) -> bool:
    return any(step.get("run") == command for step in job["steps"])
