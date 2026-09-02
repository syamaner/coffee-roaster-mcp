"""Package metadata coverage for RoastPilot distributions."""

import tomllib
from importlib import metadata, resources
from pathlib import Path


def test_installed_distribution_metadata_is_complete() -> None:
    """Check the installed PyPI metadata for the public package identity."""
    package_metadata = metadata.metadata("coffee-roaster-mcp")

    assert package_metadata["Name"] == "coffee-roaster-mcp"
    assert package_metadata["Summary"] == (
        "RoastPilot: an MCP server for coffee-roaster telemetry and controlled actuation."
    )
    assert package_metadata["Requires-Python"] == ">=3.11"
    assert package_metadata["Author"] == "Sertan Yamaner"
    assert package_metadata["Maintainer"] == "Sertan Yamaner"

    keywords = {token.strip() for token in package_metadata["Keywords"].split(",") if token.strip()}
    assert {
        "coffee-roaster-control",
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
        "Architecture Article, https://dev.to/syamaner/part-1-the-architecture-the-agent-spec-driven-ml-development-with-warpoz-3al6",
        "Prototype Intro, https://dev.to/syamaner/part-1-training-a-neural-network-to-detect-coffee-first-crack-from-audio-an-agentic-development-1jei",
        "Prototype MCP Post, https://dev.to/syamaner/part-2-building-mcp-servers-to-control-a-home-coffee-roaster-an-agentic-development-journey-with-58ik",
        "First-Crack Model, https://huggingface.co/syamaner/coffee-first-crack-detection",
        "First-Crack Dataset, https://huggingface.co/datasets/syamaner/coffee-first-crack-audio",
        "First-Crack Demo, https://huggingface.co/spaces/syamaner/coffee-first-crack-detection",
    } <= project_urls

    assert resources.files("coffee_roaster_mcp").joinpath("py.typed").is_file()


def test_console_entrypoint_metadata_targets_cli_main() -> None:
    """Check the installed console script metadata for the PyPI package."""
    scripts = metadata.entry_points(group="console_scripts")
    entrypoint = scripts["coffee-roaster-mcp"]

    assert entrypoint.value == "coffee_roaster_mcp.cli:main"


def test_project_dependency_metadata_excludes_torch_frontend_packages() -> None:
    """The declared runtime dependencies retain the NumPy/SciPy frontend boundary."""
    project_path = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert all(
        not dependency.lower().startswith(("torch", "torchaudio", "transformers"))
        for dependency in dependencies
    )
