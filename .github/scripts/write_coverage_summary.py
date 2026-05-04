"""Write a concise GitHub Actions coverage summary from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileCoverage:
    """Coverage metrics for one measured source file."""

    path: str
    percent_covered: float
    covered_lines: int
    missing_lines: int


def main() -> int:
    """Run the coverage summary writer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path, help="Path to coverage.py JSON output.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Summary output path. Defaults to stdout when omitted.",
    )
    args = parser.parse_args()

    payload = _load_coverage_payload(args.coverage_json)
    summary = _build_summary(payload)
    if args.output is None:
        print(summary)
    else:
        args.output.write_text(summary + "\n", encoding="utf-8")
    return 0


def _load_coverage_payload(path: Path) -> dict[str, Any]:
    """Load coverage.py JSON output."""
    with path.open(encoding="utf-8") as input_file:
        loaded = json.load(input_file)
    if not isinstance(loaded, dict):
        raise ValueError("Coverage JSON root must be an object.")
    return loaded


def _build_summary(payload: dict[str, Any]) -> str:
    """Build a Markdown coverage summary for GitHub Actions."""
    totals = _as_mapping(payload.get("totals"))
    files = _file_coverages(_as_mapping(payload.get("files")))
    total_percent = _as_float(totals.get("percent_covered_display"))
    if total_percent is None:
        total_percent = _as_float(totals.get("percent_covered")) or 0.0
    covered_lines = _as_int(totals.get("covered_lines"))
    missing_lines = _as_int(totals.get("missing_lines"))
    branch_percent = _as_float(totals.get("percent_covered")) or total_percent

    lines = [
        "## Test Coverage",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total coverage | **{total_percent:.2f}%** |",
        f"| Lines covered | {covered_lines} |",
        f"| Lines missing | {missing_lines} |",
        f"| Branch-aware coverage | {branch_percent:.2f}% |",
        "",
        _coverage_bar(total_percent),
        "",
        "### Lowest Covered Source Files",
        "",
    ]
    if not files:
        lines.append("No measured source files were reported.")
    else:
        lines.extend(
            [
                "| File | Coverage | Missing lines |",
                "| --- | ---: | ---: |",
            ]
        )
        for file_coverage in sorted(files, key=lambda item: item.percent_covered)[:5]:
            lines.append(
                "| "
                f"`{file_coverage.path}` | "
                f"{file_coverage.percent_covered:.2f}% | "
                f"{file_coverage.missing_lines} |"
            )

    lines.extend(
        [
            "",
            "### Artifact",
            "",
            "Download `html-coverage-report` from this workflow run for a drill-down HTML report.",
        ]
    )
    return "\n".join(lines)


def _file_coverages(files_payload: dict[str, Any]) -> list[FileCoverage]:
    """Return measured source file coverage rows from coverage.py JSON."""
    rows: list[FileCoverage] = []
    for path, value in files_payload.items():
        if not path.startswith("src/coffee_roaster_mcp/"):
            continue
        summary = _as_mapping(_as_mapping(value).get("summary"))
        percent_covered = _as_float(summary.get("percent_covered_display"))
        if percent_covered is None:
            percent_covered = _as_float(summary.get("percent_covered")) or 0.0
        rows.append(
            FileCoverage(
                path=path,
                percent_covered=percent_covered,
                covered_lines=_as_int(summary.get("covered_lines")),
                missing_lines=_as_int(summary.get("missing_lines")),
            )
        )
    return rows


def _coverage_bar(percent: float) -> str:
    """Return a simple HTML coverage bar for GitHub-flavored Markdown."""
    bounded_percent = max(0.0, min(100.0, percent))
    return (
        "<p>"
        f"<strong>{bounded_percent:.2f}%</strong><br>"
        f'<progress max="100" value="{bounded_percent:.2f}"></progress>'
        "</p>"
    )


def _as_mapping(value: object) -> dict[str, Any]:
    """Return a dictionary when the value has the expected JSON shape."""
    return value if isinstance(value, dict) else {}


def _as_float(value: object) -> float | None:
    """Return a float when the value is numeric or numeric text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: object) -> int:
    """Return an int when the value is numeric, otherwise zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
