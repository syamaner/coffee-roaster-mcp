"""Command line entrypoint for RoastPilot."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from coffee_roaster_mcp import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the RoastPilot command line parser."""
    parser = argparse.ArgumentParser(
        prog="coffee-roaster-mcp",
        description="RoastPilot: a spec-driven MCP server for autonomous coffee roasting.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RoastPilot CLI."""
    parser = build_parser()
    parser.parse_args(argv)
    return 0
