"""Pure projection of passive Hottop receipt observations for cold reads."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from coffee_roaster_mcp.drivers import HottopReceiptObservation

ColdReceiptOutcome = Literal[
    "complete_celsius_packet_received",
    "receipt_unusable",
    "no_receipt",
    "not_eligible",
    "unsupported",
    "malformed",
]

_BUILT_IN_CLASSIFIER_REASONS = frozenset({"candidate_not_configured", "receipt_age_bound_unset"})


@dataclass(frozen=True)
class ColdReceiptReport:
    """Flat, versioned host-receipt diagnostic facts for a cold read.

    The projection deliberately reports only host-side parsing and queue facts.
    It does not establish sensor freshness, physical state, or command authority.
    """

    receipt_report_version: Literal[1]
    outcome: ColdReceiptOutcome
    defects: tuple[str, ...]
    same_handle_epoch: bool | None
    read_returned_age_seconds: float | None
    drained_floor_present: bool
    drained_floor_age_seconds: float | None
    queued_before_read: int | None
    queued_after_read: int | None
    requested_byte_count: int | None
    returned_byte_count: int | None
    packet_count: int | None
    skipped_byte_count: int | None
    prior_tail_length: int | None
    tail_length: int | None
    latest_bean_temp_c: float | None
    latest_env_temp_c: float | None
    receipt_sequence: int
    observer_discard_count: int
    sensor_age: Literal["unknown"] = "unknown"
    max_host_age: Literal["not_selected"] = "not_selected"
    queue_scope: Literal["host_os_receive_queue"] = "host_os_receive_queue"


def project_cold_receipt_report(observation: HottopReceiptObservation) -> ColdReceiptReport:
    """Project one cached passive receipt observation into flat diagnostic facts.

    Args:
        observation: Cached driver observation obtained without transport I/O.

    Returns:
        A version-one report. Malformed input fails closed as ``malformed``.
    """
    try:
        reasons = observation.classification.reasons
        if not _BUILT_IN_CLASSIFIER_REASONS.issubset(reasons):
            raise ValueError("receipt classifier did not retain required built-in reasons")
        defects = tuple(reason for reason in reasons if reason not in _BUILT_IN_CLASSIFIER_REASONS)
        receipt = observation.receipt
        if receipt is None:
            return _report(
                outcome="receipt_unusable" if "clock_regression" in defects else "no_receipt",
                defects=defects,
                same_handle_epoch=None,
                receipt_sequence=observation.receipt_sequence,
                observer_discard_count=observation.observer_discard_count,
            )

        same_handle_epoch = (
            receipt.epoch == observation.epoch
            and receipt.handle_identity == observation.handle_token
        )
        clock_regressed = "clock_regression" in defects
        read_age = (
            None
            if clock_regressed
            else _age_if_nonnegative(observation.observed_at, receipt.read_returned_at)
        )
        floor = receipt.drained_floor
        floor_present = floor is not None
        floor_age = observation.classification.drained_floor_age_seconds
        outcome: ColdReceiptOutcome = (
            "complete_celsius_packet_received" if not defects else "receipt_unusable"
        )
        latest_pair = (
            receipt.latest_pair_c if outcome == "complete_celsius_packet_received" else None
        )
        return ColdReceiptReport(
            receipt_report_version=1,
            outcome=outcome,
            defects=defects,
            same_handle_epoch=same_handle_epoch,
            read_returned_age_seconds=read_age,
            drained_floor_present=floor_present,
            drained_floor_age_seconds=floor_age,
            queued_before_read=receipt.queued_before_read,
            queued_after_read=receipt.queued_after_read,
            requested_byte_count=receipt.scan.requested_byte_count,
            returned_byte_count=receipt.scan.returned_byte_count,
            packet_count=len(receipt.scan.packets),
            skipped_byte_count=receipt.scan.skipped_byte_count,
            prior_tail_length=receipt.scan.prior_tail_length,
            tail_length=receipt.scan.tail_length,
            latest_bean_temp_c=None if latest_pair is None else latest_pair[0],
            latest_env_temp_c=None if latest_pair is None else latest_pair[1],
            receipt_sequence=observation.receipt_sequence,
            observer_discard_count=observation.observer_discard_count,
        )
    except Exception:
        return _report(
            outcome="malformed",
            defects=(),
            same_handle_epoch=None,
            receipt_sequence=0,
            observer_discard_count=0,
        )


def _age_if_nonnegative(now: float | None, then: float) -> float | None:
    """Return a finite non-negative host-clock age, or ``None`` on regression."""
    if now is None:
        return None
    age = now - then
    if not isfinite(age) or age < 0:
        return None
    return age


def _report(
    *,
    outcome: ColdReceiptOutcome,
    defects: tuple[str, ...],
    same_handle_epoch: bool | None,
    receipt_sequence: int,
    observer_discard_count: int,
) -> ColdReceiptReport:
    """Build a report whose unavailable scalar facts are represented by ``None``."""
    return ColdReceiptReport(
        receipt_report_version=1,
        outcome=outcome,
        defects=defects,
        same_handle_epoch=same_handle_epoch,
        read_returned_age_seconds=None,
        drained_floor_present=False,
        drained_floor_age_seconds=None,
        queued_before_read=None,
        queued_after_read=None,
        requested_byte_count=None,
        returned_byte_count=None,
        packet_count=None,
        skipped_byte_count=None,
        prior_tail_length=None,
        tail_length=None,
        latest_bean_temp_c=None,
        latest_env_temp_c=None,
        receipt_sequence=receipt_sequence,
        observer_discard_count=observer_discard_count,
    )
