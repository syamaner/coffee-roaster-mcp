"""Closed diagnostic records: independent of capture, persistence and control."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")
NonNegative = Annotated[int, Field(strict=True, ge=0)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
NullReason = Literal[
    "not_applicable",
    "not_yet_observed",
    "recording_not_enabled",
    "recorder_never_started",
    "clock_unavailable",
    "model_never_loaded",
    "no_confirmation",
    "resolution_failed",
    "hash_failed",
    "load_not_completed",
    "hub_default_revision",
    "inference_failed",
]
ObserverState = Literal[
    "not_admitted",
    "loading",
    "observing",
    "confirmed",
    "unavailable",
    "incomplete",
    "closing",
    "stopped",
]


class Closed(BaseModel):
    """Versioned objects accept no extra keys or non-finite values."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Maybe(Closed, Generic[T]):
    """A missing value always has an explicit reason; zero is a real value."""

    value: T | None
    null_reason: NullReason | None

    @model_validator(mode="after")
    def exclusive(self) -> Maybe[T]:
        if (self.value is None) == (self.null_reason is None):
            raise ValueError("Exactly one of value and null_reason must be non-null.")
        return self


def absent(reason: NullReason = "not_yet_observed") -> Maybe[Any]:
    """Construct an explicitly missing field."""
    return Maybe[Any](value=None, null_reason=reason)


def present(value: T) -> Maybe[T]:
    """Construct a present field without a missing-value reason."""
    return Maybe[T](value=value, null_reason=None)


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp must have an explicit zero timezone offset.")
    return parsed.isoformat()


UtcString = Annotated[str, AfterValidator(_utc)]
DiagnosticReason = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_]*$")]


def _missing_reason(field: Maybe[Any], allowed: set[str]) -> None:
    if field.value is None and field.null_reason not in allowed:
        raise ValueError("Null reason is not valid for this field.")


class Time(Closed):
    session_relative_seconds: Maybe[Finite]
    recording_relative_seconds: Maybe[Finite]
    utc: Maybe[UtcString]

    @model_validator(mode="after")
    def clock_reasons(self) -> Time:
        _missing_reason(self.session_relative_seconds, {"clock_unavailable", "not_applicable"})
        _missing_reason(self.utc, {"clock_unavailable", "not_applicable"})
        _missing_reason(
            self.recording_relative_seconds,
            {
                "recording_not_enabled",
                "recorder_never_started",
                "clock_unavailable",
                "not_yet_observed",
                "not_applicable",
            },
        )
        return self


class ClockMapping(Closed):
    """Same-process origins; no raw monotonic epoch is exported."""

    session_origin: Finite
    session_utc: datetime
    recording_origin: Finite | None = None
    recording_pending: bool = False

    @model_validator(mode="after")
    def utc_origin(self) -> ClockMapping:
        if self.session_utc.tzinfo is None or self.session_utc.utcoffset() != timedelta(0):
            raise ValueError("Session clock origin must be explicit UTC.")
        return self

    def at(self, instant: float) -> Time:
        elapsed = instant - self.session_origin
        return Time(
            session_relative_seconds=present(elapsed),
            recording_relative_seconds=(
                absent("not_yet_observed" if self.recording_pending else "recorder_never_started")
                if self.recording_origin is None
                else present(instant - self.recording_origin)
            ),
            utc=present((self.session_utc + timedelta(seconds=elapsed)).isoformat()),
        )


class ModelIdentity(Closed):
    status: Literal["pending", "resolved", "unresolved", "failed"]
    repo_id: str
    revision: Maybe[str]
    precision: str
    onnx_model_filename: str
    feature_extractor_filename: str
    onnx_model_sha256: Maybe[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    feature_extractor_config_sha256: Maybe[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    failure: Maybe[DiagnosticReason]

    @model_validator(mode="after")
    def identity_reasons(self) -> ModelIdentity:
        _missing_reason(self.revision, {"hub_default_revision", "resolution_failed"})
        for digest in (self.onnx_model_sha256, self.feature_extractor_config_sha256):
            _missing_reason(
                digest,
                {"resolution_failed", "hash_failed", "load_not_completed", "model_never_loaded"},
            )
            if self.status == "resolved" and digest.value is None:
                raise ValueError("Resolved identity requires actual artifact digests.")
        _missing_reason(self.failure, {"not_applicable", "not_yet_observed"})
        if self.status == "failed" and self.failure.value is None:
            raise ValueError("Failed identity requires a sanitised failure code.")
        return self


class Settings(Closed):
    confidence_threshold: Probability
    min_positive_windows: Annotated[int, Field(strict=True, gt=0)]
    confirmation_window_seconds: Annotated[float, Field(gt=0)]
    window_seconds: Annotated[float, Field(gt=0)]
    effective_hop_seconds: Annotated[float, Field(gt=0)]
    sample_rate: Annotated[int, Field(strict=True, gt=0)]


class Counts(Closed):
    observed_windows: NonNegative = 0
    eligible_windows: NonNegative = 0
    undecided_eligible_windows: NonNegative = 0
    capture_dropped_windows: NonNegative = 0
    stale_results_discarded: NonNegative = 0
    late_results_discarded_before_freeze: NonNegative = 0
    duplicate_confirmations_rejected: NonNegative = 0
    persistence_failed_records: NonNegative = 0
    persistence_pending_records: NonNegative = 0


class IdentityPayload(Closed):
    identity: ModelIdentity
    settings: Settings
    load_available_at: Time


class WindowPayload(Closed):
    sequence: NonNegative
    probability: Maybe[Probability]
    positive_window_count: NonNegative
    window_status: Literal["negative", "candidate", "confirmed", "error", "not_inferred"]
    window_start: Time
    window_end: Time
    decision_available_at: Time
    inference_duration_ms: Annotated[float, Field(ge=0)]
    queue_depth: NonNegative
    capture_dropped_windows: NonNegative
    overflow_count_last_minute: NonNegative
    eligibility: Literal["eligible", "ineligible_pre_charge", "ineligible_post_drop"]
    availability_class: Literal["before_drop", "after_drop", "drop_not_observed"]
    human_mark_seen_at_decision: bool

    @model_validator(mode="after")
    def probability_reason(self) -> WindowPayload:
        _missing_reason(self.probability, {"inference_failed", "not_applicable"})
        decision = self.decision_available_at
        no_decision = all(
            field.value is None and field.null_reason == "not_applicable"
            for field in (
                decision.session_relative_seconds,
                decision.recording_relative_seconds,
                decision.utc,
            )
        )
        if self.window_status == "not_inferred":
            if (
                self.probability.value is not None
                or self.probability.null_reason != "not_applicable"
                or self.eligibility == "eligible"
                or not no_decision
                or self.inference_duration_ms != 0
                or self.human_mark_seen_at_decision
            ):
                raise ValueError(
                    "not_inferred requires an ineligible, unscored window and no decision instant."
                )
        elif no_decision:
            raise ValueError(
                "An attempted inference cannot claim a not-applicable decision instant."
            )
        return self


class ConfirmationPayload(Closed):
    first_confirmation_key: str
    confirming_window_sequence: NonNegative
    model_onset: Time
    model_onset_inferred: bool
    confirming_window_end: Time
    decision_available_at: Time
    confirming_probability: Probability
    positive_window_count: NonNegative
    human_mark_at_acceptance: Maybe[Time]

    @model_validator(mode="after")
    def human_reason(self) -> ConfirmationPayload:
        _missing_reason(self.human_mark_at_acceptance, {"not_yet_observed"})
        return self


class HumanPayload(Closed):
    human_mark: Time
    first_confirmation_key_seen_at_acceptance: Maybe[str]

    @model_validator(mode="after")
    def confirmation_reason(self) -> HumanPayload:
        _missing_reason(
            self.first_confirmation_key_seen_at_acceptance, {"no_confirmation", "not_yet_observed"}
        )
        return self


class ReceiptPayload(Closed):
    persisted_record_key: str
    append_completed_at: Time


class SummaryPayload(Closed):
    identity: ModelIdentity
    settings: Settings
    outcome: Literal["confirmed", "no_confirmation", "unavailable", "incomplete"]
    coverage_complete: bool
    counts: Counts
    first_confirmation_key: Maybe[str]
    human_mark: Maybe[Time]
    model_onset: Maybe[Time]
    confirming_window_end: Maybe[Time]
    decision_available_at: Maybe[Time]
    drop_cutoff: Maybe[Time]
    frozen_at: Time
    max_probability: Maybe[Probability]
    order: Literal[
        "model_before_human",
        "human_before_model",
        "same_instant",
        "human_absent",
        "model_absent",
        "both_absent",
    ]
    reason: Maybe[DiagnosticReason]

    @model_validator(mode="after")
    def summary_reasons(self) -> SummaryPayload:
        for field in (
            self.first_confirmation_key,
            self.model_onset,
            self.confirming_window_end,
            self.decision_available_at,
        ):
            _missing_reason(field, {"no_confirmation", "model_never_loaded", "inference_failed"})
        _missing_reason(
            self.max_probability,
            {
                "no_confirmation",
                "model_never_loaded",
                "inference_failed",
                "load_not_completed",
                "not_applicable",
            },
        )
        _missing_reason(self.human_mark, {"not_yet_observed"})
        _missing_reason(self.drop_cutoff, {"not_yet_observed", "not_applicable"})
        _missing_reason(self.reason, {"not_applicable"})
        return self


class CommonRecord(Closed):
    schema_version: Literal[1] = 1
    record_key: str
    session_id: str
    generation: Annotated[int, Field(strict=True, gt=0)]
    event_authority: Literal["manual"] = "manual"
    observer_protocol: Literal["manual/v1"] = "manual/v1"
    accepted_at: Time


class IdentityRecord(CommonRecord):
    record_type: Literal["fc_model_identity"] = "fc_model_identity"
    payload: IdentityPayload


class WindowRecord(CommonRecord):
    record_type: Literal["fc_observation_window"] = "fc_observation_window"
    payload: WindowPayload


class ConfirmationRecord(CommonRecord):
    record_type: Literal["fc_model_first_confirmation"] = "fc_model_first_confirmation"
    payload: ConfirmationPayload


class HumanRecord(CommonRecord):
    record_type: Literal["fc_human_mark_observed"] = "fc_human_mark_observed"
    payload: HumanPayload


class ReceiptRecord(CommonRecord):
    record_type: Literal["fc_persistence_receipt"] = "fc_persistence_receipt"
    payload: ReceiptPayload


class SummaryRecord(CommonRecord):
    record_type: Literal["fc_observation_summary"] = "fc_observation_summary"
    payload: SummaryPayload


Record = Annotated[
    IdentityRecord
    | WindowRecord
    | ConfirmationRecord
    | HumanRecord
    | ReceiptRecord
    | SummaryRecord,
    Field(discriminator="record_type"),
]


class ObservationSnapshot(Closed):
    protocol: Literal["manual/v1"] = "manual/v1"
    observer_state: ObserverState
    identity: ModelIdentity
    settings: Settings
    first_confirmation: Maybe[ConfirmationPayload]
    frozen_summary: Maybe[SummaryPayload]
    durability: Literal["pending", "persisted", "failed"]
    worker_reaped: bool

    @model_validator(mode="after")
    def snapshot_reasons(self) -> ObservationSnapshot:
        _missing_reason(self.first_confirmation, {"no_confirmation", "not_yet_observed"})
        _missing_reason(self.frozen_summary, {"not_yet_observed"})
        return self
