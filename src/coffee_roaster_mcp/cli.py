"""Command line entrypoint for RoastPilot."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.hottop_validation import (
    HottopValidationOptions,
    report_to_json,
    run_hottop_validation,
)
from coffee_roaster_mcp.mcp_server import run_stdio_server
from coffee_roaster_mcp.mic_check import DEFAULT_RMS_FLOOR, MicCheckOptions, run_mic_check
from coffee_roaster_mcp.mic_check import report_to_json as mic_report_to_json


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

    mic_parser = subparsers.add_parser(
        "mic-check",
        help="Prove the configured microphone is capturing real audio (pre-roast).",
    )
    mic_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to a coffee-roaster-mcp YAML config file (selects the device).",
    )
    mic_parser.add_argument(
        "--duration-seconds",
        type=float,
        default=5.0,
        help="How long to capture before reporting; make noise during this window.",
    )
    mic_parser.add_argument(
        "--rms-floor",
        type=float,
        default=DEFAULT_RMS_FLOOR,
        help="RMS level above which captured audio counts as real signal.",
    )
    mic_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON evidence file to write.",
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
        if not report.hardware_ready_release_label_allowed:
            return 1
    if args.command == "mic-check":
        print(
            f"Listening on the configured microphone for {args.duration_seconds:.0f}s — "
            "make noise (snap / speak / tap)…",
            file=sys.stderr,
            flush=True,
        )

        def _meter(rms: float, peak: float) -> None:
            bar = "#" * min(40, int(rms / DEFAULT_RMS_FLOOR * 4))
            print(f"  level {rms:7.4f} peak {peak:7.4f} |{bar}", file=sys.stderr, flush=True)

        report = run_mic_check(
            MicCheckOptions(
                config_path=args.config,
                duration_seconds=args.duration_seconds,
                rms_floor=args.rms_floor,
            ),
            on_chunk=_meter,
        )
        if args.output is not None:
            args.output.write_text(mic_report_to_json(report), encoding="utf-8")
        print(mic_report_to_json(report))
        verdict = "PASS — microphone is capturing real audio" if report.passed else "FAIL"
        print(f"\n{verdict}", file=sys.stderr)
        if not report.passed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
