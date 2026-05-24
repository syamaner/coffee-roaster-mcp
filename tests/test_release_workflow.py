"""Release workflow coverage for PyPI and MCP Registry publishing."""

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
PINNED_ACTION_REF = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


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
    assert _step_uses_pinned_action(pypi_job, "pypa/gh-action-pypi-publish")

    assert registry_job["environment"] == "release"
    assert registry_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert _step_runs(registry_job, "./mcp-publisher validate server.json")
    assert _step_runs(registry_job, "./mcp-publisher login github-oidc")
    assert _step_runs(registry_job, "./mcp-publisher publish server.json")


def test_release_workflow_pins_actions_and_checkout_credentials() -> None:
    """Check action steps use immutable refs and checkout does not persist credentials."""
    jobs = _load_release_workflow()["jobs"]

    for job in jobs.values():
        for step in job["steps"]:
            action = step.get("uses")
            if action is None:
                continue
            assert PINNED_ACTION_REF.match(action), action
            if action.startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_release_workflow_pins_and_verifies_mcp_publisher() -> None:
    """Check MCP Registry publishing does not execute an unverified latest binary."""
    registry_job = _load_release_workflow()["jobs"]["publish-mcp-registry"]
    install_step = _step_named(registry_job, "Install mcp-publisher")
    run_script = install_step["run"]

    assert 'MCP_PUBLISHER_VERSION="v1.7.9"' in run_script
    assert (
        'MCP_PUBLISHER_SHA256="ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac"'
        in run_script
    )
    assert "releases/download/${MCP_PUBLISHER_VERSION}" in run_script
    assert "releases/latest" not in run_script
    assert "sha256sum -c -" in run_script
    assert "curl" in run_script and "| tar" not in run_script


def test_release_workflow_metadata_validation_reports_clear_shape_errors() -> None:
    """Check release metadata validation guards produce clear operator errors."""
    validation_job = _load_release_workflow()["jobs"]["validate-release-metadata"]
    validation_step = _step_named(
        validation_job,
        "Validate release tag, package version, and registry metadata",
    )
    run_script = validation_step["run"]

    assert "version_match = re.search" in run_script
    assert "if version_match is None:" in run_script
    assert "Could not find __version__" in run_script
    assert "Expected __version__" in run_script
    assert 'packages = server_json.get("packages")' in run_script
    assert "not isinstance(packages, list)" in run_script
    assert "len(packages) == 0" in run_script
    assert "server.json must contain a non-empty 'packages' array." in run_script
    assert "not isinstance(package, dict)" in run_script
    assert 'not isinstance(package.get("version"), str)' in run_script
    assert "server.json packages[0] must be an object with a string 'version' field." in run_script


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
        "Run `./mcp-publisher validate server.json` before authenticating.",
        "The first destructive registry operation is `./mcp-publisher publish server.json`.",
    ]

    for phrase in expected_phrases:
        assert phrase in runbook


def _load_release_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _step_uses_pinned_action(job: dict[str, Any], action_name: str) -> bool:
    return any(
        step.get("uses", "").startswith(f"{action_name}@") and PINNED_ACTION_REF.match(step["uses"])
        for step in job["steps"]
    )


def _step_runs(job: dict[str, Any], command: str) -> bool:
    return any(step.get("run") == command for step in job["steps"])


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step  # type: ignore[no-any-return]
    raise AssertionError(f"Step {name!r} was not found.")
