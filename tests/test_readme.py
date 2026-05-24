"""Documentation coverage for public setup and registry metadata."""

from pathlib import Path


def test_readme_includes_mcp_verification_string() -> None:
    """Check the README includes the MCP Registry package-name proof."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    verification_string = "<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->"

    readme_text = readme.read_text(encoding="utf-8")

    assert readme_text.count(verification_string) == 1


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
