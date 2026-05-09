"""Shared control validation helpers for RoastPilot."""

from __future__ import annotations


def validate_control_percent(value: object, *, label: str) -> int:
    """Validate one percentage-like control input.

    Args:
        value: Candidate control value.
        label: Field label to include in validation errors.

    Returns:
        Validated integer percentage.

    Raises:
        TypeError: If the value is not an integer percentage.
        ValueError: If the value is outside the inclusive 0 to 100 range.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer between 0 and 100.")
    if not 0 <= value <= 100:
        raise ValueError(f"{label} must be between 0 and 100.")
    return value
