"""README coverage for public MCP Registry verification metadata."""

from pathlib import Path


def test_readme_includes_mcp_verification_string() -> None:
    """Check the README includes the MCP Registry package-name proof."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    verification_string = "<!-- mcp-name: io.github.syamaner/coffee-roaster-mcp -->"

    readme_text = readme.read_text(encoding="utf-8")

    assert readme_text.count(verification_string) == 1
