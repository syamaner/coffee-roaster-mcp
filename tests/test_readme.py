"""Documentation coverage for public setup and registry metadata."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, heading: str, next_heading: str) -> str:
    """Return one bounded Markdown section."""
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def test_readme_includes_mcp_verification_string() -> None:
    """Check the README includes the MCP Registry package-name proof."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    verification_string = "<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->"

    readme_text = readme.read_text(encoding="utf-8")

    assert readme_text.count(verification_string) == 1


def test_cold_setup_docs_distinguish_published_candidate_and_installed_bytes() -> None:
    """Keep the three cold-test package identities distinct in setup authority."""
    repository_root = Path(__file__).resolve().parents[1]
    readme_text = (repository_root / "README.md").read_text(encoding="utf-8")
    agents_text = (repository_root / "AGENTS.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme_text.split())
    normalized_agents = " ".join(agents_text.split())

    readme_requirements = (
        "published PyPI and MCP Registry distribution is `0.2.2`",
        "does not contain D209's cold-temperature projection",
        "reviewed, unreleased `0.2.3` candidate",
        "adds D209's typed cold-only temperature and packet projection alongside #222's "
        "roast-fan-only cold-session `get_roast_state` observation",
        "includes PR #231's stopped-fault exact-session recovery semantics",
        "The #222 observation reports commanded roast-fan state only",
        "The D209 projection reports telemetry, packet validity and counters, and raw and "
        "typed temperature information",
        "Packet progress screening is an Agent comparison between observations, not a "
        "continuous freshness or link watchdog",
        "Neither the `0.2.2` publication nor the `0.2.3` candidate version, wheel name, or "
        "declared command-line digest can attest which bytes the intended interpreter imports",
        "Verifying that installed bytes correspond to a selected artifact is a separate, "
        "operator-authorised gate",
        "does not perform that gate or claim that either distribution is installed",
    )
    agents_requirements = (
        "published PyPI and MCP Registry distribution is `0.2.2`",
        "lacks D209's cold-temperature projection",
        "reviewed, unreleased `0.2.3` candidate",
        "includes D209's typed cold-only temperature and packet projection",
        "PR #231's stopped-fault exact-session recovery semantics",
        "#222 reports commanded roast-fan state only",
        "D209 reports telemetry, packet validity and counters, and raw and typed temperature "
        "information",
        "Agent-side comparison between observations owns packet-progress screening",
        "Neither surface proves physical state, calibration, continuous freshness, link health, "
        "or readiness",
        "does not attest the bytes imported by the intended interpreter",
        "Installed-byte verification is a separate, operator-authorised gate",
        "does not perform it or claim that either distribution is installed",
    )

    for phrase in readme_requirements:
        assert phrase in normalized_readme
    for phrase in agents_requirements:
        assert phrase in normalized_agents

    for docs_text in (readme_text, agents_text):
        assert "`v0.2.1` is the current published package" not in docs_text
        assert "`0.2.1` is the published PyPI and MCP Registry baseline" not in docs_text
        assert "`0.2.2` is an unpublished candidate" not in docs_text


def test_current_authority_surfaces_distinguish_published_0_2_2_and_candidate_0_2_3() -> None:
    """Keep current release authority distinct from candidate and install state."""
    paths_and_bounds = (
        ("README.md", "## Status", "## Related Project Artifacts"),
        ("AGENTS.md", "## Current authority and repository map", "```text"),
        ("docs/state/registry.md", "## Active Epic", "## Historical Narrative"),
        (
            "docs/state/epics/coffee-roaster-mcp-v0.1.md",
            "## Active Context",
            "- `E7-S1`",
        ),
        ("docs/state/github-issues.md", "# RoastPilot GitHub Issue Index", "## Epics"),
        ("docs/release.md", "## Current Release Authority", "## Changelog"),
        (
            ".claude/skills/release-registry/SKILL.md",
            "## Current Scope",
            "## Release Targets",
        ),
    )

    for relative_path, heading, next_heading in paths_and_bounds:
        text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        current = " ".join(_section(text, heading, next_heading).split())
        assert "0.2.2" in current
        assert "0.2.3" in current
        assert "PyPI" in current
        assert "MCP Registry" in current
        assert "reviewed, unreleased" in current
        assert "installed" in current
        assert "v0.2.1` is the current" not in current
        assert "released baseline is `v0.2.1" not in current
        assert "unpublished `0.2.2" not in current
        assert "`0.2.2` is an unpublished" not in current
        assert "published `0.2.3`" not in current
        assert "verified `v0.2.2` tag SHA" not in current
        assert "verified release-workflow receipt" not in current


def test_current_issue_state_keeps_225_and_230_open_and_227_closed() -> None:
    """Keep release reconciliation open and the D209 software story closed."""
    paths_and_bounds = (
        (
            "docs/state/registry.md",
            "## Active Epic",
            "## Historical Narrative",
            "#225 remains open",
            "#227 is closed",
            "PR #231's stopped-fault exact-session recovery semantics",
        ),
        (
            "docs/state/epics/coffee-roaster-mcp-v0.1.md",
            "## Active Context",
            "- `E7-S1`",
            "#225 remains open",
            "#227 is closed",
            "PR #231's stopped-fault exact-session recovery semantics",
        ),
        (
            "docs/state/github-issues.md",
            "## Current Stories",
            "## Epics",
            "#225: open",
            "#227: closed",
            "#230: open",
        ),
    )

    for (
        relative_path,
        heading,
        next_heading,
        open_phrase,
        closed_phrase,
        candidate_phrase,
    ) in paths_and_bounds:
        text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        current = " ".join(_section(text, heading, next_heading).split())
        assert open_phrase in current
        assert closed_phrase in current
        assert candidate_phrase in current
        assert "#225 complete" not in current
        assert "#225 closed" not in current
        assert "#225 checklist completed" not in current


def test_release_0_2_2_first_publication_instructions_are_historical() -> None:
    """Prevent the superseded 0.2.2 procedure from becoming executable again."""
    release_text = (REPOSITORY_ROOT / "docs/release.md").read_text(encoding="utf-8")
    checklist = _section(
        release_text,
        "## v0.2.2 Release Checklist",
        "## v0.2.1 Release Checklist",
    )
    live_release = _section(release_text, "## Live Release", "## MCP Registry Verification")

    assert "Historical/Superseded — Non-Executable" in checklist
    assert "does not prove that any listed step ran" in checklist
    assert "does not currently authorise or instruct a live release" in live_release
    for forbidden in (
        "git tag v0.2.2",
        "git push origin v0.2.2",
        "Use this checklist",
        "Run this checklist",
    ):
        assert forbidden not in checklist
        assert forbidden not in live_release


def test_future_live_release_preserves_minimum_prerequisites() -> None:
    """Keep generic publication safeguards after retiring the 0.2.2 procedure."""
    release_text = (REPOSITORY_ROOT / "docs/release.md").read_text(encoding="utf-8")
    live_release = " ".join(
        _section(release_text, "## Live Release", "## MCP Registry Verification").split()
    )

    minimum_guards = (
        "The release-preparation PR is merged",
        "Applicable protected checks are green and all conversations are resolved",
        "A release-workflow dry run has succeeded",
        "The human release operator explicitly approves the protected release environment",
    )
    for guard in minimum_guards:
        assert guard in live_release

    assert "These prerequisites are necessary, not sufficient" in live_release
    assert "future version-specific contract" in live_release
    assert (
        "human operator's tag and publication decision remain separately required" in live_release
    )
    assert "Nothing in this generic policy verifies" in live_release
    assert "published `0.2.2`" in live_release


def test_0_2_3_candidate_checklist_requires_release_and_public_evidence() -> None:
    """Keep the 0.2.3 candidate checks distinct from published evidence."""
    release_text = (REPOSITORY_ROOT / "docs/release.md").read_text(encoding="utf-8")
    checklist = _section(
        release_text,
        "## v0.2.3 Candidate Checklist",
        "## v0.2.3 Post-Publication Verification",
    )
    post_publication = _section(
        release_text,
        "## v0.2.3 Post-Publication Verification",
        "## v0.2.2 Release Checklist",
    )
    normalized_checklist = " ".join(checklist.split())
    normalized_post_publication = " ".join(post_publication.split())

    required_phrases = (
        "reviewed release candidate, not a published distribution",
        "full local and protected CI gates",
        "clean-wheel mock-safe smoke",
        "dry_run: true",
        "exact merged `main` head",
        "same commit SHA",
        "`v0.2.3` is absent",
        "created at the exact commit SHA that passed the recorded dry run",
        "tag name and package version must both be `0.2.3`",
        "protected `release` environment",
        "D209's cold-only projection",
        "#230's stopped-fault exact-session recovery semantics",
        "does not establish hardware readiness or authorise beans",
    )
    for phrase in required_phrases:
        assert phrase in normalized_checklist

    post_publication_phrases = (
        "Only after the human release operator has created the recorded tag",
        "public PyPI wheel and source-distribution hashes",
        "installed mock-safe smoke",
        "MCP Registry version and `isLatest` state",
    )
    for phrase in post_publication_phrases:
        assert phrase in normalized_post_publication


def test_release_history_is_preserved() -> None:
    """Keep the older published release evidence explicitly historical."""
    release_text = (REPOSITORY_ROOT / "docs/release.md").read_text(encoding="utf-8")
    registry_text = (REPOSITORY_ROOT / "docs/state/registry.md").read_text(encoding="utf-8")

    assert "### 0.2.1 (published)" in release_text
    assert "## v0.2.1 Release Checklist (Historical Record Only)" in release_text
    assert "## Historical Narrative (Superseded For Current Delivery)" in registry_text


def test_release_skill_fails_closed_on_unknown_current_release_evidence() -> None:
    """Keep the release skill read-only when current evidence is incomplete."""
    skill_text = (REPOSITORY_ROOT / ".claude/skills/release-registry/SKILL.md").read_text(
        encoding="utf-8"
    )
    current = " ".join(_section(skill_text, "## Current Scope", "## Release Targets").split())

    assert "read-only" in skill_text
    assert "`0.2.2`" in current
    assert "#225 remains open" in current
    assert "#227 is closed" in current
    assert "reviewed, unreleased `0.2.3` candidate" in current
    assert "Do not use it to recommend tagging or publication" in current
    assert "recommend no release action" in current
    assert "Agents do not tag, approve environments, publish" in skill_text


def test_install_and_hardware_setup_docs_cover_required_topics() -> None:
    """Check the E6-S7 setup docs cover the required operator topics."""
    docs_root = Path(__file__).resolve().parents[1] / "docs"
    setup_doc = (docs_root / "install-and-hardware-setup.md").read_text(encoding="utf-8")
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    release_doc = (docs_root / "release.md").read_text(encoding="utf-8")

    expected_phrases = [
        "## Mock Install",
        "## Hottop Configuration",
        "## Hugging Face Model Configuration",
        "## Offline Model Path",
        "## Log Output Paths",
        "roaster.driver: mock",
        "hottop_kn8828b_2k_plus",
        "syamaner/coffee-first-crack-detection",
        "first_crack.local_model_dir",
        "{logging.log_dir}/roasts/{session_id}/",
        "Do not commit generated files under `logs/`.",
    ]

    for phrase in expected_phrases:
        assert phrase in setup_doc

    assert "docs/install-and-hardware-setup.md" in readme
    assert "docs/install-and-hardware-setup.md" in release_doc
