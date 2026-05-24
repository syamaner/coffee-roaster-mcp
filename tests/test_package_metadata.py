"""Package metadata coverage for RoastPilot distributions."""

from importlib import metadata, resources


def test_installed_distribution_metadata_is_complete() -> None:
    """Check the installed PyPI metadata for the public package identity."""
    package_metadata = metadata.metadata("coffee-roaster-mcp")

    assert package_metadata["Name"] == "coffee-roaster-mcp"
    assert package_metadata["Summary"] == (
        "RoastPilot: a spec-driven MCP server for autonomous coffee roasting."
    )
    assert package_metadata["Requires-Python"] == ">=3.11"
    assert package_metadata["Author"] == "Sertan Yamaner"
    assert package_metadata["Maintainer"] == "Sertan Yamaner"

    keywords = {token.strip() for token in package_metadata["Keywords"].split(",") if token.strip()}
    assert {
        "autonomous-roasting",
        "coffee",
        "coffee-roasting",
        "mcp",
        "model-context-protocol",
        "roast-logging",
        "roastpilot",
    } <= keywords

    classifiers = set(package_metadata.get_all("Classifier", []))
    assert {
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: System :: Hardware",
        "Topic :: Utilities",
        "Typing :: Typed",
    } <= classifiers

    project_urls = set(package_metadata.get_all("Project-URL", []))
    assert {
        "Homepage, https://github.com/syamaner/coffee-roaster-mcp",
        "Documentation, https://github.com/syamaner/coffee-roaster-mcp#readme",
        "Repository, https://github.com/syamaner/coffee-roaster-mcp",
        "Issues, https://github.com/syamaner/coffee-roaster-mcp/issues",
    } <= project_urls

    assert resources.files("coffee_roaster_mcp").joinpath("py.typed").is_file()


def test_console_entrypoint_metadata_targets_cli_main() -> None:
    """Check the installed console script metadata for the PyPI package."""
    scripts = metadata.entry_points(group="console_scripts")
    entrypoint = scripts["coffee-roaster-mcp"]

    assert entrypoint.value == "coffee_roaster_mcp.cli:main"
