"""Command line entrypoint for RoastPilot."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.hottop_validation import (
    HottopValidationOptions,
    report_to_json,
    run_hottop_validation,
)
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

    validate_parser = subparsers.add_parser(
        "hottop-validate",
        help="Run guarded manual Hottop integration validation.",
    )
    validate_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a Hottop coffee-roaster-mcp YAML config file.",
    )
    validate_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON evidence file to write.",
    )
    validate_parser.add_argument(
        "--i-understand-this-controls-hardware",
        action="store_true",
        dest="hardware_acknowledged",
        help="Required acknowledgement before any Hottop hardware command is sent.",
    )
    validate_parser.add_argument(
        "--heat-percent",
        type=int,
        default=10,
        help="Conservative heat percentage for the heat validation step.",
    )
    validate_parser.add_argument(
        "--fan-percent",
        type=int,
        default=30,
        help="Fan percentage for the fan validation step.",
    )
    validate_parser.add_argument(
        "--step-duration-seconds",
        type=float,
        default=2.0,
        help="Delay after each hardware command.",
    )
    validate_parser.add_argument(
        "--telemetry-wait-seconds",
        type=float,
        default=5.0,
        help="Initial wait for status-packet telemetry after connect.",
    )
    validate_parser.add_argument(
        "--include-drop",
        action="store_true",
        help="Run the irreversible bean-drop command.",
    )
    validate_parser.add_argument(
        "--include-emergency-stop",
        action="store_true",
        help="Run the emergency-stop command as the final control step.",
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
    if args.command == "hottop-validate":
        report = run_hottop_validation(
            HottopValidationOptions(
                config_path=args.config,
                output_path=args.output,
                hardware_acknowledged=args.hardware_acknowledged,
                heat_percent=args.heat_percent,
                fan_percent=args.fan_percent,
                step_duration_seconds=args.step_duration_seconds,
                telemetry_wait_seconds=args.telemetry_wait_seconds,
                include_drop=args.include_drop,
                include_emergency_stop=args.include_emergency_stop,
            )
        )
        print(report_to_json(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
