"""Command line entrypoint for RoastPilot."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.mcp_server import run_stdio_server


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
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the RoastPilot MCP server over stdio.",
    )
    serve_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to a coffee-roaster-mcp YAML config file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RoastPilot CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        run_stdio_server(config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
