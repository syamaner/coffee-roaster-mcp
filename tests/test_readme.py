"""Documentation coverage for public setup and registry metadata."""

from pathlib import Path


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
        "reviewed, unreleased candidate",
        "also reports version `0.2.2`",
        "adds that typed projection",
        "cannot attest which bytes the intended interpreter imports",
        "Verifying that installed bytes correspond to a selected artifact is a separate, "
        "operator-authorised gate",
        "does not perform that gate or claim that either distribution is installed",
    )
    agents_requirements = (
        "published PyPI and MCP Registry distribution is `0.2.2`",
        "lacks D209's cold-temperature projection",
        "reviewed, unreleased candidate",
        "reports the same `0.2.2` version",
        "includes the typed projection",
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
