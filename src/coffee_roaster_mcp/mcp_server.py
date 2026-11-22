"""FastMCP stdio server entrypoint for RoastPilot."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from types import TracebackType
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.server.session import ServerSession
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    StringConstraints,
    model_validator,
)

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.ambient_runtime import (
    AmbientRuntimeSnapshot,
    AmbientSessionRuntime,
    build_ambient_session_runtime,
)
from coffee_roaster_mcp.capture_evidence import (
    CaptureEvidencePublisher,
    CaptureEvidenceSnapshot,
)
from coffee_roaster_mcp.cold_temperature import (
    COLD_TEMPERATURE_NOT_ELIGIBLE,
    ColdTemperatureProjection,
    project_cold_temperature,
)
from coffee_roaster_mcp.config import (
    AmbientMode,
    AppConfig,
    ConfigError,
    FirstCrackMode,
    load_config,
    trusted_manual_driver_admission,
    validate_first_crack_authority,
)
from coffee_roaster_mcp.drivers import (
    HOTTOP_DRIVER_NAME,
    DriverLifecycleEvidence,
    ExactDropArbitration,
    ExactDropCancelled,
    FixedZeroCommandedState,
    HottopExactDropCandidate,
    HottopFirstWriteDisposition,
    HottopRoasterDriver,
    HottopStartSettlement,
    LifecycleEvidenceDriver,
    MockRoasterDriver,
    RoasterCapabilities,
    RoasterDriver,
    RoasterState,
    create_roaster_driver,
)
from coffee_roaster_mcp.exports import (
    RoastLogExport,
    SealedMockReplayExport,
    export_roast_snapshot,
    seal_mock_replay_bundle,
    verify_sealed_mock_replay_bundle,
)
from coffee_roaster_mcp.first_crack_observation import ObservationSnapshot
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackSessionRuntime,
    RecordingArtifactPlan,
    build_first_crack_session_runtime,
)
from coffee_roaster_mcp.hottop_simulation import (
    SYNTHETIC_HOTTOP_PORT,
    HottopSimulationComposition,
    build_trusted_simulation_ambient_reader_factory,
    build_trusted_simulation_serial_factory,
)
from coffee_roaster_mcp.session import (
    CaptureSubject,
    DriverCommandReservation,
    EventLogRecordingError,
    EventPayloadValue,
    ManualFirstCrackAttempt,
    MockDropPersistenceTicket,
    NormalDropIdentity,
    NormalDropNonDispatchProof,
    NormalDropSettlement,
    RecoveryPersistenceSupersededError,
    RoastEvent,
    RoastPhase,
    RoastSession,
    RoastSessionStore,
    SessionLifecycleError,
    SessionPresence,
    SessionPurpose,
    StagedSessionStartPublication,
    _is_valid_mock_drop_projection,  # pyright: ignore[reportPrivateUsage]
    compute_roast_metrics,
    default_emergency_safety_payload,
    has_verified_emergency_containment,
)

FixedZeroSessionId = Annotated[str, Strict(), StringConstraints(min_length=1, max_length=128)]
_FIXED_ZERO_DRIVER_IDENTIFIER_PATTERN = r"[a-z][a-z0-9_-]{0,63}"
FixedZeroOutcome = Literal[
    "refused_identity",
    "refused_capability",
    "refused_busy",
    "command_state_returned",
    "unknown_driver_result",
    "unknown_completion",
]
FixedZeroReason = Literal[
    "unknown_session",
    "not_latest_session",
    "session_not_active",
    "session_faulted",
    "capability_absent",
    "capability_version_unsupported",
    "capability_malformed",
    "capability_unreadable",
    "capability_port_missing",
    "finalisation_in_progress",
    "command_in_progress",
    "driver_raised",
    "driver_result_missing",
    "driver_result_invalid",
    "completion_ownership_changed",
    "projection_unconfirmed",
]
CaptureEvidenceSessionId = Annotated[str, Strict(), StringConstraints(min_length=1, max_length=128)]
CaptureEvidenceOutcome = Literal["refused", "unavailable", "observed"]
CaptureEvidenceReason = Literal[
    "not_configured",
    "no_session",
    "wrong_session",
    "inventory_too_large",
    "store_contended",
    "publication_contended",
    "subject_changed",
    "start_in_flight",
    "clock_unreliable",
    "capture_pending",
    "setup_failed",
    "recorder_unavailable",
]


class _CaptureEvidenceArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for capture evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    capture_protocol: Literal["capture_evidence/v1"]
    intent: Literal["bootstrap", "current_session"]
    expected_session_id: CaptureEvidenceSessionId | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_exact_variant(cls, value: object) -> object:
        """Reject nulls, coercion, extras, and mixed request variants."""
        if not isinstance(value, Mapping):
            raise TypeError("Capture evidence arguments must be a mapping.")
        raw = cast(Mapping[str, object], value)
        intent = raw.get("intent")
        required = {"capture_protocol", "intent"}
        if intent == "current_session":
            required.add("expected_session_id")
        if set(raw) != required:
            raise ValueError("Capture evidence request has an invalid field set.")
        if type(raw.get("capture_protocol")) is not str or type(intent) is not str:
            raise TypeError("Capture evidence protocol and intent must be strings.")
        if intent == "current_session" and type(raw.get("expected_session_id")) is not str:
            raise TypeError("expected_session_id must be a string.")
        return raw


class CaptureEvidenceInputResult(BaseModel):
    """Closed public row for one current capture input."""

    model_config = ConfigDict(extra="forbid", strict=True)

    slot: Annotated[int, Strict(), Field(ge=1, le=8)]
    role: Literal["detector_teed", "additional_independent"]
    input_generation: Annotated[int, Strict(), Field(ge=1, le=2**63 - 1)] | None
    opened_input_token: (
        Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{32}$")] | None
    )
    identity: Literal["unopened", "unresolved", "resolved", "aliased"]
    requested_rate_hz: Annotated[int, Strict(), Field(gt=0, le=2**63 - 1)]
    reported_rate_hz: Annotated[float, Strict(), Field(gt=0, allow_inf_nan=False)] | None
    wav_header_rate_hz: Annotated[float, Strict(), Field(gt=0, allow_inf_nan=False)] | None
    input_frames: Annotated[int, Strict(), Field(ge=0, le=2**63 - 1)] | None
    accepted_frames: Annotated[int, Strict(), Field(ge=0, le=2**63 - 1)] | None
    flushed_frames: Annotated[int, Strict(), Field(ge=0, le=2**63 - 1)] | None
    state: Literal["pending", "capturing", "closing", "closed", "failed"]
    failure: (
        Literal["open_failed", "read_failed", "write_failed", "close_unconfirmed", "unknown"] | None
    )

    @model_validator(mode="after")
    def _validate_row(self) -> CaptureEvidenceInputResult:
        """Reject impossible identity, terminal, and counter combinations."""
        tokened = self.identity in {"resolved", "aliased"}
        if tokened != (self.opened_input_token is not None):
            raise ValueError("Capture token must exactly follow resolved identity.")
        if self.identity == "unopened":
            if self.input_generation is not None or any(
                value is not None
                for value in (
                    self.reported_rate_hz,
                    self.wav_header_rate_hz,
                    self.input_frames,
                    self.accepted_frames,
                    self.flushed_frames,
                )
            ):
                raise ValueError("Unopened input cannot have a generation.")
        elif self.input_generation is None:
            raise ValueError("Opened input requires a generation.")
        if self.state == "failed":
            if self.failure is None:
                raise ValueError("Failed input requires a failure reason.")
        elif self.state == "closing":
            if self.failure not in {None, "close_unconfirmed"}:
                raise ValueError("Closing input has an invalid failure reason.")
        elif self.failure is not None:
            raise ValueError("Only failed or closing input may carry failure.")
        if (
            self.flushed_frames is not None
            and self.accepted_frames is not None
            and self.flushed_frames > self.accepted_frames
        ):
            raise ValueError("Flushed frames cannot exceed accepted frames.")
        return self


class CaptureEvidenceBootstrapResult(BaseModel):
    """Exact static bootstrap response for capture evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    capture_protocol: Literal["capture_evidence/v1"]
    intent: Literal["bootstrap"]
    capability: Literal["manual_mock_capture", "manual_hottop_simulation_capture", "not_configured"]


class CaptureEvidenceResult(BaseModel):
    """Exact public current-session response for bounded evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    capture_protocol: Literal["capture_evidence/v1"]
    intent: Literal["current_session"]
    outcome: CaptureEvidenceOutcome
    reason: CaptureEvidenceReason | None
    session_id: CaptureEvidenceSessionId | None = None
    session_state: Literal["active", "stopped"] | None = None
    recorder_generation: Annotated[int, Strict(), Field(ge=1, le=2**63 - 1)] | None = None
    publication_sequence: Annotated[int, Strict(), Field(ge=1, le=2**63 - 1)] | None = None
    age_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)] | None = None
    inputs: tuple[CaptureEvidenceInputResult, ...] = ()

    @model_validator(mode="after")
    def _validate_variant(self) -> CaptureEvidenceResult:
        """Keep current-session result combinations closed and explicit."""
        observed = self.outcome == "observed"
        fields = (
            self.session_id,
            self.session_state,
            self.recorder_generation,
            self.publication_sequence,
            self.age_seconds,
        )
        if observed:
            if self.reason is not None or any(value is None for value in fields) or not self.inputs:
                raise ValueError("Observed response requires complete evidence.")
            if tuple(row.slot for row in self.inputs) != tuple(range(1, len(self.inputs) + 1)):
                raise ValueError("Observed inventory slots must be contiguous.")
            if any(
                row.role != ("detector_teed" if row.slot == 1 else "additional_independent")
                for row in self.inputs
            ):
                raise ValueError("Observed inventory roles must match slot order.")
            token_counts = {
                token: sum(row.opened_input_token == token for row in self.inputs)
                for token in {
                    row.opened_input_token for row in self.inputs if row.opened_input_token
                }
            }
            if any(
                row.identity == "aliased"
                and (row.opened_input_token is None or token_counts[row.opened_input_token] < 2)
                for row in self.inputs
            ):
                raise ValueError("Aliased input requires a shared current token.")
            for row in self.inputs:
                if row.opened_input_token is None:
                    continue
                shared = token_counts[row.opened_input_token]
                if (row.identity == "aliased") != (shared > 1):
                    raise ValueError("Capture token identity must agree in both directions.")
            return self
        if self.reason is None:
            raise ValueError("Non-observed response requires a reason.")
        if self.outcome == "refused" and self.reason not in {
            "not_configured",
            "no_session",
            "wrong_session",
            "inventory_too_large",
        }:
            raise ValueError("Refused response has an invalid reason.")
        if self.outcome == "unavailable" and self.reason not in {
            "store_contended",
            "publication_contended",
            "subject_changed",
            "start_in_flight",
            "clock_unreliable",
            "capture_pending",
            "setup_failed",
            "recorder_unavailable",
        }:
            raise ValueError("Unavailable response has an invalid reason.")
        if self.reason in {"capture_pending", "setup_failed", "recorder_unavailable"}:
            if (
                self.session_id is None
                or self.session_state is None
                or any(value is not None for value in fields[2:])
                or self.inputs
            ):
                raise ValueError("Matched unavailable response has an invalid payload.")
        elif any(value is not None for value in fields) or self.inputs:
            raise ValueError("Safe empty response cannot carry session evidence.")
        return self


class _FixedZeroContainmentArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for the fixed-zero containment tool."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_session_id: FixedZeroSessionId
    containment_protocol: Literal["fixed_zero/v1"]


class _LegacyHumanMarkArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for the legacy human first-crack mark."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _HottopSimulationStartArguments(ArgModelBase):
    """Strict raw SDK arguments for trusted Hottop simulation Start."""

    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: Literal["roast"]
    first_crack_observer_ack: Literal["manual/v1"]


class FixedZeroCommandedStateResult(BaseModel):
    """Exact eight-key driver command-state evidence exposed by the tool."""

    model_config = ConfigDict(extra="forbid", strict=True)

    driver: Annotated[
        str, Strict(), StringConstraints(pattern=_FIXED_ZERO_DRIVER_IDENTIFIER_PATTERN)
    ]
    capability_version: Literal[1]
    heat_level_percent: Literal[0]
    roast_fan_level_percent: Annotated[int, Strict(), Field(ge=0, le=100)]
    main_fan_level_percent: Annotated[int, Strict(), Field(ge=0, le=100)]
    drum_motor_on: StrictBool
    cooling_motor_on: StrictBool
    solenoid_open: StrictBool

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_fixed_zero_evidence(cls, value: object) -> object:
        """Reject pre-coercion literals and non-full driver identities."""
        if not isinstance(value, Mapping):
            raise TypeError("Fixed-zero commanded state must be a mapping.")
        raw_value = cast(Mapping[str, object], value)
        driver = raw_value.get("driver")
        if (
            not isinstance(driver, str)
            or re.fullmatch(_FIXED_ZERO_DRIVER_IDENTIFIER_PATTERN, driver, flags=re.ASCII) is None
        ):
            raise ValueError("driver must be a full ASCII fixed-zero identifier.")
        for field_name, expected in (
            ("capability_version", 1),
            ("heat_level_percent", 0),
        ):
            raw = raw_value.get(field_name)
            if type(raw) is not int or raw != expected:
                raise ValueError(f"{field_name} must be the exact integer {expected}.")
        for field_name in ("roast_fan_level_percent", "main_fan_level_percent"):
            raw = raw_value.get(field_name)
            if type(raw) is not int or not 0 <= raw <= 100:
                raise ValueError(f"{field_name} must be an exact integer in 0..100.")
        for field_name in ("drum_motor_on", "cooling_motor_on", "solenoid_open"):
            if type(raw_value.get(field_name)) is not bool:
                raise TypeError(f"{field_name} must be an exact boolean.")
        return raw_value


class FixedZeroContainmentResult(BaseModel):
    """Closed eleven-field business result for fixed-zero containment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    containment_protocol: Literal["fixed_zero/v1"]
    containment_version: Literal[1]
    outcome: FixedZeroOutcome
    request_dispatch: Literal["not_invoked", "invoked"]
    automatic_retry_permitted: Literal[False]
    requested_session_id: FixedZeroSessionId
    validated_session_id: FixedZeroSessionId | None
    commanded_state: FixedZeroCommandedStateResult | None
    session_projection: Literal["recorded_in_memory", "unknown", "not_changed"]
    durability: Literal["not_provided_by_this_tool"]
    reason_code: FixedZeroReason | None

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_result_literals(cls, value: object) -> object:
        """Reject Pydantic Literal coercion before closed-result validation."""
        if not isinstance(value, Mapping):
            raise TypeError("Fixed-zero result must be a mapping.")
        raw_value = cast(Mapping[str, object], value)
        if (
            type(raw_value.get("containment_version")) is not int
            or raw_value.get("containment_version") != 1
        ):
            raise ValueError("containment_version must be the exact integer 1.")
        if (
            type(raw_value.get("automatic_retry_permitted")) is not bool
            or raw_value.get("automatic_retry_permitted") is not False
        ):
            raise ValueError("automatic_retry_permitted must be the exact boolean false.")
        return raw_value

    @model_validator(mode="after")
    def _validate_outcome_row(self) -> FixedZeroContainmentResult:
        """Reject every impossible cross-field outcome combination."""
        accepted: dict[
            FixedZeroOutcome,
            tuple[str, bool, str, frozenset[FixedZeroReason] | None],
        ] = {
            "refused_identity": (
                "not_invoked",
                False,
                "not_changed",
                frozenset(
                    {
                        "unknown_session",
                        "not_latest_session",
                        "session_not_active",
                        "session_faulted",
                    }
                ),
            ),
            "refused_capability": (
                "not_invoked",
                True,
                "not_changed",
                frozenset(
                    {
                        "capability_absent",
                        "capability_version_unsupported",
                        "capability_malformed",
                        "capability_unreadable",
                        "capability_port_missing",
                    }
                ),
            ),
            "refused_busy": (
                "not_invoked",
                True,
                "not_changed",
                frozenset({"finalisation_in_progress", "command_in_progress"}),
            ),
            "command_state_returned": ("invoked", True, "recorded_in_memory", None),
            "unknown_driver_result": (
                "invoked",
                True,
                "unknown",
                frozenset({"driver_raised", "driver_result_missing", "driver_result_invalid"}),
            ),
            "unknown_completion": (
                "invoked",
                True,
                "unknown",
                frozenset({"completion_ownership_changed", "projection_unconfirmed"}),
            ),
        }
        dispatch, requires_validated_id, projection, reasons = accepted[self.outcome]
        if self.request_dispatch != dispatch or self.session_projection != projection:
            raise ValueError("Fixed-zero result has an invalid outcome projection.")
        if requires_validated_id != (self.validated_session_id is not None):
            raise ValueError("Fixed-zero result has an invalid validated session id.")
        if self.validated_session_id is not None and (
            self.validated_session_id != self.requested_session_id
        ):
            raise ValueError("validated_session_id must equal requested_session_id.")
        requires_state = self.outcome in {"command_state_returned", "unknown_completion"}
        if requires_state != (self.commanded_state is not None):
            raise ValueError("Fixed-zero result has an invalid commanded state.")
        if reasons is None:
            if self.reason_code is not None:
                raise ValueError("Successful fixed-zero result cannot have a reason code.")
        elif self.reason_code not in reasons:
            raise ValueError("Fixed-zero result has an invalid reason code.")
        return self


TimingEvidenceOutcome = Literal[
    "evidence", "refused_identity", "refused_capability", "refused_busy", "unavailable"
]
TimingEvidenceReason = Literal[
    "unknown_session",
    "not_latest_session",
    "session_faulted",
    "session_not_active",
    "session_purpose_not_eligible",
    "session_changed_during_read",
    "capability_unreadable",
    "capability_malformed",
    "capability_absent",
    "capability_version_unsupported",
    "composition_not_qualified",
    "finalisation_in_progress",
    "drop_in_progress",
    "clock_invalid",
    "driver_raised",
    "driver_result_invalid",
    "driver_not_connected",
    "clock_regression",
    "out_of_order_read",
    "sequence_exhausted",
    "record_invalid",
]
_TIMING_EPOCH_PATTERN = r"^[0-9a-f]{32}$"
TimingEpochId = Annotated[str, Strict(), StringConstraints(pattern=_TIMING_EPOCH_PATTERN)]
TimingExactInt = Annotated[int, Strict(), Field(ge=0, le=2**53 - 1)]
TimingPositiveInt = Annotated[int, Strict(), Field(ge=1, le=2**53 - 1)]
TimingFiniteFloat = Annotated[float, Strict(), Field(allow_inf_nan=False)]


class _ExactNormalDropArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for exact mock-session Drop."""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: FixedZeroSessionId
    clock_epoch_id: TimingEpochId
    ownership_sequence: TimingPositiveInt
    timing_evidence_sequence: TimingPositiveInt


_EVENT_EVIDENCE_SCHEMA_CONDITIONALS: dict[str, Any] = {
    "allOf": [
        {
            "if": {"properties": {"source": {"const": "model_detection"}}},
            "then": {"properties": {"time_basis": {"const": "detector_clock_rebased"}}},
            "else": {"properties": {"time_basis": {"const": "producer_canonical_record"}}},
        },
        {
            "if": {"properties": {"kind": {"const": "beans_added"}}},
            "then": {
                "properties": {"source": {"enum": ["manual_mark", "auto_t0", "unattributed"]}}
            },
        },
        {
            "if": {"properties": {"kind": {"const": "first_crack_detected"}}},
            "then": {
                "properties": {
                    "source": {"enum": ["manual_mark", "model_detection", "unattributed"]}
                }
            },
        },
        {
            "if": {"properties": {"kind": {"const": "beans_dropped"}}},
            "then": {"properties": {"source": {"const": "unattributed"}}},
        },
    ]
}


def _timing_mapping(
    value: object,
    *,
    string_fields: tuple[str, ...] = (),
    integer_fields: tuple[str, ...] = (),
    float_fields: tuple[str, ...] = (),
) -> Mapping[str, object]:
    """Reject coercible primitive values before Pydantic validates timing evidence."""
    if not isinstance(value, Mapping):
        raise TypeError("Timing evidence values must be mappings.")
    raw = cast(Mapping[str, object], value)
    for field_name in string_fields:
        if type(raw.get(field_name)) is not str:
            raise TypeError(f"{field_name} must be an exact string.")
    for field_name in integer_fields:
        if type(raw.get(field_name)) is not int:
            raise TypeError(f"{field_name} must be an exact integer.")
    for field_name in float_fields:
        if type(raw.get(field_name)) is not float:
            raise TypeError(f"{field_name} must be an exact float.")
    return raw


class _TimingEvidenceArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for synthetic timing evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_session_id: FixedZeroSessionId
    evidence_protocol: Literal["timing_evidence/v1"]


class _HottopReceptionEvidenceArguments(ArgModelBase):
    """Strict raw SDK-boundary arguments for simulated Hottop reception evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_session_id: FixedZeroSessionId
    evidence_protocol: Literal["hottop_reception_evidence/v1"]


_HOTTOP_RECEPTION_EVIDENCE_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "expected_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "evidence_protocol": {"const": "hottop_reception_evidence/v1"},
    },
    "required": ["expected_session_id", "evidence_protocol"],
    "additionalProperties": False,
}


class TimingClock(BaseModel):
    """Closed identifier for the store-relative synthetic clock."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    epoch_id: TimingEpochId
    domain: Literal["session_relative_store_monotonic"]
    suspend_behaviour: Literal["unqualified"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(value, string_fields=("epoch_id", "domain", "suspend_behaviour"))


class ValidChannelEvidence(BaseModel):
    """One synthetic mock channel with an in-call clock bracket."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["valid"]
    celsius: TimingFiniteFloat
    acquisition_lo_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    acquisition_hi_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    clock_epoch_id: TimingEpochId
    basis: Literal["in_call_synthesis"]
    qualification: Literal["synthetic_software_only"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(
            value,
            string_fields=("status", "clock_epoch_id", "basis", "qualification"),
            float_fields=("celsius", "acquisition_lo_seconds", "acquisition_hi_seconds"),
        )

    @model_validator(mode="after")
    def _validate_bracket(self) -> ValidChannelEvidence:
        if self.acquisition_lo_seconds > self.acquisition_hi_seconds:
            raise ValueError("Channel acquisition bracket must be ordered.")
        return self


class UnavailableChannelEvidence(BaseModel):
    """One channel explicitly reported absent by the driver."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["unavailable"]
    reason: Literal["reported_none"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(value, string_fields=("status", "reason"))


class InvalidChannelEvidence(BaseModel):
    """One non-coercible malformed channel reported by the driver."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["invalid"]
    reason: Literal["not_exact_float", "non_finite"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(value, string_fields=("status", "reason"))


ChannelEvidence = Annotated[
    ValidChannelEvidence | UnavailableChannelEvidence | InvalidChannelEvidence,
    Field(discriminator="status"),
]


class EventEvidence(BaseModel):
    """Canonical producer event operand with store-side provenance."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra=_EVENT_EVIDENCE_SCHEMA_CONDITIONALS,
    )

    kind: Literal["beans_added", "first_crack_detected", "beans_dropped"]
    event_index: TimingExactInt
    canonical_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    clock_epoch_id: TimingEpochId
    source: Literal["manual_mark", "auto_t0", "model_detection", "unattributed"]
    time_basis: Literal["producer_canonical_record", "detector_clock_rebased"]
    physical_time_bound: Literal["not_provided"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(
            value,
            string_fields=(
                "kind",
                "clock_epoch_id",
                "source",
                "time_basis",
                "physical_time_bound",
            ),
            integer_fields=("event_index",),
            float_fields=("canonical_seconds",),
        )

    @model_validator(mode="after")
    def _validate_source_time_basis(self) -> EventEvidence:
        allowed_sources = {
            "beans_added": frozenset({"manual_mark", "auto_t0", "unattributed"}),
            "first_crack_detected": frozenset({"manual_mark", "model_detection", "unattributed"}),
            "beans_dropped": frozenset({"unattributed"}),
        }
        if self.source not in allowed_sources[self.kind]:
            raise ValueError("Event source must match event kind.")
        expected = (
            "detector_clock_rebased"
            if self.source == "model_detection"
            else "producer_canonical_record"
        )
        if self.time_basis != expected:
            raise ValueError("Event time basis must match provenance.")
        return self


class TimingEvidenceRecord(BaseModel):
    """Closed atomic synthetic timing evidence record for one roast session."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    session_id: FixedZeroSessionId
    session_ownership_sequence: TimingPositiveInt
    driver: Literal["mock"]
    clock: TimingClock
    evidence_sequence: TimingPositiveInt
    phase: Literal["pre_roast", "roasting", "development", "dropped", "cooling"]
    charge: EventEvidence | None
    first_crack: EventEvidence | None
    drop: EventEvidence | None
    bean: ChannelEvidence
    environment: ChannelEvidence
    time_basis: Literal["canonical_operational_session_relative"]
    physical_qualification: Literal["none_synthetic_mock_only"]

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        return _timing_mapping(
            value,
            string_fields=("session_id", "driver", "phase", "time_basis", "physical_qualification"),
            integer_fields=("session_ownership_sequence", "evidence_sequence"),
        )

    @model_validator(mode="after")
    def _validate_record(self) -> TimingEvidenceRecord:
        events = (
            ("charge", self.charge, "beans_added"),
            ("first_crack", self.first_crack, "first_crack_detected"),
            ("drop", self.drop, "beans_dropped"),
        )
        if any(event is not None and event.kind != kind for _, event, kind in events):
            raise ValueError("Event kind must match its record slot.")
        if any(
            event is not None and event.clock_epoch_id != self.clock.epoch_id
            for _, event, _ in events
        ):
            raise ValueError("Event epoch must match the record clock.")
        for channel in (self.bean, self.environment):
            if (
                isinstance(channel, ValidChannelEvidence)
                and channel.clock_epoch_id != self.clock.epoch_id
            ):
                raise ValueError("Channel epoch must match the record clock.")
        phase_events = (
            self.charge is not None,
            self.first_crack is not None,
            self.drop is not None,
        )
        allowed = {
            "pre_roast": (False, False, False),
            "roasting": (True, False, False),
            "development": (True, True, False),
        }
        if self.phase in allowed and phase_events != allowed[self.phase]:
            raise ValueError("Phase and event slots are inconsistent.")
        if self.phase in {"dropped", "cooling"} and (not phase_events[0] or not phase_events[2]):
            raise ValueError("Dropped or cooling phase requires charge and drop.")
        present = [event for _, event, _ in events if event is not None]
        if any(
            later.event_index <= earlier.event_index
            or later.canonical_seconds < earlier.canonical_seconds
            for earlier, later in zip(present, present[1:], strict=False)
        ):
            raise ValueError("Present timing events must preserve canonical timeline order.")
        if (
            isinstance(self.bean, ValidChannelEvidence)
            and isinstance(self.environment, ValidChannelEvidence)
            and (
                self.bean.acquisition_lo_seconds != self.environment.acquisition_lo_seconds
                or self.bean.acquisition_hi_seconds != self.environment.acquisition_hi_seconds
            )
        ):
            raise ValueError("Valid channels must share the in-call bracket.")
        return self


class TimingEvidenceResult(BaseModel):
    """Closed result for one read-only timing-evidence request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    evidence_protocol: Literal["timing_evidence/v1"]
    evidence_version: Literal[1]
    outcome: TimingEvidenceOutcome
    reason_code: TimingEvidenceReason | None
    driver_read: Literal["not_invoked", "invoked"]
    requested_session_id: FixedZeroSessionId
    record: TimingEvidenceRecord | None

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, value: object) -> object:
        raw = _timing_mapping(
            value,
            string_fields=("evidence_protocol", "outcome", "driver_read", "requested_session_id"),
            integer_fields=("evidence_version",),
        )
        reason = raw.get("reason_code")
        if reason is not None and type(reason) is not str:
            raise TypeError("reason_code must be an exact string or null.")
        return raw

    @model_validator(mode="after")
    def _validate_outcome(self) -> TimingEvidenceResult:
        rows: dict[TimingEvidenceOutcome, tuple[frozenset[str] | None, frozenset[str]]] = {
            "evidence": (None, frozenset({"invoked"})),
            "refused_identity": (
                frozenset(
                    {
                        "unknown_session",
                        "not_latest_session",
                        "session_faulted",
                        "session_not_active",
                        "session_purpose_not_eligible",
                        "session_changed_during_read",
                    }
                ),
                frozenset({"not_invoked", "invoked"}),
            ),
            "refused_capability": (
                frozenset(
                    {
                        "capability_unreadable",
                        "capability_malformed",
                        "capability_absent",
                        "capability_version_unsupported",
                        "composition_not_qualified",
                    }
                ),
                frozenset({"not_invoked"}),
            ),
            "refused_busy": (
                frozenset({"finalisation_in_progress", "drop_in_progress"}),
                frozenset({"not_invoked", "invoked"}),
            ),
            "unavailable": (
                frozenset(
                    {
                        "clock_invalid",
                        "driver_raised",
                        "driver_result_invalid",
                        "driver_not_connected",
                        "clock_regression",
                        "out_of_order_read",
                        "sequence_exhausted",
                        "record_invalid",
                    }
                ),
                frozenset({"not_invoked", "invoked"}),
            ),
        }
        reasons, reads = rows[self.outcome]
        if self.driver_read not in reads or (reasons is None) != (self.reason_code is None):
            raise ValueError("Timing evidence result has an invalid outcome row.")
        if reasons is not None and self.reason_code not in reasons:
            raise ValueError("Timing evidence result has an invalid reason code.")
        if (
            self.outcome == "refused_identity"
            and self.reason_code != "session_changed_during_read"
            and self.driver_read != "not_invoked"
        ):
            raise ValueError("Pre-read identity refusals must not invoke the driver.")
        if (
            self.outcome == "refused_identity"
            and self.reason_code == "session_changed_during_read"
            and self.driver_read != "invoked"
        ):
            raise ValueError("Post-read identity changes require an invoked read.")
        if (
            self.outcome == "unavailable"
            and self.reason_code != "clock_invalid"
            and self.driver_read != "invoked"
        ):
            raise ValueError("Non-clock unavailable rows require an invoked driver read.")
        requires_record = self.outcome == "evidence"
        if requires_record != (self.record is not None):
            raise ValueError("Timing evidence record must exactly follow evidence outcome.")
        if self.record is not None and self.record.session_id != self.requested_session_id:
            raise ValueError("Timing evidence record must match the requested session.")
        return self


HottopReceptionReason = Literal[
    "unknown_session",
    "not_latest_session",
    "session_faulted",
    "session_not_active",
    "session_purpose_not_eligible",
    "session_changed_during_read",
    "capability_absent",
    "capability_version_unsupported",
    "composition_not_simulated",
    "composition_mismatch",
    "finalisation_in_progress",
    "drop_in_progress",
    "clock_invalid",
    "clock_regression",
    "driver_raised",
    "driver_not_streaming",
    "out_of_order_read",
    "sequence_exhausted",
    "record_invalid",
]
HottopReceiptToken = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class HottopReceptionComposition(BaseModel):
    """Closed trusted-composition identity for a simulated reception record."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["simulated_injected_transport"]
    composition_id: HottopReceiptToken
    reception_capability_version: Literal[1]


class HottopReceptionDecision(BaseModel):
    """Store-clock bracket around the immutable driver snapshot decision."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operational_lo_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    operational_hi_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    clock_epoch_id: TimingEpochId
    basis: Literal["producer_classification_bracket"]

    @model_validator(mode="after")
    def _validate_order(self) -> HottopReceptionDecision:
        if self.operational_lo_seconds > self.operational_hi_seconds:
            raise ValueError("Operational decision bracket must be ordered.")
        return self


class HottopReceptionReceipt(BaseModel):
    """Driver-owned receipt identity that accompanies an eligible temperature pair."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    epoch: TimingPositiveInt
    sequence: TimingPositiveInt
    basis: Literal["host_receipt_after_drained_floor"]


class HottopReceptionHostClock(BaseModel):
    """Closed statement of the driver host-clock relationship."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    domain: Literal["driver_reception_monotonic"]
    relation_to_operational: Literal["not_established"]


class HottopEligibleReceptionPair(BaseModel):
    """A pair from one immutable eligible driver receipt snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["eligible"]
    bean_c: TimingFiniteFloat
    environment_c: TimingFiniteFloat
    unit: Literal["celsius"]
    receipt_token: HottopReceiptToken
    reception: HottopReceptionReceipt
    host_receipt_clock: HottopReceptionHostClock
    drained_floor_age_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    read_returned_age_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    age_bound_seconds: Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
    age_bound_basis: Literal["simulation_composition_unqualified"]

    @model_validator(mode="after")
    def _validate_host_clock(self) -> HottopEligibleReceptionPair:
        if not (
            self.read_returned_age_seconds
            <= self.drained_floor_age_seconds
            <= self.age_bound_seconds
        ):
            raise ValueError("Reception host ages must be ordered within the configured bound.")
        return self


class HottopIneligibleReceptionPair(BaseModel):
    """Closed host-receipt refusal facts with no caller-supplied temperature pair."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["ineligible"]
    reasons: Annotated[
        tuple[
            Literal[
                "candidate_not_configured",
                "no_receipt",
                "epoch_or_handle_changed",
                "superseded",
                "unit_not_explicit_celsius",
                "queue_unreadable",
                "read_size_contradiction",
                "read_truncated",
                "misaligned_or_skipped",
                "parser_remainder",
                "backlog_visible",
                "latest_pair_rejected",
                "invalid_packet_history",
                "no_drained_floor",
                "clock_regression",
                "receipt_age_bound_unset",
                "receipt_age_exceeds_bound",
            ],
            ...,
        ],
        Field(json_schema_extra={"minItems": 1, "uniqueItems": True}),
    ]

    @model_validator(mode="after")
    def _validate_reasons(self) -> HottopIneligibleReceptionPair:
        if not self.reasons or len(set(self.reasons)) != len(self.reasons):
            raise ValueError("Ineligible pair reasons must be non-empty and unique.")
        return self


HottopReceptionPair = Annotated[
    HottopEligibleReceptionPair | HottopIneligibleReceptionPair,
    Field(discriminator="status"),
]


class _HottopCommandSettlementProjection(BaseModel):
    """One host-command settlement without a physical-effect assertion."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    transition_seq: TimingPositiveInt
    result: Literal["pending", "not_attempted", "entering", "accepted_by_host", "unknown"]
    attempt_seq: TimingPositiveInt | None
    physical_effect: Literal["not_established"]


class HottopDropCommandSettlementProjection(_HottopCommandSettlementProjection):
    """The latest settlement that belongs only to the normal Drop slot."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra={
            "required": [
                "transition",
                "transition_seq",
                "result",
                "attempt_seq",
                "physical_effect",
            ]
        },
    )

    transition: Literal["drop"]


class HottopEmergencyCommandSettlementProjection(_HottopCommandSettlementProjection):
    """The latest settlement that belongs only to the emergency slot."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra={
            "required": [
                "transition",
                "transition_seq",
                "result",
                "attempt_seq",
                "physical_effect",
            ]
        },
    )

    transition: Literal["emergency"]


class HottopCommandSettlements(BaseModel):
    """Retained latest settlement projections for the synthetic composition."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    drop: HottopDropCommandSettlementProjection | None
    emergency: HottopEmergencyCommandSettlementProjection | None


class HottopReceptionEvidenceRecord(BaseModel):
    """Closed source-bound Hottop reception evidence record."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    session_id: FixedZeroSessionId
    session_ownership_sequence: TimingPositiveInt
    driver: Literal["hottop_kn8828b_2k_plus"]
    composition: HottopReceptionComposition
    clock: TimingClock
    reception_evidence_sequence: TimingPositiveInt
    phase: Literal["pre_roast", "roasting", "development", "dropped", "cooling"]
    charge: EventEvidence | None
    first_crack: EventEvidence | None
    drop: EventEvidence | None
    decision: HottopReceptionDecision
    pair: HottopReceptionPair
    command_settlements: HottopCommandSettlements
    sensor_age: Literal["unknown"]
    physical_qualification: Literal["none_simulated_transport_only"]

    @model_validator(mode="after")
    def _validate_record(self) -> HottopReceptionEvidenceRecord:
        events = (
            (self.charge, "beans_added"),
            (self.first_crack, "first_crack_detected"),
            (self.drop, "beans_dropped"),
        )
        if any(event is not None and event.kind != kind for event, kind in events):
            raise ValueError("Event kind must match the record slot.")
        if any(
            event is not None and event.clock_epoch_id != self.clock.epoch_id for event, _ in events
        ):
            raise ValueError("Event epoch must match the record clock.")
        if self.decision.clock_epoch_id != self.clock.epoch_id:
            raise ValueError("Decision epoch must match the record clock.")
        present = [event for event, _ in events if event is not None]
        if any(
            later.event_index <= earlier.event_index
            or later.canonical_seconds < earlier.canonical_seconds
            for earlier, later in zip(present, present[1:], strict=False)
        ):
            raise ValueError("Present events must preserve canonical order.")
        return self


class HottopReceptionEvidenceResult(BaseModel):
    """Closed result for one simulated Hottop reception evidence request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    evidence_protocol: Literal["hottop_reception_evidence/v1"]
    evidence_version: Literal[1]
    outcome: Literal[
        "evidence", "refused_identity", "refused_capability", "refused_busy", "unavailable"
    ]
    reason_code: HottopReceptionReason | None
    driver_read: Literal["not_invoked", "invoked"]
    requested_session_id: FixedZeroSessionId
    record: HottopReceptionEvidenceRecord | None

    @model_validator(mode="after")
    def _validate_outcome(self) -> HottopReceptionEvidenceResult:
        rows: dict[str, tuple[frozenset[str] | None, frozenset[str]]] = {
            "evidence": (None, frozenset({"invoked"})),
            "refused_identity": (
                frozenset(
                    {
                        "unknown_session",
                        "not_latest_session",
                        "session_faulted",
                        "session_not_active",
                        "session_purpose_not_eligible",
                        "session_changed_during_read",
                    }
                ),
                frozenset({"not_invoked", "invoked"}),
            ),
            "refused_capability": (
                frozenset(
                    {
                        "capability_absent",
                        "capability_version_unsupported",
                        "composition_not_simulated",
                        "composition_mismatch",
                    }
                ),
                frozenset({"not_invoked", "invoked"}),
            ),
            "refused_busy": (
                frozenset({"finalisation_in_progress", "drop_in_progress"}),
                frozenset({"not_invoked", "invoked"}),
            ),
            "unavailable": (
                frozenset(
                    {
                        "clock_invalid",
                        "clock_regression",
                        "driver_raised",
                        "driver_not_streaming",
                        "out_of_order_read",
                        "sequence_exhausted",
                        "record_invalid",
                    }
                ),
                frozenset({"not_invoked", "invoked"}),
            ),
        }
        reasons, reads = rows[self.outcome]
        if self.driver_read not in reads or (reasons is None) != (self.reason_code is None):
            raise ValueError("Hottop reception result has an invalid outcome row.")
        if reasons is not None and self.reason_code not in reasons:
            raise ValueError("Hottop reception result has an invalid reason code.")
        if (self.outcome == "evidence") != (self.record is not None):
            raise ValueError("Hottop reception record must exactly follow evidence outcome.")
        if self.record is not None and self.record.session_id != self.requested_session_id:
            raise ValueError("Hottop reception record must match the requested session.")
        return self
        return self


def _install_fixed_zero_argument_adapter(mcp: FastMCP) -> None:
    """Bind strict raw argument validation to only the new fixed-zero tool."""
    tool = mcp._tool_manager.get_tool("contain_heat_fixed_zero")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Fixed-zero containment tool was not registered.")
    existing_metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(existing_metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Fixed-zero containment argument adapter is unsupported.")
    try:
        adapted_metadata = existing_metadata.model_copy(
            update={"arg_model": _FixedZeroContainmentArguments}
        )
        binds_fixed_zero_arguments = adapted_metadata.arg_model is _FixedZeroContainmentArguments
    except Exception as exc:
        raise RuntimeError("Fixed-zero containment argument adapter is unsupported.") from exc
    if not binds_fixed_zero_arguments:
        raise RuntimeError("Fixed-zero containment argument adapter is unsupported.")
    tool.fn_metadata = adapted_metadata
    tool.parameters = _FixedZeroContainmentArguments.model_json_schema(by_alias=True)


def _install_legacy_human_mark_argument_adapter(mcp: FastMCP) -> None:
    """Bind strict empty raw arguments to the legacy human first-crack mark only."""
    tool = mcp._tool_manager.get_tool("mark_first_crack")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Legacy human mark tool was not registered.")
    existing_metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(existing_metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Legacy human mark argument adapter is unsupported.")
    try:
        adapted_metadata = existing_metadata.model_copy(
            update={"arg_model": _LegacyHumanMarkArguments}
        )
        binds_legacy_human_mark_arguments = adapted_metadata.arg_model is _LegacyHumanMarkArguments
    except Exception as exc:
        raise RuntimeError("Legacy human mark argument adapter is unsupported.") from exc
    if not binds_legacy_human_mark_arguments:
        raise RuntimeError("Legacy human mark argument adapter is unsupported.")
    tool.fn_metadata = adapted_metadata
    tool.parameters = _LegacyHumanMarkArguments.model_json_schema(by_alias=True)


def _install_hottop_simulation_start_argument_adapter(mcp: FastMCP) -> None:
    """Require explicit raw Start authority only on the simulation server."""
    tool = mcp._tool_manager.get_tool("start_roast_session")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Hottop simulation Start tool was not registered.")
    existing_metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(existing_metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Hottop simulation Start argument adapter is unsupported.")
    try:
        adapted_metadata = existing_metadata.model_copy(
            update={"arg_model": _HottopSimulationStartArguments}
        )
        binds_simulation_start = adapted_metadata.arg_model is _HottopSimulationStartArguments
    except Exception as exc:
        raise RuntimeError("Hottop simulation Start argument adapter is unsupported.") from exc
    if not binds_simulation_start:
        raise RuntimeError("Hottop simulation Start argument adapter is unsupported.")
    tool.fn_metadata = adapted_metadata
    tool.parameters = _HottopSimulationStartArguments.model_json_schema(by_alias=True)


def _install_timing_evidence_argument_adapter(mcp: FastMCP) -> None:
    """Bind strict raw argument validation to only the timing-evidence tool."""
    tool = mcp._tool_manager.get_tool("get_timing_evidence")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Timing evidence tool was not registered.")
    existing_metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(existing_metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Timing evidence argument adapter is unsupported.")
    try:
        adapted_metadata = existing_metadata.model_copy(
            update={"arg_model": _TimingEvidenceArguments}
        )
        binds_timing_arguments = adapted_metadata.arg_model is _TimingEvidenceArguments
    except Exception as exc:
        raise RuntimeError("Timing evidence argument adapter is unsupported.") from exc
    if not binds_timing_arguments:
        raise RuntimeError("Timing evidence argument adapter is unsupported.")
    tool.fn_metadata = adapted_metadata
    tool.parameters = _TimingEvidenceArguments.model_json_schema(by_alias=True)


def _install_hottop_reception_evidence_argument_adapter(mcp: FastMCP) -> None:
    """Bind the closed raw argument schema to simulation reception evidence only."""
    tool = mcp._tool_manager.get_tool("get_hottop_reception_evidence")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Hottop reception evidence tool was not registered.")
    metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Hottop reception evidence argument adapter is unsupported.")
    try:
        adapted = metadata.model_copy(
            update={
                "arg_model": _HottopReceptionEvidenceArguments,
                "output_schema": {
                    "type": "object",
                    **_hottop_reception_evidence_output_schema(),
                },
            }
        )
    except Exception as exc:
        raise RuntimeError("Hottop reception evidence argument adapter is unsupported.") from exc
    if adapted.arg_model is not _HottopReceptionEvidenceArguments:
        raise RuntimeError("Hottop reception evidence argument adapter is unsupported.")
    tool.fn_metadata = adapted
    tool.parameters = _HOTTOP_RECEPTION_EVIDENCE_INPUT_SCHEMA


def _hottop_reception_evidence_output_schema() -> dict[str, object]:
    """Return the frozen discriminated public schema, not the SDK nullable flattening.

    FastMCP otherwise advertises the Pydantic result model as one object with
    nullable fields.  The protocol deliberately exposes mutually exclusive
    outcome rows, so discovery must retain the source-bound ``oneOf`` shape.
    """
    schema = deepcopy(HottopReceptionEvidenceResult.model_json_schema())
    definitions = cast(dict[str, object], schema["$defs"])
    properties = schema.pop("properties")
    required = schema.pop("required")
    schema.pop("additionalProperties", None)
    schema.pop("type", None)
    schema.pop("title", None)
    schema.pop("description", None)

    def row(
        outcome: str,
        reasons: list[str] | None,
        reads: list[str],
        record: object,
    ) -> dict[str, object]:
        row_properties = deepcopy(properties)
        row_properties["outcome"] = {"const": outcome}
        row_properties["reason_code"] = (
            {"enum": reasons} if reasons is not None else {"enum": [None]}
        )
        row_properties["driver_read"] = {"const": reads[0]} if len(reads) == 1 else {"enum": reads}
        row_properties["record"] = record
        return {
            "type": "object",
            "properties": row_properties,
            "required": required,
            "additionalProperties": False,
        }

    record_schema = cast(dict[str, list[object]], properties["record"])["anyOf"][0]
    null_record = {"const": None}
    schema["oneOf"] = [
        row("evidence", None, ["invoked"], record_schema),
        row(
            "refused_identity",
            [
                "unknown_session",
                "not_latest_session",
                "session_faulted",
                "session_not_active",
                "session_purpose_not_eligible",
                "session_changed_during_read",
            ],
            ["not_invoked", "invoked"],
            null_record,
        ),
        row(
            "refused_capability",
            [
                "capability_absent",
                "capability_version_unsupported",
                "composition_not_simulated",
                "composition_mismatch",
            ],
            ["not_invoked", "invoked"],
            null_record,
        ),
        row(
            "refused_busy",
            ["finalisation_in_progress", "drop_in_progress"],
            ["not_invoked", "invoked"],
            null_record,
        ),
        row(
            "unavailable",
            [
                "clock_invalid",
                "clock_regression",
                "driver_raised",
                "driver_not_streaming",
                "out_of_order_read",
                "sequence_exhausted",
                "record_invalid",
            ],
            ["not_invoked", "invoked"],
            null_record,
        ),
    ]

    def canonicalize(value: object, *, preserve_event_metadata: bool = False) -> object:
        """Inline model definitions while retaining the frozen wire constraints."""
        if isinstance(value, list):
            return [
                canonicalize(item, preserve_event_metadata=preserve_event_metadata)
                for item in cast(list[object], value)
            ]
        if not isinstance(value, dict):
            return value
        raw_value = cast(dict[str, object], value)
        reference = raw_value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            definition_name = reference.removeprefix("#/$defs/")
            definition = definitions.get(definition_name)
            if definition is None:
                raise RuntimeError("Hottop reception evidence schema has an unknown definition.")
            return canonicalize(
                definition,
                preserve_event_metadata=definition_name == "EventEvidence",
            )
        canonical = {
            key: canonicalize(item, preserve_event_metadata=preserve_event_metadata)
            for key, item in raw_value.items()
            if key != "$defs"
        }
        if not preserve_event_metadata:
            canonical.pop("title", None)
            canonical.pop("description", None)
            canonical.pop("discriminator", None)
            if "const" in canonical or "enum" in canonical:
                canonical.pop("type", None)
        return canonical

    return cast(dict[str, object], canonicalize(schema))


def _install_exact_normal_drop_argument_adapter(mcp: FastMCP) -> None:
    """Bind strict raw argument validation to only the exact mock Drop tool."""
    tool = mcp._tool_manager.get_tool("drop_beans_exact_session")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Exact mock Drop tool was not registered.")
    existing_metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(existing_metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Exact mock Drop argument adapter is unsupported.")
    try:
        adapted_metadata = existing_metadata.model_copy(
            update={"arg_model": _ExactNormalDropArguments}
        )
        binds_exact_drop_arguments = adapted_metadata.arg_model is _ExactNormalDropArguments
    except Exception as exc:
        raise RuntimeError("Exact mock Drop argument adapter is unsupported.") from exc
    if not binds_exact_drop_arguments:
        raise RuntimeError("Exact mock Drop argument adapter is unsupported.")
    tool.fn_metadata = adapted_metadata
    tool.parameters = _ExactNormalDropArguments.model_json_schema(by_alias=True)


def _install_capture_evidence_argument_adapter(mcp: FastMCP) -> None:
    """Bind strict raw argument validation to the capture-evidence tool only."""
    tool = mcp._tool_manager.get_tool("get_capture_evidence")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError("Capture evidence tool was not registered.")
    metadata = tool.fn_metadata
    declared_metadata_fields = getattr(type(metadata), "model_fields", None)
    if (
        not isinstance(declared_metadata_fields, Mapping)
        or "arg_model" not in declared_metadata_fields
    ):
        raise RuntimeError("Capture evidence argument adapter is unsupported.")
    try:
        adapted = metadata.model_copy(update={"arg_model": _CaptureEvidenceArguments})
    except Exception as exc:
        raise RuntimeError("Capture evidence argument adapter is unsupported.") from exc
    if adapted.arg_model is not _CaptureEvidenceArguments:
        raise RuntimeError("Capture evidence argument adapter is unsupported.")
    tool.fn_metadata = adapted
    tool.parameters = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["capture_protocol", "intent"],
                "properties": {
                    "capture_protocol": {"const": "capture_evidence/v1"},
                    "intent": {"const": "bootstrap"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["capture_protocol", "intent", "expected_session_id"],
                "properties": {
                    "capture_protocol": {"const": "capture_evidence/v1"},
                    "intent": {"const": "current_session"},
                    "expected_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
        ],
    }


@dataclass(frozen=True)
class _ExactNormalDropBaseline:
    """Bind one mock session to command-history counts captured at its start."""

    session_id: str
    driver_ref: MockRoasterDriver
    clock_epoch_id: str
    emergency_baseline: int
    drop_baseline: int


@dataclass(frozen=True)
class _HottopReceptionIssuedEvidence:
    """Server-only binding between one public receipt token and driver snapshot."""

    session_id: str
    ownership_sequence: int
    reception_evidence_sequence: int
    receipt_token: str
    candidate: HottopExactDropCandidate


@dataclass
class _HottopReceptionEvidenceRegistry:
    """Mutable server-owned receipt binding kept outside the frozen context."""

    issued: _HottopReceptionIssuedEvidence | None = None

    def install(self, issued: _HottopReceptionIssuedEvidence | None) -> None:
        """Replace the current-owner binding after a successful evidence read."""
        self.issued = issued


ExactNormalDropOutcome = Literal[
    "refused_capability",
    "refused_state",
    "refused_precondition",
    "already_dropped",
    "in_progress",
    "ambiguous",
    "refused",
    "cancelled",
    "success",
    "unknown",
]
ExactNormalDropReason = (
    Literal[
        "manual_mock_normal_drop_required",
        "exact_baseline_unavailable",
        "human_first_crack_unavailable",
        "exact_live_owner_unavailable",
        "human_first_crack_missing",
        "endpoint_origin_not_established",
        "dispatch_reservation_unavailable",
        "normal_drop_in_progress",
        "normal_drop_unresolved",
        "store_refusal_unavailable",
        "invalid_identity",
        "unknown_session",
        "not_latest_session",
        "clock_epoch_mismatch",
        "ownership_sequence_mismatch",
        "session_purpose_not_eligible",
        "session_faulted",
        "session_not_active",
        "timing_evidence_missing",
        "timing_evidence_superseded",
        "finalisation_in_progress",
        "command_in_progress",
        "phase_invalid",
        "arbitration_unavailable",
        "emergency_since_session_baseline",
        "prior_drop_transition_unaccounted",
        "driver_result_unavailable",
        "driver_result_invalid",
        "observation_boundary_unavailable",
        "settlement_unavailable",
        "retain_unavailable",
        "settlement_stale",
        "observation_freeze_failed",
        "observation_resolution_failed",
        "post_drop_cleanup_failed",
        "endpoint_history_unresolved",
    ]
    | None
)


@dataclass(frozen=True)
class _ExactNormalDropSessionResult:
    """Private result for the unregistered exact mock-drop producer."""

    session_id: str
    clock_epoch_id: str
    ownership_sequence: int
    timing_evidence_sequence: int
    outcome: ExactNormalDropOutcome
    reason: ExactNormalDropReason
    request_dispatch: Literal[
        "not_invoked", "invoked_no_state_transition", "transitioned", "unknown"
    ]
    this_request: Literal["no_reservation", "reservation_owned"]
    history: Literal["not_applicable", "saved", "unknown"]
    fence: Literal["not_applicable", "released", "retained", "not_established"]
    prior_transition: Literal["not_applicable", "not_established", "unaccounted", "unknown"]
    retry: Literal["forbidden"] = "forbidden"
    physical_effect: Literal["not_established"] = "not_established"
    observer_failure: bool = False
    cleanup_failure: bool = False


class ExactNormalDropResult(BaseModel):
    """Closed public result for one exact mock-session Drop request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    session_id: FixedZeroSessionId
    clock_epoch_id: TimingEpochId
    ownership_sequence: TimingPositiveInt
    timing_evidence_sequence: TimingPositiveInt
    outcome: ExactNormalDropOutcome
    reason: ExactNormalDropReason
    request_dispatch: Literal[
        "not_invoked", "invoked_no_state_transition", "transitioned", "unknown"
    ]
    this_request: Literal["no_reservation", "reservation_owned"]
    history: Literal["not_applicable", "saved", "unknown"]
    fence: Literal["not_applicable", "released", "retained", "not_established"]
    prior_transition: Literal["not_applicable", "not_established", "unaccounted", "unknown"]
    retry: Literal["forbidden"]
    physical_effect: Literal["not_established"]
    observer_failure: StrictBool
    cleanup_failure: StrictBool


def _exact_normal_drop_public_result(
    result: _ExactNormalDropSessionResult,
) -> ExactNormalDropResult:
    """Project the private producer reply onto the closed public wire result."""
    return ExactNormalDropResult.model_validate(dataclasses.asdict(result))


@dataclass(frozen=True)
class _TrustedHottopSimulationBinding:
    """Private factory binding for one trusted in-process simulation."""

    composition: HottopSimulationComposition


@dataclass(frozen=True)
class ServerContext:
    """Server bootstrap context.

    Attributes:
        config: Loaded RoastPilot configuration.
        transport: Actual MCP transport used by this server process.
        session_store: Authoritative in-process roast session owner.
        roaster_driver: Configured driver boundary.
        first_crack_runtime: Session-owned first-crack detector runtime.
        ambient_runtime: Session-owned ambient sensor runtime (#185).
        telemetry_sampler: Session-owned autonomous telemetry sampler.
        started_at_utc: UTC time when the MCP process initialized.
    """

    config: AppConfig
    transport: str
    session_store: RoastSessionStore
    roaster_driver: RoasterDriver
    first_crack_runtime: FirstCrackSessionRuntime
    ambient_runtime: AmbientSessionRuntime
    telemetry_sampler: _TelemetrySampler
    started_at_utc: datetime
    capture_evidence_publisher: CaptureEvidencePublisher
    capture_evidence_configured: bool
    unavailable_export_marker: Path | None
    mock_replay_publisher: _MockReplayPublisher
    simulation_composition: HottopSimulationComposition | None = None
    simulation_composition_id: str | None = None
    _trusted_hottop_simulation_binding: _TrustedHottopSimulationBinding | None = None
    _trusted_hottop_simulation_driver: HottopRoasterDriver | None = None
    hottop_reception_registry: _HottopReceptionEvidenceRegistry = field(
        default_factory=_HottopReceptionEvidenceRegistry
    )
    hottop_reception_lock: Lock = field(default_factory=Lock)
    lifecycle_barrier: Lock = field(default_factory=Lock)
    exact_normal_drop_baseline: _ExactNormalDropBaseline | None = None


class _MockReplayPublisher:
    """One bounded off-loop publisher for eligible manual/mock replay bundles."""

    def __init__(self, *, budget_seconds: float = 1.0) -> None:
        self._budget_seconds = budget_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mock-replay")
        self._lock = Lock()
        self._outcomes: dict[tuple[str, int], Future[SealedMockReplayExport | None]] = {}
        self._sealed: dict[tuple[str, int], SealedMockReplayExport] = {}
        self._in_flight: Future[SealedMockReplayExport | None] | None = None
        self._retention_floor = 0

    def request(
        self,
        *,
        store: RoastSessionStore,
        session_id: str,
        observation: Callable[[str], ObservationSnapshot | None],
        config: AppConfig,
        deadline_monotonic_seconds: float,
    ) -> Future[SealedMockReplayExport | None]:
        """Return the one owned publication outcome for an exact generation."""
        try:
            snapshot, _incomplete = store.observe_export_readiness(session_id=session_id)
            retained_keys = store.get_retained_capture_ownership_keys()
        except SessionLifecycleError:
            completed: Future[SealedMockReplayExport | None] = Future()
            completed.set_result(None)
            return completed
        key = (snapshot.id, snapshot.capture_ownership_sequence)
        retained_key_set = frozenset(retained_keys)
        minimum_retained_sequence = min((item[1] for item in retained_keys), default=0)
        future: Future[SealedMockReplayExport | None] | None = None
        reused = False
        with self._lock:
            self._retention_floor = max(self._retention_floor, minimum_retained_sequence)
            for cached_key in tuple(self._sealed):
                if cached_key[1] < self._retention_floor:
                    del self._sealed[cached_key]
            for cached_key, cached_outcome in tuple(self._outcomes.items()):
                if cached_key[1] < self._retention_floor and cached_outcome.done():
                    del self._outcomes[cached_key]
            if key not in retained_key_set or key[1] < self._retention_floor:
                completed = Future[SealedMockReplayExport | None]()
                completed.set_result(None)
                return completed
            existing = self._outcomes.get(key)
            if existing is not None:
                if not existing.done():
                    return existing
                try:
                    prior = existing.result()
                except Exception:
                    prior = None
                if prior is not None:
                    # Reuse remains filesystem verification, so it retains the
                    # same sole bounded owner as first publication.
                    future = self._executor.submit(
                        self._verify_reuse, prior, deadline_monotonic_seconds
                    )
                    self._outcomes[key] = future
                    self._in_flight = future
                    reused = True
                else:
                    del self._outcomes[key]
            elif (prior := self._sealed.get(key)) is not None:
                future = self._executor.submit(
                    self._verify_reuse, prior, deadline_monotonic_seconds
                )
                self._outcomes[key] = future
                self._in_flight = future
                reused = True
            if reused:
                pass
            elif self._in_flight is not None and not self._in_flight.done():
                completed = Future[SealedMockReplayExport | None]()
                completed.set_result(None)
                return completed
            elif not reused:
                future = self._executor.submit(
                    self._publish,
                    store,
                    session_id,
                    observation,
                    config,
                    deadline_monotonic_seconds,
                )
                self._outcomes[key] = future
                self._in_flight = future

        if future is None:
            raise RuntimeError("Mock replay publication ownership was not established.")

        def retire_temporary_refusal(completion: Future[SealedMockReplayExport | None]) -> None:
            """Permit a later eligible request after a bounded refusal."""
            try:
                retryable = completion.result() is None
            except Exception:
                retryable = True
            with self._lock:
                if retryable:
                    if self._outcomes.get(key) is completion:
                        del self._outcomes[key]
                else:
                    sealed = completion.result()
                    if sealed is not None and key[1] >= self._retention_floor:
                        self._sealed[key] = sealed

        future.add_done_callback(retire_temporary_refusal)
        return future

    def _publish(
        self,
        store: RoastSessionStore,
        session_id: str,
        observation: Callable[[str], ObservationSnapshot | None],
        config: AppConfig,
        deadline_monotonic_seconds: float,
    ) -> SealedMockReplayExport | None:
        """Drain, stage, and seal one generation without SDK or driver ownership."""
        prepared = store.prepare_manual_mock_replay_export(
            session_id=session_id,
            timeout_seconds=max(0.0, deadline_monotonic_seconds - time.monotonic()),
        )
        if prepared is None or time.monotonic() >= deadline_monotonic_seconds:
            return None
        session, writer = prepared
        try:
            observation_ready, first_crack_observation = self._terminal_observation(
                observation,
                session.id,
                deadline_monotonic_seconds=deadline_monotonic_seconds,
            )
            if not observation_ready or time.monotonic() >= deadline_monotonic_seconds:
                return None
            raw_jsonl = writer.read_sealed_bytes()
            sealed = seal_mock_replay_bundle(
                session,
                raw_jsonl=raw_jsonl,
                ror_window_seconds=config.session.ror_window_seconds,
                ror_min_sample_seconds=config.session.ror_min_sample_seconds,
                first_crack_observation=first_crack_observation,
                deadline_monotonic_seconds=deadline_monotonic_seconds,
            )
            # Final-byte verification, including a later shared-result reuse,
            # belongs to this one bounded filesystem owner.  Callers only join
            # its outcome and never create competing verification workers.
            # A rename admitted before its strict cutoff may settle after it.
            # Retain that exact verified publication for a later caller while
            # the caller whose budget expired still receives no ready reply.
            return sealed if verify_sealed_mock_replay_bundle(sealed) else None
        except Exception:
            return None

    @staticmethod
    def _terminal_observation(
        observation: Callable[[str], ObservationSnapshot | None],
        session_id: str,
        *,
        deadline_monotonic_seconds: float,
    ) -> tuple[bool, ObservationSnapshot | None]:
        """Read terminal observer evidence within the owned publication budget.

        An absent observer and explicit non-admission are legitimate neutral
        evidence. A present observer remains provisional until its own worker
        reports reaped, so its final confirmation cannot be replaced by an
        early replay summary.
        """
        while time.monotonic() < deadline_monotonic_seconds:
            snapshot = observation(session_id)
            if (
                snapshot is None
                or snapshot.worker_reaped
                or snapshot.observer_state == "not_admitted"
            ):
                return True, snapshot
            remaining = deadline_monotonic_seconds - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
        return False, None

    @staticmethod
    def _verify_reuse(
        sealed: SealedMockReplayExport,
        deadline_monotonic_seconds: float,
    ) -> SealedMockReplayExport | None:
        """Verify a prior exact bundle under the one publication owner."""
        if time.monotonic() >= deadline_monotonic_seconds:
            return None
        if not verify_sealed_mock_replay_bundle(sealed):
            return None
        return sealed if time.monotonic() < deadline_monotonic_seconds else None

    def shutdown(self) -> None:
        """Stop accepting new publication work without cancelling owned filesystem work."""
        self._executor.shutdown(wait=False, cancel_futures=False)

    @property
    def budget_seconds(self) -> float:
        """Return the injected mock-only producer budget."""
        return self._budget_seconds


TelemetrySampleCallback = Callable[[str], bool]


@dataclass
class _TelemetrySamplerStartReceipt:
    """Identify one sampler generation started by a caller."""

    stop_event: Event | None = None
    thread: Thread | None = None


class _TelemetrySampler:
    """Poll driver telemetry for the currently owning roast session."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        sample_callback: TelemetrySampleCallback,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._sample_callback = sample_callback
        self._lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._thread_session_id: str | None = None
        self._active_session_id: str | None = None
        self._last_error: str | None = None

    def start_for_session(self, session_id: str) -> None:
        """Start autonomous sampling for one session id."""
        self._start_for_session(session_id, receipt=None)

    def _start_for_session(
        self, session_id: str, *, receipt: _TelemetrySamplerStartReceipt | None
    ) -> None:
        """Start sampling and bind an optional caller-local generation receipt."""
        generation = receipt if receipt is not None else _TelemetrySamplerStartReceipt()
        thread: Thread | None
        with self._lock:
            thread = self._stop_locked()
        self._join_thread(thread)

        with self._lock:
            stop_event = Event()
            self._active_session_id = session_id
            self._thread_session_id = session_id
            self._last_error = None
            worker = Thread(
                target=self._run,
                args=(session_id, stop_event, generation),
                name=f"roast-telemetry-sampler-{session_id}",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = worker
            generation.stop_event = stop_event
            generation.thread = worker
            try:
                worker.start()
            except BaseException:
                if self._owns_active_generation(session_id, stop_event, generation):
                    self._active_session_id = None
                    if not worker.is_alive():
                        self._thread = None
                        self._thread_session_id = None
                stop_event.set()
                raise

    def stop_started_generation(
        self, receipt: _TelemetrySamplerStartReceipt, *, reason: str
    ) -> None:
        """Stop only the sampler generation captured by one Start attempt."""
        _ = reason
        stop_event = receipt.stop_event
        thread = receipt.thread
        if stop_event is None or thread is None:
            return
        with self._lock:
            if self._thread is thread and self._stop_event is stop_event:
                self._active_session_id = None
        stop_event.set()
        self._join_thread(thread)
        if thread.is_alive():
            return
        with self._lock:
            if self._thread is thread and self._stop_event is stop_event:
                self._active_session_id = None
                self._thread = None
                self._thread_session_id = None

    def stop_for_session(self, session_id: str, *, reason: str) -> None:
        """Stop sampling if the supplied session currently owns the sampler."""
        _ = reason
        thread: Thread | None
        with self._lock:
            if self._active_session_id != session_id:
                return
            thread = self._stop_locked()
        self._join_thread(thread)

    def stop_and_join_for_finalisation(self, session_id: str) -> SamplerFinalisationEvidence:
        """Stop an owning sampler and retain its handle if bounded join times out."""
        with self._lock:
            owned = self._thread_session_id == session_id
            thread = self._thread if owned else None
            if owned:
                self._active_session_id = None
                self._stop_event.set()
        self._join_thread(thread)
        alive = thread is not None and thread.is_alive()
        with self._lock:
            if owned and not alive:
                self._thread = None
                self._thread_session_id = None
            return SamplerFinalisationEvidence(
                owned_by_session_before_stop=owned,
                thread_alive_after_join=alive,
                last_error=self._last_error,
            )

    def shutdown(self) -> None:
        """Stop any active sampler worker."""
        thread: Thread | None
        with self._lock:
            thread = self._stop_locked()
        self._join_thread(thread)

    @property
    def active_session_id(self) -> str | None:
        """Return the session id currently owned by the sampler."""
        with self._lock:
            return self._active_session_id

    @property
    def last_error(self) -> str | None:
        """Return the latest sampler error, if any."""
        with self._lock:
            return self._last_error

    def _run(
        self,
        session_id: str,
        stop_event: Event,
        receipt: _TelemetrySamplerStartReceipt,
    ) -> None:
        while not stop_event.wait(self._interval_seconds):
            try:
                keep_sampling = self._sample_callback(session_id)
            except Exception as exc:  # noqa: BLE001 - background worker must not escape.
                with self._lock:
                    if self._owns_active_generation(session_id, stop_event, receipt):
                        self._last_error = f"{type(exc).__name__}: {exc}"
                keep_sampling = False
            if not keep_sampling:
                with self._lock:
                    if self._owns_generation(session_id, stop_event, receipt):
                        self._active_session_id = None
                        self._thread = None
                        self._thread_session_id = None
                return

    def _owns_generation(
        self,
        session_id: str | None,
        stop_event: Event,
        receipt: _TelemetrySamplerStartReceipt,
    ) -> bool:
        """Return whether the calling worker still owns its exact handle."""
        return (
            session_id is not None
            and self._thread_session_id == session_id
            and self._stop_event is stop_event
            and self._thread is receipt.thread
        )

    def _owns_active_generation(
        self,
        session_id: str | None,
        stop_event: Event,
        receipt: _TelemetrySamplerStartReceipt,
    ) -> bool:
        """Return whether the calling worker still owns active supervision."""
        return self._active_session_id == session_id and self._owns_generation(
            session_id, stop_event, receipt
        )

    def _stop_locked(self) -> Thread | None:
        thread = self._thread
        self._active_session_id = None
        self._thread = None
        self._thread_session_id = None
        self._stop_event.set()
        return thread

    def _join_thread(self, thread: Thread | None) -> None:
        """Wait briefly for a sampler worker after releasing the sampler lock."""
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self._interval_seconds * 2))


@dataclass(frozen=True)
class ServerInfo:
    """Structured metadata about the running RoastPilot server.

    Attributes:
        product_name: Human-facing product name.
        package_name: Python package and PyPI package name.
        version: Installed package version.
        transport: Active MCP transport.
        current_phase: Current project/runtime phase label.
        roaster_driver: Configured roaster driver.
        first_crack_mode: Configured first-crack mode.
        bootstrap_safe: Whether the current defaults stay hardware-free.
        session_presence: Closed, detail-free summary of in-process session state.
        available_bootstrap_tools: Tools available while RoastPilot stays on the
            bootstrap-safe mock path.
        started_at_utc: UTC timestamp when this server process started.
    """

    product_name: str
    package_name: str
    version: str
    transport: str
    current_phase: str
    roaster_driver: str
    first_crack_mode: str
    bootstrap_safe: bool
    session_presence: SessionPresence
    available_bootstrap_tools: tuple[str, ...]
    started_at_utc: str


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    """Minimal runtime configuration snapshot for bootstrap validation.

    Attributes:
        config_source: Config file path when loaded from disk, else `None`.
        roaster_driver: Configured roaster driver.
        roaster_port: Optional roaster serial port.
        roaster_baudrate: Configured roaster baudrate.
        temperature_unit: Configured roaster temperature unit.
        command_interval_seconds: Driver command cadence in seconds.
        first_crack_mode: First-crack mode.
        model_repo_id: Hugging Face repo used for first-crack artifacts.
        model_precision: ONNX precision selection.
        allow_manual_override: Whether manual first-crack override is allowed.
        log_dir: Configured log directory.
        sample_interval_seconds: Telemetry sample interval in seconds.
        auto_t0_detection_enabled: Whether automatic T0 detection is enabled.
        auto_t0_drop_threshold_c: Bean-temperature drop threshold for automatic T0.
    """

    config_source: str | None
    roaster_driver: str
    roaster_port: str | None
    roaster_baudrate: int
    temperature_unit: str
    command_interval_seconds: float
    first_crack_mode: str
    model_repo_id: str
    model_precision: str
    allow_manual_override: bool
    log_dir: str
    sample_interval_seconds: float
    auto_t0_detection_enabled: bool
    auto_t0_drop_threshold_c: float
    first_crack_event_authority: str = "automatic"
    first_crack_observer_protocol: str | None = None


@dataclass(frozen=True)
class EventSnapshot:
    """Serializable roast event snapshot for MCP tool responses."""

    kind: str
    recorded_at_utc: str
    monotonic_seconds: float
    payload: dict[str, EventPayloadValue]


FirstCrackRuntimeStatus = Literal[
    "disabled",
    "manual",
    "pending",
    "detected",
    "faulted",
    "unavailable",
]

AmbientRuntimeStatus = Literal["disabled", "unavailable", "ok"]


@dataclass(frozen=True)
class RoasterDeviceState:
    """Serializable configured-device state returned by MCP tools.

    Attributes:
        driver: Stable driver identifier returned by the configured driver.
        connected: Whether the driver reports an open roaster connection.
        bean_temp_c: Current bean temperature in Celsius when available.
        env_temp_c: Current environment temperature in Celsius when available.
        heat_level_percent: Current heat control level.
        fan_level_percent: Current fan control level.
        cooling_on: Whether cooling is currently active.
        raw_vendor_data: Flat safe diagnostic fields from the driver boundary.
    """

    driver: str
    connected: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    raw_vendor_data: dict[str, EventPayloadValue]


@dataclass(frozen=True)
class FirstCrackStatus:
    """Serializable first-crack status for operator decisions.

    Attributes:
        mode: Configured first-crack mode.
        status: Current first-crack runtime status.
        detected_at_utc: Authoritative detection timestamp when first crack exists.
        detected_monotonic_seconds: Authoritative monotonic detection timestamp.
        allow_manual_override: Whether the explicit override tool is enabled.
        reason: Human-readable reason when status needs extra context.
        audio_running: Whether the session-owned audio runtime is running.
        queued_window_count: Detector windows waiting to be processed.
        emitted_window_count: Windows emitted by the audio runtime.
        dropped_window_count: Windows dropped by the audio runtime.
        processed_window_count: Windows processed by the detector runtime.
        mic_peak_dbfs: Live rolling peak mic level in dBFS while audio capture
            runs (#178), or ``None`` when no capture is running. ``-inf`` for
            silence — surfaces a mis-gained / dead mic under real conditions.
        mic_rms_dbfs: Live rolling RMS mic level in dBFS while audio capture runs
            (#178), or ``None`` when no capture is running.
        overflow_count_last_minute: Microphone input-overflow events (#190) in
            the trailing 60 seconds, so sustained capture degradation is
            operator-visible here instead of stderr-only warning logs.
        estimated_lost_audio_ms_last_minute: Estimated milliseconds of audio at
            risk from overflow events in the trailing 60 seconds, derived
            from the actual inter-read gap — see
            :class:`~coffee_roaster_mcp.audio.OverflowSnapshot`.
        total_overflow_count: Lifetime overflow event count for the current
            capture run.
        max_consecutive_overflow_count: Largest consecutive-overflow streak
            observed by any active input during the current capture run.
        last_inference_duration_ms: Elapsed duration of the latest inference
            attempt in milliseconds.
        max_inference_duration_ms: Largest inference-attempt duration during
            the current session in milliseconds.
        inference_overrun_count: Inference attempts at or beyond the effective
            configured audio hop during the current session.
    """

    mode: FirstCrackMode
    status: FirstCrackRuntimeStatus
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    allow_manual_override: bool
    reason: str | None = None
    audio_running: bool = False
    queued_window_count: int = 0
    emitted_window_count: int = 0
    dropped_window_count: int = 0
    processed_window_count: int = 0
    mic_peak_dbfs: float | None = None
    mic_rms_dbfs: float | None = None
    overflow_count_last_minute: int = 0
    estimated_lost_audio_ms_last_minute: float = 0.0
    total_overflow_count: int = 0
    max_consecutive_overflow_count: int = 0
    last_inference_duration_ms: float = 0.0
    max_inference_duration_ms: float = 0.0
    inference_overrun_count: int = 0


@dataclass(frozen=True)
class AmbientStatus:
    """Serializable ambient sensor status for the roast record (#185).

    Read-only corpus metadata: no roaster-write or control-loop involvement.
    Fail-soft by design — an absent, unplugged, or erroring probe always
    reports `unavailable` with null readings rather than raising or blocking
    the roast.

    Attributes:
        mode: Configured ambient sensor mode.
        status: Current ambient runtime status.
        reason: Human-readable reason when status needs extra context.
        ambient_running: Whether the session-owned ambient runtime is prepared
            to poll readings.
        temperature_c: Last-known ambient temperature in Celsius, or `None`.
        humidity_percent: Last-known relative humidity percentage, or `None`.
        pressure_hpa: Last-known barometric pressure in hectopascals, or `None`.
        last_reading_monotonic_seconds: Monotonic timestamp of the last
            successful reading, or `None` if none has ever succeeded.
    """

    mode: AmbientMode
    status: AmbientRuntimeStatus
    reason: str | None = None
    ambient_running: bool = False
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    last_reading_monotonic_seconds: float | None = None


T0RuntimeStatus = Literal["disabled", "pending", "detected", "unavailable"]
ColdCharacterisationObservationOutcome = Literal[
    "observed", "not_eligible", "unsupported", "unreadable", "malformed"
]


@dataclass(frozen=True)
class ColdCharacterisationObservation:
    """Read-only commanded roast-fan observation for a cold-characterisation session.

    Values are the driver's commanded state, not physical sensing.
    """

    outcome: ColdCharacterisationObservationOutcome
    roast_fan_level_percent: int | None


@dataclass(frozen=True)
class T0Status:
    """Serializable automatic T0 status for operator decisions.

    Attributes:
        auto_detection_enabled: Whether automatic T0 detection is enabled.
        status: Current automatic T0 detection status.
        charge_temperature_c: Max preheat/charge bean temperature seen before T0.
        current_drop_c: Current drop from charge temperature when available.
        drop_threshold_c: Configured bean-temperature drop threshold.
        detected_bean_temperature_c: Bean temperature at the detected T0 reading.
        reason: Human-readable reason when status needs extra context.
    """

    auto_detection_enabled: bool
    status: T0RuntimeStatus
    charge_temperature_c: float | None
    current_drop_c: float | None
    drop_threshold_c: float
    detected_bean_temperature_c: float | None
    reason: str | None = None


@dataclass(frozen=True)
class RoastSessionState:
    """Serializable roast session state returned by MCP tools."""

    session_id: str
    active: bool
    phase: RoastPhase
    created_at_utc: str
    stopped_at_utc: str | None
    elapsed_monotonic_seconds: float
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    beans_added_at_utc: str | None
    first_crack_at_utc: str | None
    beans_dropped_at_utc: str | None
    cooling_started_at_utc: str | None
    cooling_stopped_at_utc: str | None
    faulted_at_utc: str | None
    beans_added_monotonic_seconds: float | None
    first_crack_monotonic_seconds: float | None
    beans_dropped_monotonic_seconds: float | None
    cooling_started_monotonic_seconds: float | None
    cooling_stopped_monotonic_seconds: float | None
    faulted_monotonic_seconds: float | None
    roast_elapsed_seconds: float | None
    development_time_seconds: float | None
    development_percent: float | None
    bean_temp_delta_60s_c: float | None
    env_temp_delta_60s_c: float | None
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None
    device_state: RoasterDeviceState | None
    t0_status: T0Status
    first_crack_status: FirstCrackStatus
    ambient_status: AmbientStatus
    events: tuple[EventSnapshot, ...]
    log_dir: str | None
    session_purpose: SessionPurpose = "roast"
    cold_characterisation_observation: ColdCharacterisationObservation | None = None
    cold_temperature_projection: ColdTemperatureProjection | None = None
    first_crack_observation: ObservationSnapshot | None = None


@dataclass(frozen=True)
class StartRoastSessionResult:
    """Result for starting one new roast session."""

    session: RoastSessionState


@dataclass(frozen=True)
class ControlCommandResult:
    """Result for one in-memory control update."""

    session_id: str
    phase: RoastPhase
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool


@dataclass(frozen=True)
class EventCommandResult:
    """Result for one event-recording command."""

    session_id: str
    phase: RoastPhase
    event: EventSnapshot
    event_count: int


@dataclass(frozen=True)
class SetRecordingMetadataResult:
    """Result for storing annotation-pipeline recording metadata (#176).

    Attributes:
        origin: Stored bean origin slug used to name the captured WAVs.
        roast_num: Stored 1-based roast number used to name the captured WAVs.
    """

    origin: str
    roast_num: int


@dataclass(frozen=True)
class ExportRoastLogResult:
    """Result for one snapshot roast-log export."""

    session_id: str
    log_dir: str
    jsonl_path: str
    csv_path: str
    summary_path: str
    ready: bool
    note: str


SessionFinalisationStatus = Literal[
    "rejected", "clean", "completed_not_clean", "partial", "disconnect_indeterminate", "aborted"
]
FinalisationRejectionReason = Literal[
    "unknown_session",
    "not_latest_session",
    "session_not_active",
    "session_purpose_not_eligible",
    "session_faulted",
    "command_in_progress",
    "finalisation_in_progress",
    "driver_lifecycle_evidence_unsupported",
    "driver_state_unreadable",
    "driver_state_malformed",
    "driver_not_connected",
    "driver_state_not_safe_zero",
]
FinalisationStageName = Literal[
    "telemetry_sampler", "first_crack_runtime", "recording", "driver_disconnect"
]
FinalisationStageStatus = Literal[
    "pending", "completed", "incomplete", "failed", "not_applicable", "skipped"
]
EmergencyStopOrdering = Literal[
    "not_reached",
    "finalisation_committed_first",
    "emergency_stop_before_disconnect_commit",
    "emergency_stop_after_disconnect_attempt",
]
ConfirmationState = Literal["confirmed", "not_applicable", "not_confirmed"]
RecordingArtifactRole = Literal[
    "primary_wav", "recording_sidecar", "annotation_session_sidecar", "additional_wav"
]


@dataclass(frozen=True)
class FinalisationFailure:
    """One append-only failure observed while finalising a session."""

    stage: FinalisationStageName | Literal["revalidation"]
    code: str
    message: str
    attempt_number: int
    recorded_at_utc: str


@dataclass(frozen=True)
class FinalisationStageResult:
    """Outcome of one ordered finalisation stage."""

    stage: FinalisationStageName
    status: FinalisationStageStatus = "pending"
    completed_at_utc: str | None = None
    completed_in_attempt: int | None = None
    detail: str | None = None


FinalisationStages = tuple[
    FinalisationStageResult,
    FinalisationStageResult,
    FinalisationStageResult,
    FinalisationStageResult,
]


@dataclass(frozen=True)
class DriverCommandStateEvidence:
    """Validated, read-only driver lifecycle evidence with safe-zero summary."""

    driver: str
    connected: bool
    command_streaming_required: bool
    command_loop_running: bool | None
    serial_open: bool | None
    heat_level_percent: int
    roast_fan_level_percent: int
    main_fan_level_percent: int
    drum_motor_on: bool
    cooling_motor_on: bool
    solenoid_open: bool
    safe_zero: bool
    non_zero_dimensions: tuple[str, ...]
    command_send_attempts: int | None
    command_write_count: int | None
    last_command_write_size: int | None
    command_loop_error_count: int | None
    status_packet_count: int | None
    status_read_error_count: int | None


@dataclass(frozen=True)
class DriverEvidenceRead:
    """One non-actuating lifecycle evidence read outcome."""

    captured_at_utc: str
    outcome: Literal["read", "unsupported", "unreadable", "malformed"]
    error: str | None
    evidence: DriverCommandStateEvidence | None


@dataclass(frozen=True)
class SamplerFinalisationEvidence:
    """Bounded telemetry sampler stop evidence."""

    owned_by_session_before_stop: bool
    thread_alive_after_join: bool
    last_error: str | None


@dataclass(frozen=True)
class FirstCrackRuntimeFinalisationEvidence:
    """First-crack capture stop evidence."""

    outcome: Literal["not_active", "stopped", "capture_still_running", "stop_failed"]
    stop_error: str | None
    capture_running_after_stop: bool
    final_status: FirstCrackStatus


@dataclass(frozen=True)
class RecordingArtifact:
    """Identity and stat-only observation of one recording artifact."""

    role: RecordingArtifactRole
    filename: str
    path: str
    exists: bool
    size_bytes: int | None


@dataclass(frozen=True)
class RecordingFinalisationEvidence:
    """Recording teardown and artifact evidence."""

    expected: bool
    outcome: Literal["not_configured", "finalised", "not_started", "failed"]
    reason: str | None
    artifacts: tuple[RecordingArtifact, ...]


@dataclass(frozen=True)
class DisconnectEvidence:
    """Append-only disconnect-attempt and confirmation evidence."""

    attempt_count: int = 0
    first_attempted_at_utc: str | None = None
    last_attempted_at_utc: str | None = None
    last_returned_without_error: bool | None = None
    last_error: str | None = None
    connected_false_confirmed: bool = False
    command_loop_stopped: ConfirmationState = "not_confirmed"
    serial_closed: ConfirmationState = "not_confirmed"


@dataclass(frozen=True)
class SessionFinalisationResult:
    """Retained non-actuating cold-characterisation teardown result."""

    session_id: str
    session_purpose: SessionPurpose | None
    status: SessionFinalisationStatus
    clean: bool
    rejection_reason: FinalisationRejectionReason | None
    abort_reason: Literal["emergency_stop", "session_or_reservation_changed"] | None
    retained: bool
    reservation_generation: int | None
    attempt_number: int
    recovered_after_failure: bool
    first_started_at_utc: str | None
    first_started_session_elapsed_seconds: float | None
    last_ended_at_utc: str | None
    last_ended_session_elapsed_seconds: float | None
    emergency_stop_ordering: EmergencyStopOrdering
    stages: FinalisationStages
    failures: tuple[FinalisationFailure, ...]
    admission_driver_evidence: DriverEvidenceRead | None
    pre_disconnect_driver_evidence: DriverEvidenceRead | None
    final_driver_evidence: DriverEvidenceRead | None
    sampler: SamplerFinalisationEvidence | None
    pre_finalisation_first_crack_status: FirstCrackStatus | None
    first_crack_runtime: FirstCrackRuntimeFinalisationEvidence | None
    recording: RecordingFinalisationEvidence | None
    disconnect: DisconnectEvidence
    session_active_after: bool
    session_phase_after: RoastPhase | None


def _validate_composed_manual_first_crack_authority(
    config: AppConfig,
    driver: RoasterDriver,
    simulation_composition: HottopSimulationComposition | None = None,
) -> None:
    """Require the closed mock declaration for manual first-crack authority.

    Automatic authority has no adapter declaration dependency. Manual authority
    remains limited to the built-in mock family and its exact versioned
    declaration; this check deliberately performs no driver lifecycle or
    transport operation.

    Args:
        config: Effective immutable server configuration.
        driver: Driver instance returned by the configured adapter factory.

    Raises:
        ConfigError: If the composed adapter cannot prove the supported manual
            first-crack declaration.
    """
    if config.first_crack.event_authority == "automatic":
        return

    if simulation_composition is not None:
        if (
            isinstance(driver, HottopRoasterDriver)
            and config.roaster.driver == HOTTOP_DRIVER_NAME
            and config.roaster.port == simulation_composition.synthetic_port
            and config.roaster.hottop_reception == "candidate/v1"
        ):
            return
        raise ConfigError(
            "Composed driver does not support configured manual first-crack authority."
        )

    try:
        driver_name = driver.name
        capabilities = cast(object, driver.capabilities)
    except Exception:
        raise ConfigError(
            "Composed driver does not support configured manual first-crack authority."
        ) from None

    if not isinstance(capabilities, RoasterCapabilities):
        raise ConfigError(
            "Composed driver does not support configured manual first-crack authority."
        )
    try:
        capability_driver = capabilities.driver
        manual_first_crack_version = capabilities.manual_first_crack_version
    except Exception:
        raise ConfigError(
            "Composed driver does not support configured manual first-crack authority."
        ) from None

    if (
        not isinstance(driver, MockRoasterDriver)
        or type(config.roaster.driver) is not str
        or config.roaster.driver != "mock"
        or type(driver_name) is not str
        or driver_name != "mock"
        or type(capability_driver) is not str
        or capability_driver != "mock"
        or type(manual_first_crack_version) is not int
        or manual_first_crack_version != 1
    ):
        raise ConfigError(
            "Composed driver does not support configured manual first-crack authority."
        )


def _manual_capture_configured(
    config: AppConfig,
    driver: RoasterDriver,
    simulation_composition: HottopSimulationComposition | None = None,
) -> bool:
    """Return the closed composition predicate for capture-evidence capability."""
    return bool(
        config.first_crack.event_authority == "manual"
        and (
            (isinstance(driver, MockRoasterDriver) and config.roaster.driver == "mock")
            or (
                simulation_composition is not None
                and isinstance(driver, HottopRoasterDriver)
                and config.roaster.driver == HOTTOP_DRIVER_NAME
                and config.roaster.port == simulation_composition.synthetic_port
                and config.roaster.hottop_reception == "candidate/v1"
            )
        )
        and config.recording.enabled
        and config.recording.autocapture
    )


def _manual_mock_capture_configured(config: AppConfig, driver: RoasterDriver) -> bool:
    """Return the closed composition predicate for capture-evidence capability."""
    return bool(
        config.first_crack.event_authority == "manual"
        and isinstance(driver, MockRoasterDriver)
        and config.roaster.driver == "mock"
        and config.recording.enabled
        and config.recording.autocapture
    )


def _trusted_hottop_simulation(server_context: ServerContext) -> bool:
    """Return whether this context retains the factory-bound synthetic authority."""
    binding = server_context._trusted_hottop_simulation_binding  # pyright: ignore[reportPrivateUsage]
    driver = server_context.roaster_driver
    config = server_context.config
    return bool(
        type(binding) is _TrustedHottopSimulationBinding
        and type(binding.composition) is HottopSimulationComposition
        and server_context.simulation_composition is binding.composition
        and type(driver) is HottopRoasterDriver
        and driver is server_context._trusted_hottop_simulation_driver  # pyright: ignore[reportPrivateUsage]
        and driver._port == SYNTHETIC_HOTTOP_PORT  # pyright: ignore[reportPrivateUsage]
        and config.roaster.port == SYNTHETIC_HOTTOP_PORT
        and config.first_crack.event_authority == "manual"
        and config.recording.enabled
        and config.recording.autocapture
    )


def _trusted_hottop_simulation_components(
    *,
    config: AppConfig,
    driver: RoasterDriver,
    simulation_composition: HottopSimulationComposition | None,
    binding: _TrustedHottopSimulationBinding | None,
) -> bool:
    """Return whether one factory-built composition may own the synthetic writer."""
    return bool(
        type(binding) is _TrustedHottopSimulationBinding
        and type(binding.composition) is HottopSimulationComposition
        and simulation_composition is binding.composition
        and type(driver) is HottopRoasterDriver
        and driver._port == SYNTHETIC_HOTTOP_PORT  # pyright: ignore[reportPrivateUsage]
        and config.roaster.port == SYNTHETIC_HOTTOP_PORT
        and config.first_crack.event_authority == "manual"
        and config.recording.enabled
        and config.recording.autocapture
    )


def _validate_hottop_simulation_composition(
    config: AppConfig, composition: HottopSimulationComposition
) -> None:
    """Validate the closed software-only composition before adapter construction."""
    roaster = config.roaster
    if (
        roaster.driver != HOTTOP_DRIVER_NAME
        or roaster.hottop_reception != "candidate/v1"
        or roaster.port != SYNTHETIC_HOTTOP_PORT
        or not roaster.temperature_unit_explicit
        or roaster.temperature_unit != "celsius"
    ):
        raise ConfigError("Trusted Hottop simulation requires its exact candidate configuration.")
    if type(composition) is not HottopSimulationComposition:
        raise ConfigError("Trusted Hottop simulation requires its repository-owned composition.")
    if (
        config.first_crack.event_authority != "manual"
        or config.first_crack.mode != "audio"
        or config.audio.source != "wav"
        or config.audio.wav_path is None
        or config.audio.input_device is not None
    ):
        raise ConfigError("Trusted Hottop simulation requires manual synthetic WAV capture.")
    if config.first_crack.local_model_dir is None:
        raise ConfigError(
            "Trusted Hottop simulation requires an explicit local first-crack model directory."
        )
    if (
        not config.recording.enabled
        or not config.recording.autocapture
        or config.recording.devices
        or config.recording.device is not None
        or config.recording.channels is not None
    ):
        raise ConfigError("Trusted Hottop simulation requires one synthetic recording stream.")
    if config.ambient.mode != "disabled" and config.ambient.device is not None:
        raise ConfigError("Trusted Hottop simulation does not permit ambient device selection.")


_UNAVAILABLE_EXPORT_NAMESPACE = ".roastpilot-export-unavailable"


def _establish_unavailable_export_marker(config: AppConfig) -> Path | None:
    """Return the fixed absent export-refusal namespace, or ``None`` if unsafe.

    This is deliberately a composition-time check.  The returned path is only
    a non-usable response marker: no export, writer, recorder, or hot path may
    create or probe it.  The marker is kept outside the raw session and default
    recorder roots, and an explicitly configured recorder root may not contain
    it or be contained by it.
    """
    try:
        logging_root = config.logging.log_dir.resolve(strict=False)
        marker_entry = logging_root / _UNAVAILABLE_EXPORT_NAMESPACE
        raw_root = (logging_root / "roasts").resolve(strict=False)
        recorder_root = (
            config.recording.export_location
            if config.recording.export_location is not None
            else logging_root / "captures"
        ).resolve(strict=False)
        # ``lstat`` is intentional: a broken symlink is occupied too.  We do
        # not create the logging root merely to test the marker.
        try:
            marker_entry.lstat()
        except FileNotFoundError:
            pass
        else:
            return None
        marker = marker_entry.resolve(strict=False)
        for owned_root in (raw_root, recorder_root):
            overlaps = (
                marker == owned_root
                or marker.is_relative_to(owned_root)
                or owned_root.is_relative_to(marker)
            )
            if overlaps:
                return None
    except (OSError, RuntimeError, ValueError):
        return None
    return marker


def build_server_context(
    *,
    config_path: str | Path | None = None,
    transport: str = "stdio",
    simulation_composition: HottopSimulationComposition | None = None,
    environ: Mapping[str, str] | None = None,
    _trusted_hottop_simulation_binding: _TrustedHottopSimulationBinding | None = None,
) -> ServerContext:
    """Build the MCP server lifecycle context from config and runtime transport.

    Args:
        config_path: Optional explicit config path.
        transport: Actual MCP transport used by this server instance.

    Returns:
        The initialized server context used by the MCP lifespan.
    """
    # Defer only the driver-family portion of manual admission until this
    # composition can reject an untrusted candidate before adapter creation.
    simulation_admission = (
        trusted_manual_driver_admission()
        if type(simulation_composition) is HottopSimulationComposition
        else None
    )
    config = load_config(
        path=config_path,
        environ=environ,
        # Defer only the driver-family portion of manual admission until this
        # composition can reject an untrusted candidate before adapter creation.
        manual_driver_admission=trusted_manual_driver_admission(),
    )
    if simulation_composition is not None:
        _validate_hottop_simulation_composition(config, simulation_composition)
    elif config.roaster.hottop_reception == "candidate/v1":
        raise ConfigError("Hottop candidate configuration requires trusted simulation composition.")
    if simulation_admission is None:
        validate_first_crack_authority(config)
    else:
        validate_first_crack_authority(config, manual_driver_admission=simulation_admission)
    try:
        if simulation_composition is None:
            roaster_driver = create_roaster_driver(
                config.roaster.driver,
                port=config.roaster.port,
                baudrate=config.roaster.baudrate,
                temperature_unit=config.roaster.temperature_unit,
                command_interval_seconds=config.roaster.command_interval_seconds,
                hottop_reception=config.roaster.hottop_reception,
            )
        else:
            roaster_driver = create_roaster_driver(
                config.roaster.driver,
                port=config.roaster.port,
                baudrate=config.roaster.baudrate,
                temperature_unit=config.roaster.temperature_unit,
                command_interval_seconds=config.roaster.command_interval_seconds,
                hottop_reception=config.roaster.hottop_reception,
                serial_factory=build_trusted_simulation_serial_factory(simulation_composition),
                reception_clock=simulation_composition.clock,
                simulation_receipt_age_bound_seconds=(
                    simulation_composition.receipt_age_bound_seconds
                ),
            )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    _validate_composed_manual_first_crack_authority(config, roaster_driver, simulation_composition)
    capture_evidence_configured = _manual_capture_configured(
        config, roaster_driver, simulation_composition
    )
    unavailable_export_marker = (
        _establish_unavailable_export_marker(config) if simulation_composition is None else None
    )
    capture_evidence_publisher = CaptureEvidencePublisher()
    server_context: ServerContext | None = None

    def sample_active_session(session_id: str) -> bool:
        if server_context is None:
            return False
        return _sample_active_session_telemetry(server_context, session_id=session_id)

    telemetry_sampler = _TelemetrySampler(
        interval_seconds=config.logging.sample_interval_seconds,
        sample_callback=sample_active_session,
    )
    if simulation_composition is None:
        session_store = RoastSessionStore(
            default_log_dir=config.logging.log_dir / "roasts",
            telemetry_log_interval_seconds=config.logging.sample_interval_seconds,
            manual_first_crack_writer_enabled=_manual_mock_capture_configured(
                config, roaster_driver
            ),
        )
    else:
        session_store = RoastSessionStore(
            default_log_dir=config.logging.log_dir / "roasts",
            telemetry_log_interval_seconds=config.logging.sample_interval_seconds,
            monotonic_now=simulation_composition.clock,
            manual_first_crack_writer_enabled=_trusted_hottop_simulation_components(
                config=config,
                driver=roaster_driver,
                simulation_composition=simulation_composition,
                binding=_trusted_hottop_simulation_binding,
            ),
            manual_first_crack_budget_seconds=(
                None
                if _trusted_hottop_simulation_components(
                    config=config,
                    driver=roaster_driver,
                    simulation_composition=simulation_composition,
                    binding=_trusted_hottop_simulation_binding,
                )
                else 1.0
            ),
        )
    mock_replay_publisher = _MockReplayPublisher()

    def session_is_retained(session_id: str) -> bool:
        try:
            session_store.get_session_snapshot(session_id=session_id)
        except SessionLifecycleError:
            return False
        return True

    first_crack_runtime = build_first_crack_session_runtime(
        config,
        session_is_retained=session_is_retained,
    )
    if simulation_composition is not None and simulation_composition.clock is not time.monotonic:
        first_crack_runtime.set_event_domain_mapped(event_domain_mapped=False)
    if simulation_composition is not None and config.first_crack.event_authority == "manual":
        first_crack_runtime.admit_trusted_manual_driver(admission=simulation_admission)
    if capture_evidence_configured:
        first_crack_runtime.set_capture_evidence_publisher(capture_evidence_publisher)
    server_context = ServerContext(
        config=config,
        transport=transport,
        session_store=session_store,
        roaster_driver=roaster_driver,
        first_crack_runtime=first_crack_runtime,
        ambient_runtime=build_ambient_session_runtime(
            config,
            reader_factory=(
                build_trusted_simulation_ambient_reader_factory(simulation_composition)
                if simulation_composition is not None
                else None
            ),
        ),
        telemetry_sampler=telemetry_sampler,
        started_at_utc=datetime.now(UTC),
        capture_evidence_publisher=capture_evidence_publisher,
        capture_evidence_configured=capture_evidence_configured,
        simulation_composition=simulation_composition,
        simulation_composition_id=(uuid4().hex if simulation_composition is not None else None),
        _trusted_hottop_simulation_binding=_trusted_hottop_simulation_binding,
        _trusted_hottop_simulation_driver=(
            cast(HottopRoasterDriver, roaster_driver)
            if _trusted_hottop_simulation_components(
                config=config,
                driver=roaster_driver,
                simulation_composition=simulation_composition,
                binding=_trusted_hottop_simulation_binding,
            )
            else None
        ),
        unavailable_export_marker=unavailable_export_marker,
        mock_replay_publisher=mock_replay_publisher,
    )
    return server_context


def create_mcp_server(
    config_path: str | Path | None = None,
    transport: str = "stdio",
) -> FastMCP:
    """Create the RoastPilot FastMCP server.

    Args:
        config_path: Optional explicit config path. When omitted, the standard
            config loading rules apply.
        transport: Actual MCP transport used by this server instance.

    Returns:
        A configured FastMCP server instance.
    """

    return _create_mcp_server(
        config_path=config_path,
        transport=transport,
        simulation_composition=None,
        environ=None,
    )


def create_hottop_simulation_mcp_server(
    *,
    config_path: str | Path,
    composition: HottopSimulationComposition,
    environ: Mapping[str, str] | None = None,
) -> FastMCP:
    """Create the trusted in-process Hottop software simulation server.

    This composition is intentionally unavailable to the CLI and deployed
    server factory. It exists only for owner-approved in-process simulation.
    """
    return _create_mcp_server(
        config_path=config_path,
        transport="stdio",
        simulation_composition=composition,
        environ={} if environ is None else environ,
    )


def _create_mcp_server(
    *,
    config_path: str | Path | None,
    transport: str,
    simulation_composition: HottopSimulationComposition | None,
    environ: Mapping[str, str] | None,
) -> FastMCP:
    """Create a FastMCP server from one already-authorized composition."""

    trusted_hottop_simulation_binding = (
        _TrustedHottopSimulationBinding(simulation_composition)
        if type(simulation_composition) is HottopSimulationComposition
        else None
    )

    @asynccontextmanager
    async def server_lifespan(_: FastMCP) -> AsyncGenerator[ServerContext, None]:
        """Load typed config once for the MCP process lifetime."""
        server_context = build_server_context(
            config_path=config_path,
            transport=transport,
            simulation_composition=simulation_composition,
            environ=environ,
            _trusted_hottop_simulation_binding=trusted_hottop_simulation_binding,
        )
        try:
            yield server_context
        finally:
            server_context.telemetry_sampler.shutdown()
            server_context.first_crack_runtime.shutdown()
            server_context.ambient_runtime.shutdown()
            server_context.mock_replay_publisher.shutdown()
            server_context.session_store.shutdown_manual_first_crack_writer()

    mcp = FastMCP(
        name="RoastPilot",
        instructions=(
            "RoastPilot MCP server with a mock-safe session runtime. "
            "This tool surface supports one in-process roast session and "
            "driver-backed roast controls before log writers land."
        ),
        lifespan=server_lifespan,
    )

    @mcp.tool()
    def get_server_info(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> ServerInfo:
        """Return structured metadata about the running MCP server."""
        server_context = ctx.request_context.lifespan_context
        config = server_context.config
        return ServerInfo(
            product_name="RoastPilot",
            package_name="coffee-roaster-mcp",
            version=__version__,
            transport=server_context.transport,
            current_phase="bootstrap",
            roaster_driver=config.roaster.driver,
            first_crack_mode=config.first_crack.mode,
            bootstrap_safe=_is_bootstrap_safe(config),
            session_presence=server_context.session_store.get_session_presence(),
            available_bootstrap_tools=(
                "get_server_info",
                "get_runtime_config",
                "start_roast_session",
                "get_roast_state",
                "set_heat",
                "set_fan",
                "mark_beans_added",
                "mark_first_crack",
                "drop_beans",
                "drop_beans_exact_session",
                "start_cooling",
                "stop_cooling",
                "export_roast_log",
                "emergency_stop",
                "set_recording_metadata",
                "finalise_cold_characterisation_session",
            ),
            started_at_utc=server_context.started_at_utc.isoformat(),
        )

    @mcp.tool()
    def get_capture_evidence(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        capture_protocol: Literal["capture_evidence/v1"],
        intent: Literal["bootstrap", "current_session"],
        expected_session_id: CaptureEvidenceSessionId | None = None,
    ) -> CaptureEvidenceBootstrapResult | CaptureEvidenceResult:
        """Read one bounded, non-actuating manual-capture evidence projection."""
        server_context = ctx.request_context.lifespan_context
        if intent == "bootstrap":
            return CaptureEvidenceBootstrapResult(
                capture_protocol=capture_protocol,
                intent="bootstrap",
                capability=(
                    "manual_hottop_simulation_capture"
                    if server_context.simulation_composition is not None
                    and server_context.capture_evidence_configured
                    else "manual_mock_capture"
                    if server_context.capture_evidence_configured
                    else "not_configured"
                ),
            )
        if expected_session_id is None:
            raise ValueError("expected_session_id is required for current_session.")
        return _read_capture_evidence(server_context, expected_session_id)

    @mcp.tool()
    def get_runtime_config(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> RuntimeConfigSnapshot:
        """Return a bootstrap-safe summary of the active RoastPilot config."""
        server_context = ctx.request_context.lifespan_context
        config = server_context.config
        return RuntimeConfigSnapshot(
            config_source=str(config.source_path) if config.source_path is not None else None,
            roaster_driver=config.roaster.driver,
            roaster_port=config.roaster.port,
            roaster_baudrate=config.roaster.baudrate,
            temperature_unit=config.roaster.temperature_unit,
            command_interval_seconds=config.roaster.command_interval_seconds,
            first_crack_mode=config.first_crack.mode,
            first_crack_event_authority=config.first_crack.event_authority,
            first_crack_observer_protocol="manual/v1"
            if config.first_crack.event_authority == "manual"
            else None,
            model_repo_id=config.first_crack.repo_id,
            model_precision=config.first_crack.precision,
            allow_manual_override=config.first_crack.allow_manual_override,
            log_dir=str(config.logging.log_dir),
            sample_interval_seconds=config.logging.sample_interval_seconds,
            auto_t0_detection_enabled=config.session.auto_t0_detection_enabled,
            auto_t0_drop_threshold_c=config.session.auto_t0_drop_threshold_c,
        )

    @mcp.tool()
    def start_roast_session(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        purpose: SessionPurpose = "roast",
        first_crack_observer_ack: str | None = None,
    ) -> StartRoastSessionResult:
        """Start one new authoritative roast session and prepare the driver."""
        server_context = ctx.request_context.lifespan_context
        if type(server_context.simulation_composition) is HottopSimulationComposition:
            validate_first_crack_authority(
                server_context.config,
                manual_driver_admission=trusted_manual_driver_admission(),
            )
        else:
            validate_first_crack_authority(server_context.config)
        manual = server_context.config.first_crack.event_authority == "manual"
        simulated_hottop = server_context.simulation_composition is not None
        if simulated_hottop:
            if purpose != "roast" or first_crack_observer_ack != "manual/v1":
                raise ValueError(
                    "Trusted Hottop simulation requires explicit roast purpose and "
                    "manual/v1 acknowledgement."
                )
        elif (manual and first_crack_observer_ack != "manual/v1") or (
            not manual and first_crack_observer_ack is not None
        ):
            raise ValueError(
                "Requested first-crack observer protocol does not match configured authority."
            )
        _validate_composed_manual_first_crack_authority(
            server_context.config,
            server_context.roaster_driver,
            server_context.simulation_composition,
        )
        with server_context.lifecycle_barrier:
            reservation = server_context.session_store.reserve_session_start()
        driver = server_context.roaster_driver
        if manual and isinstance(driver, HottopRoasterDriver):
            try:
                owner = driver.reserve_inhibited_start(reservation.token, purpose=purpose)
            except BaseException:
                with server_context.lifecycle_barrier:
                    server_context.session_store.clear_session_start_reservation(reservation)
                raise
            publication = StagedSessionStartPublication(session=None)
            try:
                driver.open_inhibited_start(owner)
                with server_context.lifecycle_barrier:
                    driver.begin_inhibited_start_promotion(owner)
                    try:
                        runtime_session = (
                            server_context.session_store.complete_staged_session_start_snapshot(
                                reservation, publication=publication, purpose=purpose
                            )
                        )
                    finally:
                        if publication.session is not None:
                            driver.bind_promoted_inhibited_start(owner, publication.session)
                published_session = publication.session
                if published_session is None:
                    raise SessionLifecycleError(
                        "Staged Hottop session publication did not complete."
                    )
            except BaseException:
                if publication.session is None:
                    driver.restore_inhibited_start_after_unpublished_promotion(owner)
                    settlement = driver.abort_inhibited_start(owner)
                    if settlement in {
                        HottopStartSettlement.NOT_ENTERED,
                        HottopStartSettlement.CLEAN_CLOSED,
                    }:
                        # Revocation clears pending admission only.  Dispose the
                        # in-flight token only after exact native non-entry or a
                        # clean close, and only while this exact owner is current.
                        with server_context.lifecycle_barrier:
                            server_context.session_store.clear_session_start_reservation(
                                reservation
                            )
                raise
            try:
                prepared = driver.prepare_promoted_first_write(owner, published_session)
                with server_context.lifecycle_barrier:
                    server_context.session_store.assert_staged_start_claimable(published_session)
                    claim = driver.claim_prepared_first_write(prepared)
                disposition = driver.dispatch_claimed_first_write(claim)
                if disposition is not HottopFirstWriteDisposition.WRITTEN:
                    raise SessionLifecycleError("Staged Hottop first write has an unknown receipt.")
                with server_context.lifecycle_barrier:
                    server_context.session_store.assert_staged_start_claimable(published_session)
                    if not driver.asserted_written_start(owner, published_session):
                        raise SessionLifecycleError("Staged Hottop first-write ownership changed.")
                sampler_receipt = _TelemetrySamplerStartReceipt()
                started: tuple[Callable[[], object], ...] = (
                    lambda: server_context.telemetry_sampler.stop_started_generation(
                        sampler_receipt, reason="staged start failed"
                    ),
                    lambda: server_context.ambient_runtime.stop_for_session(
                        published_session.id, reason="staged start failed"
                    ),
                    lambda: server_context.first_crack_runtime.stop_after_failed_start(
                        published_session.id, reason="staged start failed"
                    ),
                )
                try:
                    _start_first_crack_runtime(server_context, session=runtime_session)
                    server_context.ambient_runtime.start_for_session(runtime_session)
                    server_context.telemetry_sampler._start_for_session(  # pyright: ignore[reportPrivateUsage]
                        published_session.id, receipt=sampler_receipt
                    )
                    with server_context.lifecycle_barrier:
                        session = server_context.session_store.snapshot_staged_start_claimable(
                            published_session
                        )
                        if not driver.asserted_written_start(owner, published_session):
                            raise SessionLifecycleError("Staged Hottop start changed after setup.")
                        if driver.capabilities.hottop_reception_candidate_version == 1:
                            result = StartRoastSessionResult(
                                session=_serialize_session_state(
                                    session,
                                    config=server_context.config,
                                    monotonic_now=server_context.session_store.operational_monotonic_now,
                                    first_crack_runtime=server_context.first_crack_runtime.snapshot(),
                                    first_crack_observation=(
                                        server_context.first_crack_runtime.observation_for_session(
                                            session.id
                                        )
                                    ),
                                    ambient_runtime=server_context.ambient_runtime.snapshot(),
                                )
                            )
                            driver.hand_off_written_start_to_stream(owner, published_session)
                            return result
                except BaseException:
                    for stop_runtime in started:
                        with suppress(Exception):
                            stop_runtime()
                    raise
            except BaseException:
                # A published session and its claimed/written owner are intentionally
                # retained: neither a missing receipt nor a setup failure proves
                # non-entry or authorises a substitute transport action.
                raise
            return StartRoastSessionResult(
                session=_serialize_session_state(
                    session,
                    config=server_context.config,
                    monotonic_now=server_context.session_store.operational_monotonic_now,
                    first_crack_runtime=server_context.first_crack_runtime.snapshot(),
                    first_crack_observation=server_context.first_crack_runtime.observation_for_session(
                        session.id
                    ),
                    ambient_runtime=server_context.ambient_runtime.snapshot(),
                )
            )
        try:
            driver.connect()
            with server_context.lifecycle_barrier:
                exact_baseline: tuple[MockRoasterDriver, int, int] | None = None
                if _exact_normal_drop_capable(server_context):
                    driver = server_context.roaster_driver
                    assert type(driver) is MockRoasterDriver
                    exact_baseline = (
                        driver,
                        driver.emergency_count,
                        driver.drop_transition_count,
                    )
                session = server_context.session_store.complete_session_start_snapshot(
                    reservation, purpose=purpose
                )
                object.__setattr__(server_context, "exact_normal_drop_baseline", None)
                if exact_baseline is not None:
                    driver, emergency_baseline, drop_baseline = exact_baseline
                    try:
                        live_session = server_context.session_store.get_active_session()
                        if live_session is None or live_session.id != session.id:
                            raise RuntimeError(
                                "Started session was not published as the active owner."
                            )
                        identity = server_context.session_store.normal_drop_identity(live_session)
                    except Exception:
                        # Exact Drop is optional to ordinary Start. A post-publication
                        # lookup failure or owner mismatch leaves its holder unavailable
                        # without rolling back the already published ordinary Start.
                        pass
                    else:
                        object.__setattr__(
                            server_context,
                            "exact_normal_drop_baseline",
                            _ExactNormalDropBaseline(
                                session_id=session.id,
                                driver_ref=driver,
                                clock_epoch_id=identity.clock_epoch_id,
                                emergency_baseline=emergency_baseline,
                                drop_baseline=drop_baseline,
                            ),
                        )
        except Exception:
            server_context.session_store.clear_session_start_reservation(reservation)
            raise
        _start_first_crack_runtime(server_context, session=session)
        server_context.ambient_runtime.start_for_session(session)
        server_context.telemetry_sampler.start_for_session(session.id)
        return StartRoastSessionResult(
            session=_serialize_session_state(
                session,
                config=server_context.config,
                monotonic_now=server_context.session_store.operational_monotonic_now,
                first_crack_runtime=server_context.first_crack_runtime.snapshot(),
                first_crack_observation=server_context.first_crack_runtime.observation_for_session(
                    session.id
                ),
                ambient_runtime=server_context.ambient_runtime.snapshot(),
            )
        )

    @mcp.tool()
    async def finalise_cold_characterisation_session(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext], session_id: str
    ) -> SessionFinalisationResult:
        """Finalise one eligible cold-characterisation session without actuation."""
        return await asyncio.to_thread(
            _finalise_cold_characterisation_session,
            ctx.request_context.lifespan_context,
            session_id,
        )

    @mcp.tool()
    async def get_roast_state(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> RoastSessionState:
        """Return the current authoritative roast session state."""
        server_context = ctx.request_context.lifespan_context
        session = _resolve_session(server_context, session_id=session_id)
        observation_source_session_id = session.id
        driver_state = _read_current_driver_state(server_context)
        device_state = _serialize_device_state(driver_state)
        cold_temperature_projection = _project_cold_temperature_for_session(
            server_context,
            session=session,
            driver_state=driver_state,
        )
        cold_characterisation_observation = _observe_cold_characterisation_roast_fan(
            server_context,
            session=session,
            driver_state=driver_state,
        )
        session = _record_polling_telemetry_for_active_session(
            server_context,
            session=session,
            requested_session_id=session_id,
            driver_state=driver_state,
        )
        try:
            if _manual_first_crack_writer_composition(server_context):
                # An automatic charge may own an off-loop canonical writer turn.
                # Keep the registered SDK loop free for an independently eligible
                # emergency while that exact historical row settles.
                auto_t0_recorded = await asyncio.to_thread(
                    _process_auto_t0_for_active_session,
                    server_context,
                    session_id=observation_source_session_id,
                    device_state=device_state,
                )
            else:
                auto_t0_recorded = _process_auto_t0_for_active_session(
                    server_context,
                    session_id=observation_source_session_id,
                    device_state=device_state,
                )
        except EventLogRecordingError:
            # A validated state read remains useful when only its automatic
            # charge row could not be persisted. Do not recast persistence as
            # a driver read failure or grant recovery authority.
            auto_t0_recorded = False
        if auto_t0_recorded:
            server_context.first_crack_runtime.discard_queued_windows_for_session(
                session.id,
                reason="Dropped queued pre-T0 detector windows after automatic T0.",
            )
        _process_first_crack_runtime_for_active_session(server_context, session_id=session_id)
        _process_ambient_runtime_for_active_session(server_context, session_id=session_id)
        session = _resolve_session(server_context, session_id=session_id)
        cold_characterisation_observation = _reconcile_cold_characterisation_observation(
            server_context,
            session=session,
            observation_source_session_id=observation_source_session_id,
            observation=cold_characterisation_observation,
        )
        cold_temperature_projection = _reconcile_cold_temperature_projection(
            server_context,
            session=session,
            observation_source_session_id=observation_source_session_id,
            projection=cold_temperature_projection,
        )
        return _serialize_session_state(
            session,
            config=server_context.config,
            monotonic_now=server_context.session_store.operational_monotonic_now,
            device_state=device_state,
            first_crack_runtime=server_context.first_crack_runtime.snapshot(),
            first_crack_observation=server_context.first_crack_runtime.observation_for_session(
                session.id
            ),
            ambient_runtime=server_context.ambient_runtime.snapshot(),
            cold_characterisation_observation=cold_characterisation_observation,
            cold_temperature_projection=cold_temperature_projection,
        )

    @mcp.tool()
    def set_heat(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        heat_level_percent: int,
    ) -> ControlCommandResult:
        """Set heat through the configured driver boundary."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        snapshot = _run_reserved_driver_control(
            server_context,
            session,
            driver_command=lambda: server_context.roaster_driver.set_heat(
                heat_level_percent=heat_level_percent,
            ),
        )
        return _serialize_control_result(snapshot)

    @mcp.tool()
    def contain_heat_fixed_zero(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        expected_session_id: str,
        containment_protocol: Literal["fixed_zero/v1"],
    ) -> FixedZeroContainmentResult:
        """Contain commanded heat for one exact session with bounded command evidence.

        The returned state is not evidence of physical heat removal, transport
        settlement, or durable audit. Unknown outcomes block ordinary commands
        and never permit automatic retry. Existing stale-completion handling may
        apply the broader emergency profile after the returned evidence was read.
        """
        _ = containment_protocol
        return _run_fixed_zero_heat_containment(
            ctx.request_context.lifespan_context,
            expected_session_id=expected_session_id,
        )

    @mcp.tool()
    def get_timing_evidence(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        expected_session_id: str,
        evidence_protocol: Literal["timing_evidence/v1"],
    ) -> TimingEvidenceResult:
        """Return one mock-only, read-only timing-evidence record.

        The response is synthetic software evidence and does not qualify live
        hardware, physical timing, recipe decisions, or roast readiness.
        """
        _ = evidence_protocol
        return _run_timing_evidence(
            ctx.request_context.lifespan_context,
            expected_session_id=expected_session_id,
        )

    if simulation_composition is not None:

        @mcp.tool()
        def get_hottop_reception_evidence(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
            ctx: Context[ServerSession, ServerContext],
            expected_session_id: str,
            evidence_protocol: Literal["hottop_reception_evidence/v1"],
        ) -> HottopReceptionEvidenceResult:
            """Read one immutable trusted-simulation Hottop reception snapshot.

            This read performs no serial I/O and does not establish physical
            sensor age, transport health, or a physical effect.
            """
            _ = evidence_protocol
            return _run_hottop_reception_evidence(
                ctx.request_context.lifespan_context,
                expected_session_id=expected_session_id,
                trusted_composition=simulation_composition,
            )

    @mcp.tool()
    async def drop_beans_exact_session(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str,
        clock_epoch_id: str,
        ownership_sequence: int,
        timing_evidence_sequence: int,
    ) -> ExactNormalDropResult:
        """Request one exact mock-only Drop for the supplied session binding."""
        result = await asyncio.to_thread(
            _run_normal_drop_exact_session,
            ctx.request_context.lifespan_context,
            session_id=session_id,
            clock_epoch_id=clock_epoch_id,
            ownership_sequence=ownership_sequence,
            timing_evidence_sequence=timing_evidence_sequence,
        )
        return _exact_normal_drop_public_result(result)

    @mcp.tool()
    def set_fan(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        fan_level_percent: int,
    ) -> ControlCommandResult:
        """Set fan through the configured driver boundary."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        snapshot = _run_reserved_driver_control(
            server_context,
            session,
            driver_command=lambda: server_context.roaster_driver.set_fan(
                fan_level_percent=fan_level_percent,
            ),
        )
        return _serialize_control_result(snapshot)

    @mcp.tool()
    async def mark_beans_added(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative beans-added event."""
        server_context = ctx.request_context.lifespan_context
        if _manual_first_crack_writer_composition(server_context):
            # The composed mock writer may be waiting for durable storage.  Do
            # not occupy the SDK event loop during that wait: an independent
            # emergency stop must still reach its driver boundary.  The store
            # owns admission and rejects a settlement displaced by that stop.
            result = await asyncio.to_thread(_record_session_event, ctx, "beans_added")
        else:
            result = _record_session_event(ctx, "beans_added")
        _process_first_crack_runtime_for_active_session(
            server_context,
            session_id=result.session_id,
        )
        snapshot = _snapshot_session(
            server_context,
            session_id=result.session_id,
        )
        return EventCommandResult(
            session_id=snapshot.id,
            phase=snapshot.phase,
            event=result.event,
            event_count=len(snapshot.event_timeline),
        )

    @mcp.tool()
    async def mark_first_crack(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative first-crack event."""
        server_context = ctx.request_context.lifespan_context
        if not server_context.config.first_crack.allow_manual_override:
            raise ValueError("Manual first-crack override is disabled by configuration.")
        if _manual_first_crack_writer_composition(server_context):
            return await _record_manual_first_crack_with_budget(server_context)
        result = _record_session_event(ctx, "first_crack_detected")
        if server_context.config.first_crack.event_authority == "manual":
            server_context.first_crack_runtime.notify_human_mark(
                _resolve_session(server_context, session_id=result.session_id)
            )
        else:
            server_context.first_crack_runtime.stop_for_session(
                result.session_id, reason="manual first-crack override"
            )
        return result

    @mcp.tool()
    async def drop_beans(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        expected_session_id: str | None = None,
    ) -> EventCommandResult:
        """Drop beans through the driver and record the cooling transition."""
        server_context = ctx.request_context.lifespan_context
        if expected_session_id is None:
            session = _require_active_session(server_context)
            if _manual_first_crack_writer_composition(server_context):
                event, snapshot = await asyncio.to_thread(
                    _run_reserved_driver_drop, server_context, session
                )
            else:
                event, snapshot = _run_reserved_driver_drop(server_context, session)
        else:
            with server_context.lifecycle_barrier:
                session = _require_guarded_fault_recovery_session(
                    server_context,
                    expected_session_id=expected_session_id,
                )
                reservation = server_context.session_store.reserve_driver_fault_recovery(
                    session,
                    kind="drop",
                )
            if _manual_first_crack_writer_composition(server_context):
                event, snapshot = await asyncio.to_thread(
                    _run_reserved_driver_fault_recovery,
                    server_context,
                    session,
                    reservation=reservation,
                )
            else:
                event, snapshot = _run_reserved_driver_fault_recovery(
                    server_context,
                    session,
                    reservation=reservation,
                )
            return _serialize_event_result(snapshot=snapshot, event=event)
        _stop_runtimes_after_drop(server_context, session=session, snapshot=snapshot)
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    async def start_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        expected_session_id: str | None = None,
    ) -> EventCommandResult:
        """Start cooling through the driver as an explicit recovery command."""
        server_context = ctx.request_context.lifespan_context
        if expected_session_id is None:
            session = _require_active_session(server_context)
            if _manual_first_crack_writer_composition(server_context):
                event, snapshot = await asyncio.to_thread(
                    _run_reserved_driver_start_cooling, server_context, session
                )
            else:
                event, snapshot = _run_reserved_driver_start_cooling(server_context, session)
        else:
            with server_context.lifecycle_barrier:
                session = _require_guarded_fault_recovery_session(
                    server_context,
                    expected_session_id=expected_session_id,
                )
                reservation = server_context.session_store.reserve_driver_fault_recovery(
                    session,
                    kind="start_cooling",
                )
            if _manual_first_crack_writer_composition(server_context):
                event, snapshot = await asyncio.to_thread(
                    _run_reserved_driver_fault_recovery,
                    server_context,
                    session,
                    reservation=reservation,
                )
            else:
                event, snapshot = _run_reserved_driver_fault_recovery(
                    server_context,
                    session,
                    reservation=reservation,
                )
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    async def stop_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        expected_session_id: str | None = None,
    ) -> EventCommandResult:
        """Stop cooling through the configured driver boundary."""
        server_context = ctx.request_context.lifespan_context
        if expected_session_id is not None:
            with server_context.lifecycle_barrier:
                session = _require_guarded_fault_recovery_session(
                    server_context,
                    expected_session_id=expected_session_id,
                )
                reservation = server_context.session_store.reserve_driver_fault_recovery(
                    session,
                    kind="stop_cooling",
                )
            if _manual_first_crack_writer_composition(server_context):
                event, snapshot = await asyncio.to_thread(
                    _run_reserved_driver_fault_recovery,
                    server_context,
                    session,
                    reservation=reservation,
                )
            else:
                event, snapshot = _run_reserved_driver_fault_recovery(
                    server_context,
                    session,
                    reservation=reservation,
                )
        else:
            session = server_context.session_store.get_active_session()
            if session is None:
                with server_context.lifecycle_barrier:
                    session = _require_faulted_cooling_session(server_context)
                    reservation = server_context.session_store.reserve_driver_fault_recovery(
                        session,
                        kind="stop_cooling",
                    )
                if _manual_first_crack_writer_composition(server_context):
                    event, snapshot = await asyncio.to_thread(
                        _run_reserved_driver_fault_recovery,
                        server_context,
                        session,
                        reservation=reservation,
                    )
                else:
                    event, snapshot = _run_reserved_driver_fault_recovery(
                        server_context,
                        session,
                        reservation=reservation,
                    )
            else:
                if _manual_first_crack_writer_composition(server_context):
                    event, snapshot = await asyncio.to_thread(
                        _run_reserved_driver_stop_cooling, server_context, session
                    )
                else:
                    event, snapshot = _run_reserved_driver_stop_cooling(server_context, session)
        server_context.first_crack_runtime.stop_for_session(
            snapshot.id,
            reason="cooling stopped",
        )
        server_context.telemetry_sampler.stop_for_session(
            snapshot.id,
            reason="cooling stopped",
        )
        server_context.ambient_runtime.stop_for_session(
            snapshot.id,
            reason="cooling stopped",
        )
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    async def export_roast_log(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> ExportRoastLogResult:
        """Write a snapshot export for one roast session."""
        server_context = ctx.request_context.lifespan_context
        try:
            session, persistence_incomplete = server_context.session_store.observe_export_readiness(
                session_id=session_id
            )
        except SessionLifecycleError as error:
            raise ValueError(str(error)) from error
        manual_mock_export = _manual_mock_capture_configured(
            server_context.config, server_context.roaster_driver
        )
        manual_mock_replay = manual_mock_export and server_context.capture_evidence_configured
        trusted_hottop_export = (
            _trusted_hottop_simulation(server_context) and session.purpose == "roast"
        )
        if trusted_hottop_export:
            if persistence_incomplete or session.active:
                unavailable = _unavailable_manual_mock_export(
                    server_context,
                    session.id,
                    note=(
                        "Roast log recording is unavailable: JSONL persistence is incomplete."
                        if persistence_incomplete
                        else "Roast log export is unavailable while this roast session is active."
                    ),
                )
                if unavailable is None:
                    raise ValueError("Roast export is unavailable.")
                return unavailable
            export = await _export_trusted_hottop_simulation_snapshot(
                server_context, session.id
            )
            if export is not None:
                return ExportRoastLogResult(
                    session_id=export.session_id,
                    log_dir=str(export.log_dir),
                    jsonl_path=str(export.jsonl_path),
                    csv_path=str(export.csv_path),
                    summary_path=str(export.summary_path),
                    ready=export.ready,
                    note=export.note,
                )
            unavailable = _unavailable_manual_mock_export(
                server_context,
                session.id,
                note="Roast log export is unavailable.",
            )
            if unavailable is None:
                raise ValueError("Roast export is unavailable.")
            return unavailable
        if persistence_incomplete or (
            session.active and session.purpose == "roast" and manual_mock_export
        ):
            if session.log_writer is None:
                raise ValueError("Session log target is unavailable.")
            if _manual_first_crack_writer_composition(server_context):
                unavailable = _unavailable_manual_mock_export(
                    server_context,
                    session.id,
                    note=(
                        "Roast log recording is unavailable: JSONL persistence is incomplete."
                        if persistence_incomplete
                        else "Roast log export is unavailable while this roast session is active."
                    ),
                )
                if unavailable is None:
                    raise ValueError("Roast export is unavailable.")
                return unavailable
            log_dir = session.log_writer.log_dir
            return ExportRoastLogResult(
                session_id=session.id,
                log_dir=str(log_dir),
                jsonl_path=str(log_dir / "roast.jsonl"),
                csv_path=str(log_dir / "roast.csv"),
                summary_path=str(log_dir / "summary.json"),
                ready=False,
                note=(
                    "Roast log recording is unavailable: JSONL persistence is incomplete."
                    if persistence_incomplete
                    else "Roast log export is unavailable while this roast session is active."
                ),
            )
        if manual_mock_replay and session.purpose == "roast":
            if session.log_writer is None:
                raise ValueError("Session log target is unavailable.")
            sealed = await _await_mock_replay_export(server_context, session.id)
            if sealed is not None:
                return ExportRoastLogResult(
                    session_id=sealed.session_id,
                    log_dir=str(sealed.log_dir),
                    jsonl_path=str(sealed.jsonl_path),
                    csv_path=str(sealed.csv_path),
                    summary_path=str(sealed.summary_path),
                    ready=True,
                    note=sealed.note,
                )
            unavailable = _unavailable_manual_mock_export(
                server_context,
                session.id,
                note="Roast log export is unavailable.",
            )
            if unavailable is None:
                raise ValueError("Roast export is unavailable.")
            return unavailable
        observation = server_context.first_crack_runtime.observation_for_session(session.id)
        export = export_roast_snapshot(
            session,
            roaster_driver=server_context.config.roaster.driver,
            ror_window_seconds=server_context.config.session.ror_window_seconds,
            ror_min_sample_seconds=server_context.config.session.ror_min_sample_seconds,
            first_crack_observation=observation,
        )
        return ExportRoastLogResult(
            session_id=export.session_id,
            log_dir=str(export.log_dir),
            jsonl_path=str(export.jsonl_path),
            csv_path=str(export.csv_path),
            summary_path=str(export.summary_path),
            ready=export.ready,
            note=export.note,
        )

    @mcp.tool()
    async def emergency_stop(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        reason: str = "manual emergency stop",
        expected_session_id: str | None = None,
    ) -> EventCommandResult:
        """Call the configured driver safety method and record a fault event."""
        server_context = ctx.request_context.lifespan_context
        session: RoastSession | None = None
        cleanup_owner: RoastSession | None = None
        has_verified_containment = False
        primary_error: BaseException | None = None
        observation_ticket: object | None = None
        deferred_mock_fault_event: RoastEvent | None = None
        deferred_mock_fault_ticket: MockDropPersistenceTicket | None = None
        event: RoastEvent | None = None
        guarded_fault_recording: (
            tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None] | None
        ) = None
        guarded_fault_refusal = False
        mode: Literal["ACTIVE_REASON_ONLY", "ACTIVE_OWNER", "STOPPED_FAULT"]
        snapshot: RoastSession | None = None
        try:
            with server_context.lifecycle_barrier:
                if expected_session_id is None:
                    session = _require_active_session(server_context)
                    mode = "ACTIVE_REASON_ONLY"
                    cleanup_owner = session
                elif _trusted_hottop_simulation(server_context):
                    candidate_session = server_context.session_store.get_latest_session()
                    if candidate_session is None or candidate_session.id != expected_session_id:
                        raise ValueError("Expected session is not the latest roast session.")
                    if candidate_session.active:
                        try:
                            server_context.session_store.assert_exact_active_emergency_owner(
                                candidate_session,
                                expected_session_id,
                            )
                        except SessionLifecycleError as exc:
                            raise ValueError(str(exc)) from exc
                        session = candidate_session
                        mode = "ACTIVE_OWNER"
                    else:
                        session = _require_guarded_fault_recovery_session(
                            server_context,
                            expected_session_id=expected_session_id,
                            allow_in_flight=True,
                        )
                        server_context.session_store.cancel_session_start_for_emergency_stop()
                        mode = "STOPPED_FAULT"
                else:
                    session = _require_guarded_fault_recovery_session(
                        server_context,
                        expected_session_id=expected_session_id,
                        allow_in_flight=True,
                    )
                    cleanup_owner = session
                    server_context.session_store.cancel_session_start_for_emergency_stop()
                    mode = "STOPPED_FAULT"
                try:
                    server_context.session_store.cancel_nonfinalisation_driver_command(session)
                except SessionLifecycleError as exc:
                    if mode == "ACTIVE_OWNER":
                        latest = server_context.session_store.get_latest_session()
                        if latest is None or latest.id != expected_session_id:
                            raise ValueError(
                                "Expected session is not the latest roast session."
                            ) from exc
                    raise
                if _manual_first_crack_writer_composition(server_context):
                    # Endpoint ownership is visible to new mark admission
                    # before the emergency driver call. A captured session
                    # which completed meanwhile is left untouched so this
                    # eligible emergency still reaches its driver boundary.
                    try:
                        server_context.session_store.admit_manual_first_crack_endpoint(session)
                    except SessionLifecycleError as exc:
                        if mode == "ACTIVE_OWNER":
                            latest = server_context.session_store.get_latest_session()
                            if latest is None or latest.id != expected_session_id:
                                raise ValueError(
                                    "Expected session is not the latest roast session."
                                ) from exc
                        raise
                else:
                    server_context.session_store.revoke_unconsumed_manual_first_crack_live_publication(
                        session
                    )
                if server_context.config.first_crack.event_authority == "manual":
                    revoke_publication = getattr(
                        server_context.first_crack_runtime,
                        "revoke_human_mark_publication",
                        None,
                    )
                    if revoke_publication is not None:
                        revoke_publication(session)
                if cleanup_owner is None:
                    cleanup_owner = session
                safety_payload = run_driver_emergency_stop(server_context, reason=reason)
                if server_context.config.first_crack.event_authority == "manual":
                    observation_ticket = (
                        server_context.first_crack_runtime.begin_observation_boundary(
                            session, reason="emergency_stop"
                        )
                    )
                has_verified_containment = has_verified_emergency_containment(
                    safety_payload=safety_payload
                )
                if mode == "STOPPED_FAULT" and not has_verified_containment:
                    guarded_fault_recording = _retain_guarded_emergency_stop_block(
                        server_context,
                        session=session,
                        safety_payload=safety_payload,
                    )
                    guarded_fault_refusal = True
                if not guarded_fault_refusal:
                    try:
                        if (
                            type(server_context.roaster_driver) is MockRoasterDriver
                            and server_context.config.roaster.driver == "mock"
                            and server_context.session_store.has_pending_normal_drop_persistence(
                                session,
                            )
                        ):
                            event, deferred_mock_fault_ticket = (
                                server_context.session_store.prepare_mock_drop_emergency_stop(
                                    session,
                                    reason=reason,
                                    safety_payload=safety_payload,
                                    allow_stopped_latest=True,
                                )
                            )
                            deferred_mock_fault_event = event
                        else:
                            event, snapshot = server_context.session_store.emergency_stop_snapshot(
                                session,
                                reason=reason,
                                safety_payload=safety_payload,
                                allow_stopped_latest=True,
                            )
                    except Exception as exc:
                        if (
                            mode != "STOPPED_FAULT"
                            and has_verified_containment
                            and _manual_first_crack_writer_composition(server_context)
                        ):
                            server_context.session_store.retain_active_verified_emergency_containment(
                                session,
                                safety_payload=safety_payload,
                            )
                            raise SessionLifecycleError(
                                "Emergency stop command was accepted, but fault recording "
                                "is unavailable."
                            ) from exc
                        if mode == "STOPPED_FAULT":
                            with suppress(Exception):  # Preserve the persistence failure.
                                server_context.session_store.retain_fault_block(
                                    session,
                                    safety_payload=safety_payload,
                                )
                            raise
                        server_context.session_store.retain_active_fault_block(
                            session,
                            safety_payload=safety_payload,
                        )
                        raise SessionLifecycleError(
                            "Emergency stop fault recording failed; containment is unverified."
                        ) from exc
                    if (
                        server_context.config.first_crack.event_authority == "manual"
                        and deferred_mock_fault_ticket is None
                    ):
                        try:
                            server_context.first_crack_runtime.resolve_observation_boundary(
                                session,
                                observation_ticket,
                                cutoff_monotonic_seconds=session.monotonic_start
                                + event.monotonic_seconds,
                            )
                        except Exception as exc:
                            if mode == "STOPPED_FAULT":
                                with suppress(Exception):
                                    server_context.session_store.retain_fault_block(
                                        session,
                                        safety_payload=safety_payload,
                                    )
                                raise
                            server_context.session_store.retain_active_fault_block(
                                session,
                                safety_payload=safety_payload,
                            )
                            raise SessionLifecycleError(
                                "Emergency stop fault recording failed; containment is unverified."
                            ) from exc
            if guarded_fault_refusal:
                if guarded_fault_recording is not None:
                    try:
                        await asyncio.to_thread(
                            _commit_prepared_fault_recording,
                            server_context,
                            session=session,
                            prepared_fault=guarded_fault_recording,
                        )
                    except Exception as exc:
                        with suppress(Exception):
                            server_context.session_store.retain_fault_block(
                                session, safety_payload=safety_payload
                            )
                        raise SessionLifecycleError(
                            "Emergency stop fault recording failed; containment is unverified."
                        ) from exc
                raise SessionLifecycleError(_guarded_emergency_stop_failure_message(safety_payload))
            if deferred_mock_fault_event is not None and deferred_mock_fault_ticket is not None:
                try:
                    snapshot = await asyncio.to_thread(
                        server_context.session_store.commit_prepared_mock_drop_emergency_stop,
                        session,
                        event=deferred_mock_fault_event,
                        ticket=deferred_mock_fault_ticket,
                    )
                except Exception as exc:
                    if mode == "STOPPED_FAULT":
                        with suppress(Exception):
                            server_context.session_store.retain_fault_block(
                                session, safety_payload=safety_payload
                            )
                        raise SessionLifecycleError(
                            "Emergency stop fault recording failed; containment is unverified."
                        ) from exc
                    server_context.session_store.retain_active_fault_block(
                        session, safety_payload=safety_payload
                    )
                    raise SessionLifecycleError(
                        "Emergency stop fault recording failed; containment is unverified."
                    ) from exc
                if server_context.config.first_crack.event_authority == "manual":
                    server_context.first_crack_runtime.resolve_observation_boundary(
                        session,
                        observation_ticket,
                        cutoff_monotonic_seconds=session.monotonic_start
                        + deferred_mock_fault_event.monotonic_seconds,
                    )
            if not has_verified_containment:
                raise SessionLifecycleError(
                    "Emergency stop did not report verified emergency containment."
                )
            if event is not None:
                completion = server_context.session_store.emergency_fault_completion(event)
                if completion is not None:
                    try:
                        await asyncio.shield(asyncio.wrap_future(completion))
                    except Exception as exc:
                        raise SessionLifecycleError(
                            "Emergency stop received a confirmed mock driver response, "
                            "but fault save failed or is unknown."
                        ) from exc
                snapshot = server_context.session_store.get_session_snapshot(session_id=session.id)
            if snapshot is None:
                raise RuntimeError("Emergency stop did not produce a session snapshot.")
            if event is None:
                raise RuntimeError("Emergency stop did not produce a fault event.")
            return _serialize_event_result(snapshot=snapshot, event=event)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if cleanup_owner is not None:
                cleanup_error: Exception | None = None
                for stop_runtime in (
                    lambda: (
                        server_context.first_crack_runtime.freeze_observation_boundary(
                            cleanup_owner, observation_ticket, reason="emergency_stop"
                        )
                        if server_context.config.first_crack.event_authority == "manual"
                        and observation_ticket is not None
                        else server_context.first_crack_runtime.stop_for_session(
                            cleanup_owner.id, reason="emergency stop"
                        )
                    ),
                    lambda: server_context.telemetry_sampler.stop_for_session(
                        cleanup_owner.id,
                        reason="emergency stop",
                    ),
                    lambda: server_context.ambient_runtime.stop_for_session(
                        cleanup_owner.id,
                        reason="emergency stop",
                    ),
                ):
                    try:
                        stop_runtime()
                    except Exception as exc:  # noqa: BLE001 - finish every containment cleanup.
                        if cleanup_error is None:
                            cleanup_error = exc
                if cleanup_error is not None and primary_error is None:
                    raise cleanup_error

    @mcp.tool()
    def set_recording_metadata(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        origin: str,
        roast_num: int,
    ) -> SetRecordingMetadataResult:
        """Store bean origin + roast number for the next roast's audio recording.

        Call this BEFORE starting the roast. The recorder reads it when it creates
        the WAVs, naming them ``mic{N}-{origin}-roast{N}.wav`` and writing a
        ``{origin}-roast{N}-session.json`` so the captured audio plugs straight
        into the coffee-first-crack-detection annotation pipeline. This is purely
        additive metadata: it sends no hardware command. If never called,
        recording falls back to the session id as origin and roast number 0.

        Args:
            origin: Bean origin slug (e.g. ``"brazil"``).
            roast_num: 1-based roast number.

        Returns:
            The stored origin and roast number.

        Raises:
            ValueError: If origin is blank or roast_num is negative.
        """
        server_context = ctx.request_context.lifespan_context
        stored = server_context.first_crack_runtime.set_recording_metadata(
            origin=origin,
            roast_num=roast_num,
        )
        return SetRecordingMetadataResult(origin=stored.origin, roast_num=stored.roast_num)

    _install_fixed_zero_argument_adapter(mcp)
    _install_legacy_human_mark_argument_adapter(mcp)
    _install_capture_evidence_argument_adapter(mcp)
    _install_timing_evidence_argument_adapter(mcp)
    _install_exact_normal_drop_argument_adapter(mcp)
    if simulation_composition is not None:
        _install_hottop_simulation_start_argument_adapter(mcp)
        _install_hottop_reception_evidence_argument_adapter(mcp)
    return mcp


# Logger of the MCP SDK's low-level server, which logs one INFO line
# ("Processing request of type ...") for every request it dispatches. During a
# live roast the agent polls ``get_roast_state`` once per controller tick (~1 Hz),
# so at INFO this single logger floods the console for the whole roast and buries
# the operator-relevant output (phase changes, advisor recs, safety verdicts,
# audio mic-overflow recovery). The name is stable across the SDK versions we pin;
# it is asserted in the tests so a rename surfaces loudly rather than silently
# re-flooding.
SDK_REQUEST_LOGGER_NAME = "mcp.server.lowlevel.server"


def quiet_sdk_per_request_log() -> None:
    """Raise the MCP SDK's per-request logger to WARNING if it is more verbose.

    Suppresses the routine per-request ``Processing request of type
    CallToolRequest`` INFO line that the SDK emits once per dispatched request,
    while leaving ``WARNING`` and above visible so faults, mic-overflow recovery,
    and real errors still surface. This is observability-only and touches no other
    logger (the project's own ``coffee_roaster_mcp.*`` INFO lines are unaffected).

    Only raises the level: if a user or config has already set the logger to a
    stricter level (``ERROR``/``CRITICAL``), that setting is preserved rather than
    trampled down to ``WARNING``.
    """
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    # The #162 fix is the original getEffectiveLevel() guard with `<=`, not `<`:
    # this runs at run_stdio_server startup BEFORE the SDK's .run() configures INFO,
    # so the effective level is still the inherited default WARNING — the old `<`
    # skipped, leaving the logger NOTSET, then .run() set INFO and it inherited the
    # flood. `<= WARNING` instead PINS an explicit WARNING in that startup case
    # (which .run()'s later root INFO can no longer override). It still skips a
    # stricter inherited/explicit ERROR/CRITICAL (> WARNING) so we never LOWER the
    # effective threshold — keying off getEffectiveLevel(), not .level, so a NOTSET
    # logger under an ERROR root is left alone (Augment #182).
    if sdk_logger.getEffectiveLevel() <= logging.WARNING:
        sdk_logger.setLevel(logging.WARNING)


def run_stdio_server(config_path: str | Path | None = None) -> None:
    """Run RoastPilot over stdio transport.

    Args:
        config_path: Optional explicit config path.
    """
    quiet_sdk_per_request_log()
    create_mcp_server(config_path=config_path, transport="stdio").run(transport="stdio")


def _is_bootstrap_safe(config: AppConfig) -> bool:
    """Return whether the active config matches bootstrap-safe defaults.

    Args:
        config: Loaded RoastPilot configuration.

    Returns:
        True when the config stays on the mock driver and uses a first-crack mode
        that does not require audio capture or model startup.
    """
    return config.roaster.driver == "mock" and config.first_crack.mode in {
        "disabled",
        "manual",
    }


def _require_active_session(server_context: ServerContext) -> RoastSession:
    """Return the active session or fail clearly when none exists."""
    session = server_context.session_store.get_active_session()
    if session is None:
        raise ValueError("No active roast session exists.")
    return session


def _require_faulted_cooling_session(server_context: ServerContext) -> RoastSession:
    """Return the latest stopped fault session when cooling recovery is possible."""
    session = server_context.session_store.get_latest_session()
    if (
        session is None
        or session.active
        or session.phase != "fault"
        or session.faulted_at_utc is None
        or not session.cooling_on
    ):
        raise ValueError("No active roast session exists.")
    return session


def _require_guarded_fault_recovery_session(
    server_context: ServerContext,
    *,
    expected_session_id: str,
    allow_in_flight: bool = False,
) -> RoastSession:
    """Return the exact latest stopped-fault session selected by an operator.

    The session store repeats the stopped-fault and latest-session checks when it
    reserves the command. This helper keeps the MCP boundary's identity check
    adjacent to the lifecycle barrier so a mismatch cannot reach a driver call.
    """
    session = server_context.session_store.get_latest_session()
    if session is None or session.id != expected_session_id:
        raise ValueError("Expected session is not the latest roast session.")
    if session.active or session.phase != "fault" or session.faulted_at_utc is None:
        raise ValueError("Expected session is not a stopped fault session.")
    if session.heat_level_percent != 0 and not allow_in_flight:
        raise ValueError("Expected fault session does not have zero heat.")
    try:
        if allow_in_flight:
            server_context.session_store.assert_guarded_fault_emergency_stop_admission(session)
        else:
            server_context.session_store.assert_guarded_fault_recovery_admission(session)
    except SessionLifecycleError as exc:
        raise ValueError(str(exc)) from exc
    return session


def _resolve_session(server_context: ServerContext, *, session_id: str | None) -> RoastSession:
    """Resolve one session for read or export operations."""
    return _snapshot_session(server_context, session_id=session_id)


def _record_session_event(
    ctx: Context[ServerSession, ServerContext],
    kind: Literal["beans_added", "first_crack_detected"],
) -> EventCommandResult:
    """Record one core event against the active session."""
    server_context = ctx.request_context.lifespan_context
    session = _require_active_session(server_context)
    event, snapshot = server_context.session_store.record_event_snapshot(
        session,
        kind,
        provenance="manual_mark",
    )
    return _serialize_event_result(snapshot=snapshot, event=event)


def _manual_first_crack_writer_composition(server_context: ServerContext) -> bool:
    """Return whether this context owns the bounded manual first-crack writer."""
    return server_context.capture_evidence_configured and (
        _manual_mock_capture_configured(
            server_context.config,
            server_context.roaster_driver,
        )
        or _trusted_hottop_simulation(server_context)
    )


def _unavailable_manual_mock_export(
    server_context: ServerContext,
    session_id: str,
    *,
    note: str = "Roast log export is unavailable.",
) -> ExportRoastLogResult | None:
    """Build an inert unready export response without touching storage.

    ``None`` represents a composition that could not establish the reserved
    unavailable namespace.  The caller converts that state to the fixed tool
    error, deliberately without exposing an implementation path.
    """
    marker = server_context.unavailable_export_marker
    if marker is None:
        return None
    return ExportRoastLogResult(
        session_id=session_id,
        log_dir=str(marker),
        jsonl_path=str(marker / "roast.jsonl"),
        csv_path=str(marker / "roast.csv"),
        summary_path=str(marker / "summary.json"),
        ready=False,
        note=note,
    )


async def _export_trusted_hottop_simulation_snapshot(
    server_context: ServerContext, session_id: str
) -> RoastLogExport | None:
    """Export one settled trusted-simulation generation without owning JSONL writes."""
    deadline = time.monotonic() + server_context.mock_replay_publisher.budget_seconds

    def remaining_seconds() -> float:
        """Return the remaining bounded export budget."""
        return max(0.0, deadline - time.monotonic())

    try:
        prepared = await asyncio.wait_for(
            asyncio.to_thread(
                server_context.session_store.prepare_manual_mock_replay_export,
                session_id=session_id,
                timeout_seconds=remaining_seconds(),
            ),
            timeout=remaining_seconds(),
        )
    except TimeoutError:
        return None
    if prepared is None or remaining_seconds() <= 0:
        return None
    session, writer = prepared
    try:
        before = await asyncio.wait_for(
            asyncio.to_thread(writer.read_sealed_bytes), timeout=remaining_seconds()
        )
    except (EventLogRecordingError, OSError, TimeoutError):
        return None
    observation = server_context.first_crack_runtime.observation_for_session(session.id)
    try:
        export = export_roast_snapshot(
            session,
            roaster_driver=server_context.config.roaster.driver,
            ror_window_seconds=server_context.config.session.ror_window_seconds,
            ror_min_sample_seconds=server_context.config.session.ror_min_sample_seconds,
            first_crack_observation=observation,
            require_existing_jsonl=True,
        )
        after = await asyncio.wait_for(
            asyncio.to_thread(writer.read_sealed_bytes), timeout=remaining_seconds()
        )
    except (EventLogRecordingError, OSError, TimeoutError, ValueError):
        return None
    return export if before == after else None


async def _await_mock_replay_export(
    server_context: ServerContext, session_id: str
) -> RoastLogExport | None:
    """Await one mock replay publication without blocking the registered SDK loop."""
    deadline = time.monotonic() + server_context.mock_replay_publisher.budget_seconds
    future = server_context.mock_replay_publisher.request(
        store=server_context.session_store,
        session_id=session_id,
        observation=server_context.first_crack_runtime.observation_for_session,
        config=server_context.config,
        deadline_monotonic_seconds=deadline,
    )
    try:
        timeout = max(0.0, deadline - time.monotonic())
        sealed = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)), timeout=timeout
        )
    except TimeoutError:
        return None
    if sealed is None or time.monotonic() >= deadline:
        return None
    return sealed.export


async def _record_manual_first_crack_with_budget(
    server_context: ServerContext,
) -> EventCommandResult:
    """Await one composed manual mark without cancelling its owned writer.

    The store's injected producer deadline, when configured, covers staging,
    append and verification. A local timeout only revokes live publication; it
    never abandons, retries, or infers the writer's eventual filesystem result.
    """
    started_monotonic_seconds = server_context.session_store.manual_first_crack_monotonic_now()
    session = _require_active_session(server_context)
    capture_publication = getattr(
        server_context.first_crack_runtime, "capture_human_mark_publication", None
    )
    publication_token = None if capture_publication is None else capture_publication(session)
    try:
        attempt: ManualFirstCrackAttempt = (
            server_context.session_store.begin_manual_first_crack_write(
                session,
                observer_publication_token=publication_token,
                on_live=(
                    lambda current, token: server_context.first_crack_runtime.notify_human_mark(
                        current, publication_token=token
                    )
                ),
                started_monotonic_seconds=started_monotonic_seconds,
            )
        )
    except BaseException:
        _discard_human_mark_publication(server_context, publication_token)
        raise
    if publication_token is not attempt.observer_publication_token:
        # Observer identity is captured before store admission. A duplicate or
        # retry owns no new attempt, so it must retire only its unused claim.
        _discard_human_mark_publication(server_context, publication_token)
    if attempt.token == "duplicate":
        settlement = await asyncio.wrap_future(attempt.completion)
    else:
        remaining = server_context.session_store.manual_first_crack_remaining_seconds(attempt)
        if remaining is not None and remaining <= 0:
            settlement = (
                server_context.session_store.settle_or_revoke_manual_first_crack_live_publication(
                    attempt
                )
            )
            if settlement is not None:
                if settlement.snapshot is None or settlement.event is None:
                    raise ValueError("Manual first-crack mark remains pending; do not resend it.")
                return _serialize_event_result(snapshot=settlement.snapshot, event=settlement.event)
            _discard_human_mark_publication(server_context, attempt.observer_publication_token)
            raise ValueError("Manual first-crack mark remains pending; do not resend it.")
        try:
            if remaining is None:
                settlement = await asyncio.wrap_future(attempt.completion)
            else:
                settlement = await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(attempt.completion)),
                    timeout=remaining,
                )
        except TimeoutError:
            # Choose live settlement versus revocation under the store lock.
            # A completed future cannot straddle a separate done()/revoke().
            settlement = (
                server_context.session_store.settle_or_revoke_manual_first_crack_live_publication(
                    attempt
                )
            )
            if settlement is None:
                _discard_human_mark_publication(server_context, attempt.observer_publication_token)
                raise ValueError(
                    "Manual first-crack mark remains pending; do not resend it."
                ) from None
    if (
        settlement.outcome not in {"live", "duplicate"}
        or settlement.event is None
        or settlement.snapshot is None
    ):
        _discard_human_mark_publication(server_context, attempt.observer_publication_token)
        raise ValueError("Manual first-crack mark is pending or incomplete; do not resend it.")
    return _serialize_event_result(snapshot=settlement.snapshot, event=settlement.event)


def _discard_human_mark_publication(
    server_context: ServerContext, publication_token: object | None
) -> None:
    """Discard one unused real-runtime publication claim when available."""
    discard_publication = getattr(
        server_context.first_crack_runtime, "discard_human_mark_publication", None
    )
    if discard_publication is not None:
        discard_publication(publication_token)


def _revoke_manual_mark_publication_for_accepted_drop(
    server_context: ServerContext, *, session: RoastSession
) -> None:
    """Revoke an outstanding mock manual-mark claim at accepted Drop admission.

    The store has already accepted the exact Drop reservation before this
    narrow publication-only call.  It deliberately does not acquire the
    runtime lock, observer state, capture, filesystem, or driver boundaries.
    Rejected and stale requests never reach this endpoint.
    """
    if not _manual_first_crack_writer_composition(server_context):
        return
    revoke_publication = getattr(
        server_context.first_crack_runtime,
        "revoke_human_mark_publication",
        None,
    )
    if revoke_publication is not None:
        revoke_publication(session)


def _start_first_crack_runtime(
    server_context: ServerContext,
    *,
    session: RoastSession,
) -> None:
    """Start first-crack runtime preparation for a newly created session."""
    server_context.first_crack_runtime.start_for_session(session)


def _exact_normal_drop_capable(server_context: ServerContext) -> bool:
    """Return whether this context is the closed manual-authority mock composition."""
    return (
        type(server_context.roaster_driver) is MockRoasterDriver
        and server_context.config.roaster.driver == "mock"
        and server_context.config.first_crack.event_authority == "manual"
    )


def _exact_normal_drop_result(
    *,
    identity: NormalDropIdentity,
    outcome: ExactNormalDropOutcome,
    reason: ExactNormalDropReason,
    request_dispatch: Literal[
        "not_invoked", "invoked_no_state_transition", "transitioned", "unknown"
    ],
    this_request: Literal["no_reservation", "reservation_owned"],
    history: Literal["not_applicable", "saved", "unknown"],
    fence: Literal["not_applicable", "released", "retained", "not_established"],
    prior_transition: Literal[
        "not_applicable", "not_established", "unaccounted", "unknown"
    ] = "not_applicable",
    observer_failure: bool = False,
    cleanup_failure: bool = False,
) -> _ExactNormalDropSessionResult:
    """Build one closed internal reply while echoing the caller's exact binding."""
    return _ExactNormalDropSessionResult(
        session_id=identity.session_id,
        clock_epoch_id=identity.clock_epoch_id,
        ownership_sequence=identity.ownership_sequence,
        timing_evidence_sequence=identity.timing_evidence_sequence,
        outcome=outcome,
        reason=reason,
        request_dispatch=request_dispatch,
        this_request=this_request,
        history=history,
        fence=fence,
        prior_transition=prior_transition,
        observer_failure=observer_failure,
        cleanup_failure=cleanup_failure,
    )


def _exact_normal_drop_settlement_result(
    *,
    identity: NormalDropIdentity,
    settlement: object | None,
    request_dispatch: Literal[
        "not_invoked", "invoked_no_state_transition", "transitioned", "unknown"
    ],
    reason: ExactNormalDropReason = None,
    prior_transition: Literal[
        "not_applicable", "not_established", "unaccounted", "unknown"
    ] = "not_applicable",
    observer_failure: bool = False,
    cleanup_failure: bool = False,
) -> _ExactNormalDropSessionResult:
    """Map an owned exact-drop settlement without implying a physical result."""
    if settlement is None:
        return _exact_normal_drop_result(
            identity=identity,
            outcome="unknown",
            reason="retain_unavailable",
            request_dispatch=request_dispatch,
            this_request="reservation_owned",
            history="unknown",
            fence="not_established",
            prior_transition=prior_transition,
            observer_failure=observer_failure,
            cleanup_failure=cleanup_failure,
        )
    if not isinstance(settlement, NormalDropSettlement):
        return _exact_normal_drop_result(
            identity=identity,
            outcome="unknown",
            reason=reason or "settlement_unavailable",
            request_dispatch=request_dispatch,
            this_request="reservation_owned",
            history="unknown",
            fence="not_established",
            prior_transition=prior_transition,
            observer_failure=observer_failure,
            cleanup_failure=cleanup_failure,
        )
    if settlement.outcome == "success":
        return _exact_normal_drop_result(
            identity=identity,
            outcome="success",
            reason=reason,
            request_dispatch=request_dispatch,
            this_request="reservation_owned",
            history="saved",
            fence="released",
            prior_transition=prior_transition,
            observer_failure=observer_failure,
            cleanup_failure=cleanup_failure,
        )
    if settlement.outcome == "cancelled":
        return _exact_normal_drop_result(
            identity=identity,
            outcome="cancelled",
            reason=reason or "emergency_since_session_baseline",
            request_dispatch=request_dispatch,
            this_request="reservation_owned",
            history="not_applicable",
            fence="released",
            prior_transition="not_established",
            observer_failure=observer_failure,
            cleanup_failure=cleanup_failure,
        )
    return _exact_normal_drop_result(
        identity=identity,
        outcome="unknown",
        reason="settlement_stale" if settlement.outcome == "stale" else reason,
        request_dispatch=request_dispatch,
        this_request="reservation_owned",
        history="unknown",
        fence="retained" if settlement.outcome == "unknown" else "not_established",
        prior_transition=("unknown" if settlement.outcome == "stale" else prior_transition),
        observer_failure=observer_failure,
        cleanup_failure=cleanup_failure,
    )


def _exact_normal_drop_store_refusal_reason(rejection: object) -> ExactNormalDropReason:
    """Map closed store refusals without exposing an open reason vocabulary."""
    known_rejections: frozenset[ExactNormalDropReason] = frozenset(
        {
            "invalid_identity",
            "unknown_session",
            "not_latest_session",
            "clock_epoch_mismatch",
            "ownership_sequence_mismatch",
            "session_purpose_not_eligible",
            "session_faulted",
            "session_not_active",
            "timing_evidence_missing",
            "timing_evidence_superseded",
            "finalisation_in_progress",
            "command_in_progress",
            "phase_invalid",
            "prior_drop_transition_unaccounted",
        }
    )
    if isinstance(rejection, str) and rejection in known_rejections:
        return cast(ExactNormalDropReason, rejection)
    return "store_refusal_unavailable"


def _retain_exact_normal_drop_unknown(
    server_context: ServerContext,
    *,
    session: RoastSession,
    reservation: object,
) -> NormalDropSettlement | None:
    """Try once to retain an exact reservation without fabricating settlement."""
    try:
        settlement = server_context.session_store.retain_normal_drop_unknown(
            session, reservation=reservation
        )
        if _is_expected_normal_drop_settlement(settlement, operation="retention"):
            return settlement
        return None
    except Exception:  # noqa: BLE001 - a failed fallback must not be retried.
        return None


def _is_expected_normal_drop_settlement(
    settlement: object,
    *,
    operation: Literal["cancelled", "completion", "retention"],
) -> bool:
    """Return whether a store reply is a complete variant for one operation."""
    if not isinstance(settlement, NormalDropSettlement):
        return False
    if settlement.outcome == "stale":
        return settlement.event is None and settlement.snapshot is None
    if operation == "cancelled":
        return (
            settlement.outcome == "cancelled"
            and settlement.event is None
            and isinstance(settlement.snapshot, RoastSession)
        )
    if operation == "retention":
        return (
            settlement.outcome == "unknown"
            and settlement.event is None
            and isinstance(settlement.snapshot, RoastSession)
        )
    return (
        settlement.outcome == "success"
        and isinstance(settlement.event, RoastEvent)
        and isinstance(settlement.snapshot, RoastSession)
    ) or (
        settlement.outcome == "unknown"
        and settlement.event is None
        and isinstance(settlement.snapshot, RoastSession)
    )


class _ExactNormalDropReservationGuard:
    """Own and retain one exact Drop reservation across the whole operation."""

    def __init__(
        self,
        server_context: ServerContext,
    ) -> None:
        self._server_context = server_context
        self._session: RoastSession | None = None
        self._reservation: object | None = None
        self._retention_attempted = False
        self._settled = False

    def __enter__(self) -> _ExactNormalDropReservationGuard:
        """Start the one ownership interval before admission."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Best-effort retain an owned unsettled reservation on interruption."""
        del exc_value, traceback
        if exc_type is not None:
            with suppress(BaseException):
                self.retain_once()
        return False

    def acquire(self, *, session: RoastSession, reservation: object) -> None:
        """Record ownership only after a successful dispatch admission."""
        self._session = session
        self._reservation = reservation

    def mark_settled(self) -> None:
        """Record that terminal store settlement transferred ownership."""
        self._settled = True

    def retain_once(self) -> NormalDropSettlement | None:
        """Attempt the sole best-effort uncertainty retention for this request."""
        if (
            self._settled
            or self._retention_attempted
            or self._session is None
            or self._reservation is None
        ):
            return None
        self._retention_attempted = True
        return _retain_exact_normal_drop_unknown(
            self._server_context,
            session=self._session,
            reservation=self._reservation,
        )


def _freeze_exact_normal_drop_observation(
    server_context: ServerContext,
    *,
    session: RoastSession,
    ticket: object | None,
    reason: str,
) -> bool:
    """Freeze a returned observation ticket and report whether freezing failed."""
    try:
        _freeze_drop_observation_boundary(
            server_context,
            session=session,
            ticket=ticket,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 - the store outcome remains authoritative.
        return True
    return False


def _run_normal_drop_exact_session(  # pyright: ignore[reportUnusedFunction]
    server_context: ServerContext,
    *,
    session_id: str,
    clock_epoch_id: str,
    ownership_sequence: int,
    timing_evidence_sequence: int,
) -> _ExactNormalDropSessionResult:
    """Run one exact Drop beneath its single admission-to-settlement boundary."""
    with _ExactNormalDropReservationGuard(server_context) as ownership:
        return _run_owned_normal_drop_exact_session(
            server_context,
            session_id=session_id,
            clock_epoch_id=clock_epoch_id,
            ownership_sequence=ownership_sequence,
            timing_evidence_sequence=timing_evidence_sequence,
            ownership=ownership,
        )


def _run_owned_normal_drop_exact_session(
    server_context: ServerContext,
    *,
    session_id: str,
    clock_epoch_id: str,
    ownership_sequence: int,
    timing_evidence_sequence: int,
    ownership: _ExactNormalDropReservationGuard,
) -> _ExactNormalDropSessionResult:
    """Execute one unregistered, exact, manual-authority mock normal Drop.

    The session-start baseline and the store reservation deliberately fence this
    producer without changing the existing MCP Drop or emergency routes.
    """
    identity = NormalDropIdentity(
        session_id=session_id,
        clock_epoch_id=clock_epoch_id,
        ownership_sequence=ownership_sequence,
        timing_evidence_sequence=timing_evidence_sequence,
    )
    if not _exact_normal_drop_capable(server_context):
        return _exact_normal_drop_result(
            identity=identity,
            outcome="refused_capability",
            reason="manual_mock_normal_drop_required",
            request_dispatch="not_invoked",
            this_request="no_reservation",
            history="not_applicable",
            fence="not_applicable",
        )

    driver = cast(MockRoasterDriver, server_context.roaster_driver)
    assert type(driver) is MockRoasterDriver
    session: RoastSession | None = None
    reservation = None
    baseline: _ExactNormalDropBaseline | None = None
    with server_context.lifecycle_barrier:
        baseline = server_context.exact_normal_drop_baseline
        if (
            baseline is None
            or baseline.session_id != identity.session_id
            or baseline.driver_ref is not driver
            or baseline.clock_epoch_id != identity.clock_epoch_id
        ):
            return _exact_normal_drop_result(
                identity=identity,
                outcome="refused_state",
                reason="exact_baseline_unavailable",
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
            )
        try:
            latest_snapshot = server_context.session_store.get_session_snapshot()
            session = server_context.session_store.get_latest_session()
        except Exception:  # noqa: BLE001 - unavailable first-crack state cannot admit Drop.
            return _exact_normal_drop_result(
                identity=identity,
                outcome="refused_precondition",
                reason="human_first_crack_unavailable",
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
            )
        if session is None:
            return _exact_normal_drop_result(
                identity=identity,
                outcome="refused_state",
                reason="exact_live_owner_unavailable",
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
            )
        if session.id == identity.session_id and not any(
            event.kind == "beans_dropped" for event in latest_snapshot.event_timeline
        ):
            if latest_snapshot.id != identity.session_id:
                return _exact_normal_drop_result(
                    identity=identity,
                    outcome="refused_precondition",
                    reason="human_first_crack_unavailable",
                    request_dispatch="not_invoked",
                    this_request="no_reservation",
                    history="not_applicable",
                    fence="not_applicable",
                )
            human_first_crack = latest_snapshot.first_crack_source == "manual_mark" and any(
                event.kind == "first_crack_detected" for event in latest_snapshot.event_timeline
            )
            if not human_first_crack:
                return _exact_normal_drop_result(
                    identity=identity,
                    outcome="refused_precondition",
                    reason="human_first_crack_missing",
                    request_dispatch="not_invoked",
                    this_request="no_reservation",
                    history="not_applicable",
                    fence="not_applicable",
                )
        admission = server_context.session_store.admit_normal_drop(identity)
        if admission.outcome == "dispatch" and admission.reservation is None:
            return _exact_normal_drop_result(
                identity=identity,
                outcome="refused_state",
                reason="dispatch_reservation_unavailable",
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
            )
        if admission.outcome != "dispatch":
            if admission.outcome == "recorded":
                return _exact_normal_drop_result(
                    identity=identity,
                    outcome="already_dropped",
                    reason="endpoint_origin_not_established",
                    request_dispatch="not_invoked",
                    this_request="no_reservation",
                    history="not_applicable",
                    fence="not_applicable",
                )
            if admission.outcome == "in_progress":
                return _exact_normal_drop_result(
                    identity=identity,
                    outcome="in_progress",
                    reason="normal_drop_in_progress",
                    request_dispatch="not_invoked",
                    this_request="no_reservation",
                    history="not_applicable",
                    fence="not_applicable",
                )
            if admission.outcome == "ambiguous":
                return _exact_normal_drop_result(
                    identity=identity,
                    outcome="ambiguous",
                    reason="normal_drop_unresolved",
                    request_dispatch="not_invoked",
                    this_request="no_reservation",
                    history="unknown",
                    fence="retained",
                    prior_transition="unknown",
                )
            return _exact_normal_drop_result(
                identity=identity,
                outcome=admission.outcome,
                reason=_exact_normal_drop_store_refusal_reason(admission.rejection),
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
                prior_transition=(
                    "unaccounted"
                    if admission.rejection == "prior_drop_transition_unaccounted"
                    else "not_applicable"
                ),
            )
        if admission.reservation is None:
            return _exact_normal_drop_result(
                identity=identity,
                outcome="refused_state",
                reason="dispatch_reservation_unavailable",
                request_dispatch="not_invoked",
                this_request="no_reservation",
                history="not_applicable",
                fence="not_applicable",
            )
        reservation = admission.reservation
        ownership.acquire(session=session, reservation=reservation)

    assert session is not None
    _revoke_manual_mark_publication_for_accepted_drop(server_context, session=session)
    try:
        arbitration = ExactDropArbitration(
            operation_id=reservation.token,
            session_id=reservation.identity.session_id,
            cancelled=Event(),
            expected_drop_transition_count=baseline.drop_baseline,
            expected_emergency_count=baseline.emergency_baseline,
        )
    except Exception:
        settlement = ownership.retain_once()
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="not_invoked",
            reason="arbitration_unavailable",
        )
    except BaseException:
        with suppress(BaseException):
            ownership.retain_once()
        raise

    try:
        driver_state = cast(object, driver.drop_beans(exact_drop=arbitration))
    except ExactDropCancelled as error:
        if error.discriminator in {"emergency_count_changed", "emergency_cancelled"}:
            try:
                proof = NormalDropNonDispatchProof(reservation=reservation, zero_actuation=True)
                settlement = server_context.session_store.settle_normal_drop_cancelled(
                    session, reservation=reservation, proof=proof
                )
            except Exception:
                settlement = ownership.retain_once()
            except BaseException:
                with suppress(BaseException):
                    ownership.retain_once()
                raise
            else:
                if _is_expected_normal_drop_settlement(settlement, operation="cancelled"):
                    ownership.mark_settled()
                else:
                    settlement = ownership.retain_once()
            return _exact_normal_drop_settlement_result(
                identity=identity,
                settlement=settlement,
                request_dispatch="invoked_no_state_transition",
                reason="emergency_since_session_baseline",
                prior_transition="not_established",
            )
        settlement = ownership.retain_once()
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="invoked_no_state_transition",
            reason="prior_drop_transition_unaccounted",
            prior_transition="unaccounted",
        )
    except Exception:
        settlement = ownership.retain_once()
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="unknown",
            reason="driver_result_unavailable",
        )
    except BaseException:
        with suppress(BaseException):
            ownership.retain_once()
        raise

    try:
        valid_projection = isinstance(
            driver_state, RoasterState
        ) and _is_valid_mock_drop_projection(
            heat_level_percent=driver_state.heat_level_percent,
            fan_level_percent=driver_state.fan_level_percent,
            cooling_on=driver_state.cooling_on,
        )
    except BaseException:
        with suppress(BaseException):
            ownership.retain_once()
        raise
    if not valid_projection:
        settlement = ownership.retain_once()
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="unknown",
            reason="driver_result_invalid",
        )
    assert isinstance(driver_state, RoasterState)

    observation_ticket: object | None = None
    try:
        observation_ticket = _begin_drop_observation_boundary(server_context, session=session)
    except Exception:
        settlement = ownership.retain_once()
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="transitioned",
            reason="observation_boundary_unavailable",
            observer_failure=True,
        )
    except BaseException:
        with suppress(BaseException):
            ownership.retain_once()
        raise

    try:
        settlement = cast(
            object,
            server_context.session_store.complete_normal_drop_success(
                session,
                reservation=reservation,
                heat_level_percent=driver_state.heat_level_percent,
                fan_level_percent=driver_state.fan_level_percent,
                cooling_on=driver_state.cooling_on,
            ),
        )
    except Exception:
        settlement = ownership.retain_once()
        freeze_failed = _freeze_exact_normal_drop_observation(
            server_context,
            session=session,
            ticket=observation_ticket,
            reason="exact_drop_settlement_unresolved",
        )
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="transitioned",
            reason="observation_freeze_failed" if freeze_failed else "settlement_unavailable",
            observer_failure=freeze_failed,
        )
    except BaseException:
        with suppress(BaseException):
            ownership.retain_once()
        with suppress(BaseException):
            _freeze_exact_normal_drop_observation(
                server_context,
                session=session,
                ticket=observation_ticket,
                reason="exact_drop_settlement_unresolved",
            )
        raise

    if not _is_expected_normal_drop_settlement(settlement, operation="completion"):
        settlement = ownership.retain_once()
        freeze_failed = _freeze_exact_normal_drop_observation(
            server_context,
            session=session,
            ticket=observation_ticket,
            reason="exact_drop_settlement_unresolved",
        )
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="transitioned",
            reason="observation_freeze_failed" if freeze_failed else "settlement_unavailable",
            observer_failure=freeze_failed,
        )

    ownership.mark_settled()
    assert isinstance(settlement, NormalDropSettlement)

    if settlement.outcome != "success":
        freeze_failed = _freeze_exact_normal_drop_observation(
            server_context,
            session=session,
            ticket=observation_ticket,
            reason="exact_drop_settlement_unresolved",
        )
        return _exact_normal_drop_settlement_result(
            identity=identity,
            settlement=settlement,
            request_dispatch="transitioned",
            reason="observation_freeze_failed" if freeze_failed else "endpoint_history_unresolved",
            observer_failure=freeze_failed,
        )

    validated_settlement = settlement
    observer_failure = False
    freeze_failure = False
    cleanup_failure = False
    try:
        _resolve_drop_observation_boundary(
            server_context,
            session=session,
            ticket=observation_ticket,
            event=cast(RoastEvent, validated_settlement.event),
        )
    except Exception:
        observer_failure = True
        freeze_failure = _freeze_exact_normal_drop_observation(
            server_context,
            session=session,
            ticket=observation_ticket,
            reason="exact_drop_observation_resolution_failed",
        )
        observer_failure = freeze_failure or observer_failure
    except BaseException:
        with suppress(BaseException):
            _freeze_exact_normal_drop_observation(
                server_context,
                session=session,
                ticket=observation_ticket,
                reason="exact_drop_observation_resolution_interrupted",
            )
        raise
    try:
        _stop_runtimes_after_drop(
            server_context,
            session=session,
            snapshot=cast(RoastSession, validated_settlement.snapshot),
        )
    except Exception:
        cleanup_failure = True
    return _exact_normal_drop_settlement_result(
        identity=identity,
        settlement=validated_settlement,
        request_dispatch="transitioned",
        reason=(
            "observation_freeze_failed"
            if freeze_failure
            else "observation_resolution_failed"
            if observer_failure
            else "post_drop_cleanup_failed"
            if cleanup_failure
            else None
        ),
        observer_failure=observer_failure,
        cleanup_failure=cleanup_failure,
    )


def _read_capture_evidence(
    server_context: ServerContext, expected_session_id: str
) -> CaptureEvidenceResult:
    """Bracket one immutable publication with two nonblocking store samples."""
    empty = _capture_evidence_empty
    if not server_context.capture_evidence_configured:
        return empty("refused", "not_configured")
    slot_count = max(1, len(server_context.config.recording.devices or ()))
    if slot_count > 8:
        return empty("refused", "inventory_too_large")
    first = server_context.session_store.try_get_capture_subject()
    if first is None:
        return empty("unavailable", "store_contended")
    if first.presence == "starting":
        return empty("unavailable", "start_in_flight")
    if first.presence == "none":
        return empty("refused", "no_session")
    if first.session_id != expected_session_id:
        return empty("refused", "wrong_session")
    if first.session_id is None or first.ownership_sequence is None:
        return empty("unavailable", "subject_changed")
    snapshot = server_context.capture_evidence_publisher.try_snapshot()
    if snapshot is False:
        return empty("unavailable", "publication_contended")
    second = server_context.session_store.try_get_capture_subject()
    if second is None:
        return empty("unavailable", "store_contended")
    if not _same_capture_subject(first, second):
        return empty("unavailable", "subject_changed")
    if snapshot is None or not _snapshot_belongs_to_subject(snapshot, first):
        return _capture_evidence_empty("unavailable", "capture_pending", subject=first)
    if snapshot.setup_failed:
        return _capture_evidence_empty("unavailable", "setup_failed", subject=first)
    if snapshot.recorder_unavailable:
        return _capture_evidence_empty("unavailable", "recorder_unavailable", subject=first)
    now = time.monotonic()
    age = now - snapshot.published_at_monotonic_seconds
    if not math.isfinite(now) or not math.isfinite(age) or age < 0:
        return empty("unavailable", "clock_unreliable")
    inputs = tuple(
        CaptureEvidenceInputResult(
            slot=row.slot,
            role=row.role,
            input_generation=row.input_generation,
            opened_input_token=row.opened_input_token,
            identity=row.identity,
            requested_rate_hz=row.requested_rate_hz,
            reported_rate_hz=(
                float(row.reported_rate_hz) if row.reported_rate_hz is not None else None
            ),
            wav_header_rate_hz=(
                float(row.wav_header_rate_hz) if row.wav_header_rate_hz is not None else None
            ),
            input_frames=row.input_frames,
            accepted_frames=row.accepted_frames,
            flushed_frames=row.flushed_frames,
            state=row.state,
            failure=row.failure,
        )
        for row in snapshot.inputs
    )
    return CaptureEvidenceResult(
        capture_protocol="capture_evidence/v1",
        intent="current_session",
        outcome="observed",
        reason=None,
        session_id=first.session_id,
        session_state=first.presence,
        recorder_generation=snapshot.recorder_generation,
        publication_sequence=snapshot.publication_sequence,
        age_seconds=float(age),
        inputs=inputs,
    )


def _capture_evidence_empty(
    outcome: CaptureEvidenceOutcome,
    reason: CaptureEvidenceReason,
    *,
    subject: CaptureSubject | None = None,
) -> CaptureEvidenceResult:
    """Build one exact empty current-session response without a fallback cache."""
    matched = reason in {"capture_pending", "setup_failed", "recorder_unavailable"}
    return CaptureEvidenceResult(
        capture_protocol="capture_evidence/v1",
        intent="current_session",
        outcome=outcome,
        reason=reason,
        session_id=subject.session_id if matched and subject is not None else None,
        session_state=(
            cast(Literal["active", "stopped"], subject.presence)
            if matched and subject is not None
            else None
        ),
        recorder_generation=None,
        publication_sequence=None,
        age_seconds=None,
        inputs=(),
    )


def _same_capture_subject(left: CaptureSubject, right: CaptureSubject) -> bool:
    """Return exact equality for the bounded store-read bracket."""
    return (
        left.presence == right.presence
        and left.session_id == right.session_id
        and left.ownership_sequence == right.ownership_sequence
    )


def _snapshot_belongs_to_subject(
    snapshot: CaptureEvidenceSnapshot, subject: CaptureSubject
) -> bool:
    """Return whether immutable evidence is owned by the sampled subject."""
    return (
        subject.session_id is not None
        and subject.ownership_sequence is not None
        and snapshot.session_id == subject.session_id
        and snapshot.ownership_sequence == subject.ownership_sequence
    )


def _process_first_crack_runtime_for_active_session(
    server_context: ServerContext,
    *,
    session_id: str | None,
) -> None:
    """Process queued detector windows when the requested session is active."""
    active_session = server_context.session_store.get_active_session()
    if active_session is None:
        return
    if session_id is not None and session_id != active_session.id:
        return
    if server_context.session_store.finalisation_blocks_session(active_session.id):
        return
    server_context.first_crack_runtime.process_available_windows(
        session_store=server_context.session_store,
        session=active_session,
    )


def _process_ambient_runtime_for_active_session(
    server_context: ServerContext,
    *,
    session_id: str | None,
) -> None:
    """Refresh the ambient sensor cache when the requested session is active.

    The ambient runtime's own `poll` is already bounded by
    `poll_interval_seconds` and fail-soft on read errors, so this is safe to
    call on every `get_roast_state` tick without hitting the USB bus each time
    or ever raising into the state-read hot path.
    """
    active_session = server_context.session_store.get_active_session()
    if active_session is None:
        return
    if session_id is not None and session_id != active_session.id:
        return
    if server_context.session_store.finalisation_blocks_session(active_session.id):
        return
    server_context.ambient_runtime.poll()


def _process_auto_t0_for_active_session(
    server_context: ServerContext,
    *,
    session_id: str | None,
    device_state: RoasterDeviceState,
    defer_canonical_settlement: bool = False,
) -> bool:
    """Process automatic T0 after a successful configured-driver state read."""
    if not server_context.config.session.auto_t0_detection_enabled:
        return False
    active_session = server_context.session_store.get_active_session()
    if active_session is None:
        return False
    if session_id is not None and session_id != active_session.id:
        return False
    if server_context.session_store.finalisation_blocks_session(active_session.id):
        return False
    if active_session.phase != "pre_roast" or active_session.beans_added_at_utc is not None:
        return False
    # The exact Drop persistence window begins only after the session leaves
    # pre-roast through the beans-added/drop lifecycle. It cannot overlap this
    # pre-T0 backdating path, so the established backdating behavior remains.
    if not device_state.connected or device_state.bean_temp_c is None:
        return False

    def discard_pre_t0_windows_after_live_settlement() -> None:
        """Discard only observations made obsolete by a committed live charge."""
        server_context.first_crack_runtime.discard_queued_windows_for_session(
            active_session.id,
            reason="Dropped queued pre-T0 detector windows after automatic T0.",
        )

    event, _ = server_context.session_store.process_auto_t0_reading_snapshot(
        active_session,
        bean_temp_c=device_state.bean_temp_c,
        drop_threshold_c=server_context.config.session.auto_t0_drop_threshold_c,
        defer_canonical_settlement=defer_canonical_settlement,
        on_live_settlement=(
            discard_pre_t0_windows_after_live_settlement if defer_canonical_settlement else None
        ),
    )
    return event is not None


def _timing_evidence_result(
    *,
    outcome: TimingEvidenceOutcome,
    requested_session_id: str,
    reason_code: TimingEvidenceReason | None,
    driver_read: Literal["not_invoked", "invoked"],
    record: object | None = None,
) -> TimingEvidenceResult:
    """Build and validate one closed timing-evidence result without leakage."""
    return TimingEvidenceResult.model_validate(
        {
            "evidence_protocol": "timing_evidence/v1",
            "evidence_version": 1,
            "outcome": outcome,
            "reason_code": reason_code,
            "driver_read": driver_read,
            "requested_session_id": requested_session_id,
            "record": record,
        }
    )


def _timing_evidence_capability(
    server_context: ServerContext,
) -> TimingEvidenceReason | None:
    """Check the mock-only timing-evidence composition without driver I/O."""
    driver: Any = server_context.roaster_driver
    try:
        capabilities: Any = driver.capabilities
    except Exception:  # noqa: BLE001 - untrusted adapter declarations fail closed.
        return "capability_unreadable"
    if type(capabilities) is not RoasterCapabilities:
        return "capability_malformed"
    try:
        version: Any = capabilities.timing_evidence_version
    except Exception:  # noqa: BLE001 - malformed declarations fail closed.
        return "capability_malformed"
    if version is None:
        return "capability_absent"
    if type(version) is not int:
        return "capability_malformed"
    if version != 1:
        return "capability_version_unsupported"
    try:
        driver_name: Any = driver.name
        capability_driver: Any = capabilities.driver
        configured_driver: Any = server_context.config.roaster.driver
    except Exception:  # noqa: BLE001 - composition inspection is not driver I/O.
        return "composition_not_qualified"
    if (
        type(driver) is not MockRoasterDriver
        or type(driver_name) is not str
        or driver_name != "mock"
        or type(capability_driver) is not str
        or capability_driver != "mock"
        or type(configured_driver) is not str
        or configured_driver != "mock"
    ):
        return "composition_not_qualified"
    return None


def _timing_clock_value(store: RoastSessionStore) -> float | None:
    """Read one exact finite store clock value without exposing exceptions."""
    try:
        value: object = store.read_timing_evidence_clock()
    except Exception:  # noqa: BLE001 - clock failures are a closed unavailable result.
        return None
    if type(value) is not float or not math.isfinite(value):
        return None
    return value


def _classify_timing_channel(value: object) -> Mapping[str, object]:
    """Classify one driver channel without coercion or shared-channel failure."""
    if value is None:
        return {"status": "unavailable", "reason": "reported_none"}
    if type(value) is not float:
        return {"status": "invalid", "reason": "not_exact_float"}
    if not math.isfinite(value):
        return {"status": "invalid", "reason": "non_finite"}
    return {"status": "valid", "celsius": value}


def _run_timing_evidence(
    server_context: ServerContext,
    *,
    expected_session_id: str,
) -> TimingEvidenceResult:
    """Run one atomic mock-only timing-evidence read without lifecycle barriers."""
    store = server_context.session_store
    identity_rejection = store.inspect_timing_evidence_identity(expected_session_id)
    if identity_rejection is not None:
        return _timing_evidence_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            reason_code=identity_rejection,
            driver_read="not_invoked",
        )
    capability_rejection = _timing_evidence_capability(server_context)
    if capability_rejection is not None:
        return _timing_evidence_result(
            outcome="refused_capability",
            requested_session_id=expected_session_id,
            reason_code=capability_rejection,
            driver_read="not_invoked",
        )
    admission = store.admit_timing_evidence(expected_session_id)
    if admission.identity_rejection is not None:
        return _timing_evidence_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            reason_code=admission.identity_rejection,
            driver_read="not_invoked",
        )
    if admission.busy_rejection is not None:
        return _timing_evidence_result(
            outcome="refused_busy",
            requested_session_id=expected_session_id,
            reason_code=admission.busy_rejection,
            driver_read="not_invoked",
        )
    t0 = _timing_clock_value(store)
    if t0 is None:
        return _timing_evidence_result(
            outcome="unavailable",
            requested_session_id=expected_session_id,
            reason_code="clock_invalid",
            driver_read="not_invoked",
        )
    try:
        state: object = server_context.roaster_driver.read_state()
    except Exception:  # noqa: BLE001 - never expose adapter exception text.
        return _timing_evidence_result(
            outcome="unavailable",
            requested_session_id=expected_session_id,
            reason_code="driver_raised",
            driver_read="invoked",
        )
    t1 = _timing_clock_value(store)
    if t1 is None:
        return _timing_evidence_result(
            outcome="unavailable",
            requested_session_id=expected_session_id,
            reason_code="clock_invalid",
            driver_read="invoked",
        )
    if t1 < t0:
        return _timing_evidence_result(
            outcome="unavailable",
            requested_session_id=expected_session_id,
            reason_code="clock_regression",
            driver_read="invoked",
        )
    try:
        if (
            type(state) is not RoasterState
            or type(state.driver) is not str
            or state.driver != "mock"
            or type(state.connected) is not bool
        ):
            raise ValueError("Invalid timing driver state.")
        if state.connected is not True:
            return _timing_evidence_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="driver_not_connected",
                driver_read="invoked",
            )
        channels = {
            "bean": _classify_timing_channel(state.bean_temp_c),
            "environment": _classify_timing_channel(state.env_temp_c),
        }
    except Exception:  # noqa: BLE001 - malformed state attribute access is closed.
        return _timing_evidence_result(
            outcome="unavailable",
            requested_session_id=expected_session_id,
            reason_code="driver_result_invalid",
            driver_read="invoked",
        )
    assembled = store.assemble_timing_evidence(
        admission,
        t0=t0,
        t1=t1,
        channels=channels,
        finalize=lambda record: _timing_evidence_result(
            outcome="evidence",
            requested_session_id=expected_session_id,
            reason_code=None,
            driver_read="invoked",
            record=record,
        ),
    )
    if assembled.outcome == "evidence":
        assert isinstance(assembled.result, TimingEvidenceResult)
        return assembled.result
    return _timing_evidence_result(
        outcome=assembled.outcome,
        requested_session_id=expected_session_id,
        reason_code=assembled.reason_code,
        driver_read="invoked",
    )


def _hottop_reception_result(
    *,
    outcome: Literal[
        "evidence", "refused_identity", "refused_capability", "refused_busy", "unavailable"
    ],
    requested_session_id: str,
    reason_code: HottopReceptionReason | None,
    driver_read: Literal["not_invoked", "invoked"],
    record: object | None = None,
) -> HottopReceptionEvidenceResult:
    """Build one closed simulated-Hottop reception result without leakage."""
    return HottopReceptionEvidenceResult.model_validate(
        {
            "evidence_protocol": "hottop_reception_evidence/v1",
            "evidence_version": 1,
            "outcome": outcome,
            "reason_code": reason_code,
            "driver_read": driver_read,
            "requested_session_id": requested_session_id,
            "record": record,
        }
    )


def _hottop_reception_capability(
    server_context: ServerContext,
    *,
    trusted_composition: HottopSimulationComposition,
) -> HottopReceptionReason | None:
    """Check the closed trusted composition before obtaining a driver snapshot."""
    if server_context.simulation_composition is None:
        return "composition_not_simulated"
    if server_context.simulation_composition is not trusted_composition:
        return "composition_mismatch"
    if (
        server_context.simulation_composition_id is None
        or type(server_context.simulation_composition_id) is not str
        or re.fullmatch(_TIMING_EPOCH_PATTERN, server_context.simulation_composition_id) is None
    ):
        return "composition_mismatch"
    driver: Any = server_context.roaster_driver
    if type(driver) is not HottopRoasterDriver:
        return "capability_absent"
    try:
        capabilities: Any = driver.capabilities
        version: Any = capabilities.hottop_reception_candidate_version
        driver_name: Any = driver.name
        capability_driver: Any = capabilities.driver
        configured_driver: Any = server_context.config.roaster.driver
        configured_port: Any = server_context.config.roaster.port
        configured_mode: Any = server_context.config.roaster.hottop_reception
        configured_unit: Any = server_context.config.roaster.temperature_unit
    except Exception:  # noqa: BLE001 - declaration reads fail closed without leakage.
        return "composition_mismatch"
    if type(capabilities) is not RoasterCapabilities or version is None:
        return "capability_absent"
    if type(version) is not int or version != 1:
        return "capability_version_unsupported"
    if (
        driver_name != HOTTOP_DRIVER_NAME
        or capability_driver != HOTTOP_DRIVER_NAME
        or configured_driver != HOTTOP_DRIVER_NAME
        or configured_port != trusted_composition.synthetic_port
        or configured_mode != "candidate/v1"
        or configured_unit != "celsius"
    ):
        return "composition_mismatch"
    return None


def _project_hottop_settlement(
    settlement: object, *, expected_transition: Literal["drop", "emergency"]
) -> dict[str, object] | None:
    """Project one driver settlement without exposing driver-private revisions."""
    if settlement is None:
        return None
    transition = getattr(settlement, "transition", None)
    transition_seq = getattr(settlement, "transition_seq", None)
    result = getattr(settlement, "result", None)
    attempt_seq = getattr(settlement, "attempt_seq", None)
    physical_effect = getattr(settlement, "physical_effect", None)
    if (
        transition != expected_transition
        or type(transition_seq) is not int
        or transition_seq < 1
        or result not in {"pending", "not_attempted", "entering", "accepted_by_host", "unknown"}
        or (attempt_seq is not None and (type(attempt_seq) is not int or attempt_seq < 1))
        or physical_effect != "not_established"
    ):
        raise ValueError("Invalid Hottop command settlement.")
    return {
        "transition": transition,
        "transition_seq": transition_seq,
        "result": result,
        "attempt_seq": attempt_seq,
        "physical_effect": physical_effect,
    }


def _project_hottop_reception_pair(
    candidate: HottopExactDropCandidate,
    *,
    receipt_age_bound_seconds: float,
) -> tuple[dict[str, object], str | None]:
    """Project one immutable candidate snapshot without re-reading its driver."""
    if (
        type(receipt_age_bound_seconds) not in {int, float}
        or isinstance(receipt_age_bound_seconds, bool)
        or not math.isfinite(receipt_age_bound_seconds)
        or receipt_age_bound_seconds <= 0
    ):
        raise ValueError("Invalid trusted receipt age bound.")
    age_bound_seconds = float(receipt_age_bound_seconds)
    classification = candidate.receipt_classification
    receipt = candidate.receipt
    if type(candidate.streaming) is not bool:
        raise ValueError("Invalid Hottop streaming declaration.")
    if not candidate.streaming:
        raise RuntimeError("driver_not_streaming")
    if candidate.drop_transition_count != 0 or candidate.emergency_count != 0:
        return {"status": "ineligible", "reasons": ("superseded",)}, None
    if not classification.eligible:
        reasons = classification.reasons
        if not reasons or any(type(reason) is not str for reason in reasons):
            raise ValueError("Invalid Hottop receipt classification.")
        return {"status": "ineligible", "reasons": reasons}, None
    if (
        receipt is None
        or receipt.latest_pair_c is None
        or receipt.latest_resolved_temperature_unit != "celsius"
        or receipt.configured_temperature_unit != "celsius"
        or type(receipt.epoch) is not int
        or receipt.epoch < 1
        or type(receipt.sequence) is not int
        or receipt.sequence < 1
        or type(classification.drained_floor_age_seconds) is not float
        or type(classification.read_returned_age_seconds) is not float
        or classification.drained_floor_age_seconds < 0
        or classification.read_returned_age_seconds < 0
        or not math.isfinite(classification.drained_floor_age_seconds)
        or not math.isfinite(classification.read_returned_age_seconds)
        or classification.read_returned_age_seconds > classification.drained_floor_age_seconds
        or classification.drained_floor_age_seconds > age_bound_seconds
    ):
        raise ValueError("Eligible Hottop receipt is incomplete.")
    bean_c, environment_c = receipt.latest_pair_c
    if type(bean_c) is not float or type(environment_c) is not float:
        raise ValueError("Hottop receipt temperatures must be exact floats.")
    token = uuid4().hex
    return (
        {
            "status": "eligible",
            "bean_c": bean_c,
            "environment_c": environment_c,
            "unit": "celsius",
            "receipt_token": token,
            "reception": {
                "epoch": receipt.epoch,
                "sequence": receipt.sequence,
                "basis": "host_receipt_after_drained_floor",
            },
            "host_receipt_clock": {
                "domain": "driver_reception_monotonic",
                "relation_to_operational": "not_established",
            },
            "drained_floor_age_seconds": classification.drained_floor_age_seconds,
            "read_returned_age_seconds": classification.read_returned_age_seconds,
            "age_bound_seconds": age_bound_seconds,
            "age_bound_basis": "simulation_composition_unqualified",
        },
        token,
    )


def _run_hottop_reception_evidence(
    server_context: ServerContext,
    *,
    expected_session_id: str,
    trusted_composition: HottopSimulationComposition,
) -> HottopReceptionEvidenceResult:
    """Read a trusted immutable simulated-Hottop candidate without native I/O."""
    store = server_context.session_store
    identity_rejection = store.inspect_timing_evidence_identity(expected_session_id)
    if identity_rejection is not None:
        return _hottop_reception_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            reason_code=identity_rejection,
            driver_read="not_invoked",
        )
    capability_rejection = _hottop_reception_capability(
        server_context, trusted_composition=trusted_composition
    )
    if capability_rejection is not None:
        return _hottop_reception_result(
            outcome="refused_capability",
            requested_session_id=expected_session_id,
            reason_code=capability_rejection,
            driver_read="not_invoked",
        )
    admission = store.admit_timing_evidence(expected_session_id)
    if admission.identity_rejection is not None:
        return _hottop_reception_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            reason_code=admission.identity_rejection,
            driver_read="not_invoked",
        )
    if admission.busy_rejection is not None:
        return _hottop_reception_result(
            outcome="refused_busy",
            requested_session_id=expected_session_id,
            reason_code=admission.busy_rejection,
            driver_read="not_invoked",
        )
    with server_context.hottop_reception_lock:
        t0 = _timing_clock_value(store)
        if t0 is None:
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="clock_invalid",
                driver_read="not_invoked",
            )
        try:
            driver = cast(HottopRoasterDriver, server_context.roaster_driver)
            candidate: object = driver.snapshot_exact_drop_candidate()
        except Exception:  # noqa: BLE001 - never expose adapter failure text.
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="driver_raised",
                driver_read="invoked",
            )
        t1 = _timing_clock_value(store)
        if t1 is None:
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="clock_invalid",
                driver_read="invoked",
            )
        if t1 < t0:
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="clock_regression",
                driver_read="invoked",
            )
        if type(candidate) is not HottopExactDropCandidate:
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="record_invalid",
                driver_read="invoked",
            )
        try:
            pair, token = _project_hottop_reception_pair(
                candidate,
                receipt_age_bound_seconds=trusted_composition.receipt_age_bound_seconds,
            )
        except RuntimeError as exc:
            if str(exc) == "driver_not_streaming":
                return _hottop_reception_result(
                    outcome="unavailable",
                    requested_session_id=expected_session_id,
                    reason_code="driver_not_streaming",
                    driver_read="invoked",
                )
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="record_invalid",
                driver_read="invoked",
            )
        except Exception:  # noqa: BLE001 - malformed immutable snapshot is unavailable.
            return _hottop_reception_result(
                outcome="unavailable",
                requested_session_id=expected_session_id,
                reason_code="record_invalid",
                driver_read="invoked",
            )

        def finalize(core: dict[str, object]) -> HottopReceptionEvidenceResult:
            record_pair = pair
            # A command transition can be recorded by the store after the
            # immutable driver snapshot but before this store-owned record.
            # It must never inherit a pre-transition receipt token.
            if core["drop"] is not None:
                record_pair = {"status": "ineligible", "reasons": ("superseded",)}
            settlements = {
                "drop": _project_hottop_settlement(
                    candidate.drop_settlement, expected_transition="drop"
                ),
                "emergency": _project_hottop_settlement(
                    candidate.emergency_settlements[-1]
                    if candidate.emergency_settlements
                    else None,
                    expected_transition="emergency",
                ),
            }
            record = {
                **core,
                "driver": HOTTOP_DRIVER_NAME,
                "composition": {
                    "kind": "simulated_injected_transport",
                    "composition_id": server_context.simulation_composition_id,
                    "reception_capability_version": 1,
                },
                "pair": record_pair,
                "command_settlements": settlements,
                "sensor_age": "unknown",
                "physical_qualification": "none_simulated_transport_only",
            }
            result = _hottop_reception_result(
                outcome="evidence",
                requested_session_id=expected_session_id,
                reason_code=None,
                driver_read="invoked",
                record=record,
            )
            assert result.record is not None
            server_context.hottop_reception_registry.install(
                _HottopReceptionIssuedEvidence(
                    session_id=result.record.session_id,
                    ownership_sequence=result.record.session_ownership_sequence,
                    reception_evidence_sequence=result.record.reception_evidence_sequence,
                    receipt_token=token,
                    candidate=candidate,
                )
                if token is not None and record_pair["status"] == "eligible"
                else None,
            )
            return result

        assembled = store.assemble_hottop_reception_evidence(
            admission, t0=t0, t1=t1, finalize=finalize
        )
    if assembled.outcome == "evidence":
        assert isinstance(assembled.result, HottopReceptionEvidenceResult)
        return assembled.result
    return _hottop_reception_result(
        outcome=assembled.outcome,
        requested_session_id=expected_session_id,
        reason_code=assembled.reason_code,
        driver_read="invoked",
    )


def _fixed_zero_result(
    *,
    outcome: FixedZeroOutcome,
    requested_session_id: str,
    validated_session_id: str | None,
    commanded_state: FixedZeroCommandedStateResult | None,
    session_projection: Literal["recorded_in_memory", "unknown", "not_changed"],
    reason_code: FixedZeroReason | None,
) -> FixedZeroContainmentResult:
    """Build and validate one exact fixed-zero business result."""
    return FixedZeroContainmentResult.model_validate(
        {
            "containment_protocol": "fixed_zero/v1",
            "containment_version": 1,
            "outcome": outcome,
            "request_dispatch": "invoked"
            if outcome in {"command_state_returned", "unknown_driver_result", "unknown_completion"}
            else "not_invoked",
            "automatic_retry_permitted": False,
            "requested_session_id": requested_session_id,
            "validated_session_id": validated_session_id,
            "commanded_state": commanded_state,
            "session_projection": session_projection,
            "durability": "not_provided_by_this_tool",
            "reason_code": reason_code,
        }
    )


def _fixed_zero_capability(
    server_context: ServerContext,
) -> tuple[RoasterCapabilities | None, FixedZeroReason | None]:
    """Return the explicit fixed-zero capability declaration without I/O."""
    try:
        capabilities: Any = server_context.roaster_driver.capabilities
    except Exception:  # noqa: BLE001 - untrusted adapter declarations fail closed.
        return None, "capability_unreadable"
    if not isinstance(capabilities, RoasterCapabilities):
        return None, "capability_malformed"
    version: Any = capabilities.heat_containment_version
    if version is None:
        return None, "capability_absent"
    if isinstance(version, bool) or not isinstance(version, int):
        return None, "capability_malformed"
    if version != 1:
        return None, "capability_version_unsupported"
    try:
        driver: Any = server_context.roaster_driver
        driver_name: Any = driver.name
        containment_port = driver.contain_heat_fixed_zero
    except Exception:  # noqa: BLE001 - a property/port read is not command I/O.
        return None, "capability_unreadable"
    if (
        not isinstance(driver_name, str)
        or re.fullmatch(_FIXED_ZERO_DRIVER_IDENTIFIER_PATTERN, driver_name, flags=re.ASCII) is None
        or driver_name != capabilities.driver
    ):
        return None, "capability_malformed"
    if not callable(containment_port):
        return None, "capability_port_missing"
    return capabilities, None


def _validated_fixed_zero_commanded_state(
    evidence: object,
    *,
    capability_driver: str,
) -> FixedZeroCommandedStateResult | None:
    """Validate one driver result without accepting coercion or extra fields."""
    if not isinstance(evidence, FixedZeroCommandedState) or evidence.driver != capability_driver:
        return None
    try:
        return FixedZeroCommandedStateResult.model_validate(evidence.as_dict())
    except Exception:  # noqa: BLE001 - malformed driver evidence is a business outcome.
        return None


def _run_fixed_zero_heat_containment(
    server_context: ServerContext,
    *,
    expected_session_id: str,
) -> FixedZeroContainmentResult:
    """Run one fixed-zero command with exact-session uncertainty preservation."""
    identity_rejection = server_context.session_store.inspect_fixed_zero_containment_identity(
        expected_session_id
    )
    if identity_rejection is not None:
        return _fixed_zero_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            validated_session_id=None,
            commanded_state=None,
            session_projection="not_changed",
            reason_code=identity_rejection,
        )
    capabilities, capability_rejection = _fixed_zero_capability(server_context)
    if capability_rejection is not None:
        return _fixed_zero_result(
            outcome="refused_capability",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=None,
            session_projection="not_changed",
            reason_code=capability_rejection,
        )
    assert capabilities is not None
    admission = server_context.session_store.reserve_fixed_zero_containment(expected_session_id)
    if admission.identity_rejection is not None:
        return _fixed_zero_result(
            outcome="refused_identity",
            requested_session_id=expected_session_id,
            validated_session_id=None,
            commanded_state=None,
            session_projection="not_changed",
            reason_code=admission.identity_rejection,
        )
    if admission.busy_rejection is not None:
        return _fixed_zero_result(
            outcome="refused_busy",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=None,
            session_projection="not_changed",
            reason_code=admission.busy_rejection,
        )
    session = admission.session
    reservation = admission.reservation
    assert session is not None and reservation is not None
    try:
        driver: Any = server_context.roaster_driver
        evidence: Any = driver.contain_heat_fixed_zero()
    except Exception:  # noqa: BLE001 - a raised invocation has unknown settlement.
        return _fixed_zero_result(
            outcome="unknown_driver_result",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=None,
            session_projection="unknown",
            reason_code="driver_raised",
        )
    if evidence is None:
        return _fixed_zero_result(
            outcome="unknown_driver_result",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=None,
            session_projection="unknown",
            reason_code="driver_result_missing",
        )
    commanded_state = _validated_fixed_zero_commanded_state(
        evidence, capability_driver=capabilities.driver
    )
    if commanded_state is None:
        return _fixed_zero_result(
            outcome="unknown_driver_result",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=None,
            session_projection="unknown",
            reason_code="driver_result_invalid",
        )
    try:
        server_context.session_store.complete_fixed_zero_containment_snapshot(
            session, reservation=reservation
        )
    except SessionLifecycleError:
        _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
        return _fixed_zero_result(
            outcome="unknown_completion",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=commanded_state,
            session_projection="unknown",
            reason_code=(
                "projection_unconfirmed"
                if server_context.session_store.fixed_zero_containment_ownership_matches(
                    session, reservation
                )
                else "completion_ownership_changed"
            ),
        )
    except Exception:  # noqa: BLE001 - never clear or recreate an unconfirmed token.
        reason: FixedZeroReason = (
            "projection_unconfirmed"
            if server_context.session_store.fixed_zero_containment_ownership_matches(
                session, reservation
            )
            else "completion_ownership_changed"
        )
        return _fixed_zero_result(
            outcome="unknown_completion",
            requested_session_id=expected_session_id,
            validated_session_id=expected_session_id,
            commanded_state=commanded_state,
            session_projection="unknown",
            reason_code=reason,
        )
    return _fixed_zero_result(
        outcome="command_state_returned",
        requested_session_id=expected_session_id,
        validated_session_id=expected_session_id,
        commanded_state=commanded_state,
        session_projection="recorded_in_memory",
        reason_code=None,
    )


def _run_reserved_driver_control(
    server_context: ServerContext,
    session: RoastSession,
    *,
    driver_command: Callable[[], RoasterState],
) -> RoastSession:
    """Run one reserved driver control command outside the store lock."""
    reservation = server_context.session_store.reserve_driver_command(
        session,
        kind="control",
    )
    try:
        driver_state = driver_command()
        try:
            return _complete_driver_control(
                server_context,
                session,
                reservation=reservation,
                driver_state=driver_state,
            )
        except SessionLifecycleError:
            _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
            raise
    except Exception:
        server_context.session_store.clear_driver_command_reservation(session, reservation)
        raise


def _begin_drop_observation_boundary(
    server_context: ServerContext, *, session: RoastSession
) -> object | None:
    """Begin the manual recording boundary after a drop driver dispatch."""
    if server_context.config.first_crack.event_authority != "manual":
        return None
    return server_context.first_crack_runtime.begin_observation_boundary(
        session, reason="beans_dropped"
    )


def _resolve_drop_observation_boundary(
    server_context: ServerContext,
    *,
    session: RoastSession,
    ticket: object | None,
    event: RoastEvent,
) -> None:
    """Resolve the manual drop boundary from the accepted event timestamp."""
    if server_context.config.first_crack.event_authority == "manual":
        server_context.first_crack_runtime.resolve_observation_boundary(
            session,
            ticket,
            cutoff_monotonic_seconds=session.monotonic_start + event.monotonic_seconds,
        )


def _freeze_drop_observation_boundary(
    server_context: ServerContext,
    *,
    session: RoastSession,
    ticket: object | None,
    reason: str,
) -> None:
    """Freeze an unresolved manual drop boundary after failed persistence."""
    if server_context.config.first_crack.event_authority == "manual":
        server_context.first_crack_runtime.freeze_observation_boundary(
            session, ticket, reason=reason
        )


def _stop_runtimes_after_drop(
    server_context: ServerContext, *, session: RoastSession, snapshot: RoastSession
) -> None:
    """Perform the ordinary post-drop runtime cleanup in its established order.

    Pending automatic windows are deliberately classified only after driver
    dispatch and accepted Drop persistence. Per #191, ``session`` is the live
    session so its accepted beans-dropped cutoff bounds classification, while
    ``snapshot`` is the saved lifecycle result used for cleanup ownership.
    This never adds a first-crack event to the causal session timeline.
    Repeated successful Drops retain this cleanup.
    """
    if server_context.config.first_crack.event_authority != "manual":
        server_context.first_crack_runtime.process_pending_windows_after_drop(
            session_store=server_context.session_store, session=session
        )
        server_context.first_crack_runtime.stop_for_session(snapshot.id, reason="beans dropped")
    if not snapshot.active:
        server_context.telemetry_sampler.stop_for_session(snapshot.id, reason="beans dropped")
        server_context.ambient_runtime.stop_for_session(snapshot.id, reason="beans dropped")


def _run_reserved_driver_drop(
    server_context: ServerContext,
    session: RoastSession,
) -> tuple[RoastEvent, RoastSession]:
    """Run a reserved drop command, preserving idempotent retry behavior."""
    drop_reservation = server_context.session_store.reserve_driver_drop(session)
    if drop_reservation.reservation is None:
        if drop_reservation.existing_event is None or drop_reservation.snapshot is None:
            raise ValueError("Drop reservation did not include the existing event snapshot.")
        return drop_reservation.existing_event, drop_reservation.snapshot

    reservation = drop_reservation.reservation
    observation_ticket: object | None = None
    driver_returned = False
    try:
        _revoke_manual_mark_publication_for_accepted_drop(server_context, session=session)
        if _manual_first_crack_writer_composition(server_context):
            # Establish pre-dispatch ownership only after confirming that the
            # exact reservation still owns admission. A driver return is
            # recorded separately below; an emergency may cancel the ordinary
            # token but cannot let recovery replay this owned call.
            server_context.session_store.admit_ordinary_driver_drop_dispatch(
                session, reservation=reservation
            )
        driver_state = server_context.roaster_driver.drop_beans()
        driver_returned = True
        if _manual_first_crack_writer_composition(server_context):
            server_context.session_store.record_verified_ordinary_driver_drop(
                session, reservation=reservation
            )
        observation_ticket = _begin_drop_observation_boundary(server_context, session=session)
        try:
            event, snapshot = server_context.session_store.complete_reserved_driver_drop_snapshot(
                session,
                reservation=reservation,
                heat_level_percent=driver_state.heat_level_percent,
                fan_level_percent=driver_state.fan_level_percent,
                cooling_on=driver_state.cooling_on,
            )
            _resolve_drop_observation_boundary(
                server_context,
                session=session,
                ticket=observation_ticket,
                event=event,
            )
            return event, snapshot
        except RecoveryPersistenceSupersededError as error:
            # The returned driver state and its canonical rows are already
            # verified. A global emergency displaced only live projection
            # while that owned storage turn settled, so it is not uncertain
            # in-driver work and must not dispatch a redundant emergency.
            raise SessionLifecycleError(
                "Drop command was accepted by the driver, but roast log history "
                "is incomplete or unsettled."
            ) from error
        except (EventLogRecordingError, SessionLifecycleError) as error:
            canonical_recording_failure = (
                driver_returned
                and _manual_first_crack_writer_composition(server_context)
                and isinstance(error, EventLogRecordingError)
            )
            if not canonical_recording_failure:
                _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
            _freeze_drop_observation_boundary(
                server_context,
                session=session,
                ticket=observation_ticket,
                reason="drop_recording_failed",
            )
            if canonical_recording_failure:
                raise SessionLifecycleError(
                    "Drop command was accepted by the driver, but roast log history "
                    "is incomplete or unsettled."
                ) from error
            raise
    except BaseException:
        if _manual_first_crack_writer_composition(server_context):
            server_context.session_store.retain_ordinary_driver_drop_unknown(
                session, reservation=reservation
            )
        server_context.session_store.clear_driver_command_reservation(session, reservation)
        raise


def _run_reserved_driver_start_cooling(
    server_context: ServerContext,
    session: RoastSession,
) -> tuple[RoastEvent, RoastSession]:
    """Run a reserved cooling-start command or return the existing event."""
    event_reservation = server_context.session_store.reserve_driver_start_cooling(session)
    if event_reservation.reservation is None:
        if event_reservation.existing_event is None or event_reservation.snapshot is None:
            raise ValueError(
                "Cooling-start reservation did not include the existing event snapshot."
            )
        return event_reservation.existing_event, event_reservation.snapshot

    reservation = event_reservation.reservation
    try:
        driver_state = server_context.roaster_driver.start_cooling()
        try:
            return server_context.session_store.complete_reserved_driver_start_cooling_snapshot(
                session,
                reservation=reservation,
                heat_level_percent=driver_state.heat_level_percent,
                fan_level_percent=driver_state.fan_level_percent,
                cooling_on=driver_state.cooling_on,
            )
        except EventLogRecordingError as error:
            settled = server_context.session_store.settle_known_torn_start_cooling(
                session,
                reservation=reservation,
                heat_level_percent=driver_state.heat_level_percent,
                fan_level_percent=driver_state.fan_level_percent,
                cooling_on=driver_state.cooling_on,
                connected=driver_state.connected,
            )
            if settled is not None:
                raise SessionLifecycleError(
                    "Cooling command completed, but roast log recording is unavailable."
                ) from error
            server_context.session_store.settle_unconfirmed_torn_start_cooling(
                session, reservation=reservation
            )
            _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
            raise SessionLifecycleError(
                "Cooling could not be confirmed after roast log recording failed."
            ) from error
        except SessionLifecycleError:
            _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
            raise
    except Exception:
        server_context.session_store.clear_driver_command_reservation(session, reservation)
        raise


def _run_reserved_driver_stop_cooling(
    server_context: ServerContext,
    session: RoastSession,
) -> tuple[RoastEvent, RoastSession]:
    """Run a reserved cooling-stop command."""
    reservation = server_context.session_store.reserve_driver_stop_cooling(session)
    try:
        driver_state = server_context.roaster_driver.stop_cooling()
        try:
            return server_context.session_store.complete_reserved_driver_stop_cooling_snapshot(
                session,
                reservation=reservation,
                heat_level_percent=driver_state.heat_level_percent,
                fan_level_percent=driver_state.fan_level_percent,
                cooling_on=driver_state.cooling_on,
            )
        except SessionLifecycleError:
            _fail_closed_after_stale_driver_command(server_context, reservation=reservation)
            raise
    except Exception:
        server_context.session_store.clear_driver_command_reservation(session, reservation)
        raise


def _run_reserved_driver_fault_recovery(
    server_context: ServerContext,
    session: RoastSession,
    *,
    reservation: DriverCommandReservation,
) -> tuple[RoastEvent, RoastSession]:
    """Run a pre-reserved stopped-fault command and retain the terminal fault state.

    The caller creates ``reservation`` while holding the lifecycle barrier, then
    releases that barrier before this function calls the driver. The store
    reservation fences new-session admission while allowing emergency stop to
    cancel it and reach a blocked driver command promptly.
    """
    command: Literal["drop", "start_cooling", "stop_cooling"] = "stop_cooling"
    driver_state: RoasterState | None = None
    observation_ticket: object | None = None
    known_torn_completion = False
    known_incomplete_completion = False
    try:
        if reservation.kind == "drop":
            command = "drop"
            _revoke_manual_mark_publication_for_accepted_drop(server_context, session=session)
            driver_state = server_context.roaster_driver.drop_beans()
        elif reservation.kind == "start_cooling":
            command = "start_cooling"
            driver_state = server_context.roaster_driver.start_cooling()
        elif reservation.kind == "stop_cooling":
            command = "stop_cooling"
            driver_state = server_context.roaster_driver.stop_cooling()
        else:
            raise SessionLifecycleError("Unsupported stopped-fault recovery command.")
        if command == "drop":
            observation_ticket = _begin_drop_observation_boundary(server_context, session=session)
        try:
            event, snapshot = (
                server_context.session_store.complete_reserved_driver_fault_recovery_snapshot(
                    session,
                    reservation=reservation,
                    heat_level_percent=driver_state.heat_level_percent,
                    fan_level_percent=driver_state.fan_level_percent,
                    cooling_on=driver_state.cooling_on,
                    connected=driver_state.connected,
                )
            )
        except EventLogRecordingError as error:
            if _manual_first_crack_writer_composition(server_context):
                settled = server_context.session_store.settle_known_incomplete_fault_recovery(
                    session,
                    reservation=reservation,
                    heat_level_percent=driver_state.heat_level_percent,
                    fan_level_percent=driver_state.fan_level_percent,
                    cooling_on=driver_state.cooling_on,
                    connected=driver_state.connected,
                )
                if settled is not None:
                    known_incomplete_completion = True
                    command_label = {
                        "drop": "Bean drop",
                        "start_cooling": "Cooling start",
                        "stop_cooling": "Cooling stop",
                    }[command]
                    raise SessionLifecycleError(
                        f"{command_label} command was accepted, but roast log recording "
                        "is unavailable."
                    ) from error
            if command == "start_cooling":
                settled = server_context.session_store.settle_known_torn_start_cooling(
                    session,
                    reservation=reservation,
                    heat_level_percent=driver_state.heat_level_percent,
                    fan_level_percent=driver_state.fan_level_percent,
                    cooling_on=driver_state.cooling_on,
                    connected=driver_state.connected,
                )
                if settled is not None:
                    known_torn_completion = True
                    raise SessionLifecycleError(
                        "Cooling command completed, but roast log recording is unavailable."
                    ) from error
            elif command == "stop_cooling":
                settled = server_context.session_store.settle_known_torn_stop_cooling_recovery(
                    session,
                    reservation=reservation,
                    heat_level_percent=driver_state.heat_level_percent,
                    fan_level_percent=driver_state.fan_level_percent,
                    cooling_on=driver_state.cooling_on,
                    connected=driver_state.connected,
                )
                if settled is not None:
                    known_torn_completion = True
                    raise SessionLifecycleError(
                        "Cooling command completed, but roast log recording is unavailable."
                    ) from error
            raise
        except SessionLifecycleError:
            raise
        if command == "drop":
            _resolve_drop_observation_boundary(
                server_context,
                session=session,
                ticket=observation_ticket,
                event=event,
            )
        return event, snapshot
    except RecoveryPersistenceSupersededError:
        # A later emergency already revoked this exact recovery reservation
        # while its owned writer turn was in flight.  Do not dispatch a second
        # protective command merely because the stale caller observes failure.
        raise
    except Exception:
        if known_torn_completion or known_incomplete_completion:
            raise
        if command == "drop":
            server_context.session_store.retain_fault_drop_ambiguity_if_unrecorded(session)
        _fail_closed_guarded_fault_recovery_command(
            server_context,
            session=session,
            reservation=reservation,
            command=command,
        )
        if driver_state is not None and (
            not driver_state.connected
            or (command in {"drop", "start_cooling"} and driver_state.fan_level_percent != 100)
        ):
            server_context.session_store.retain_fault_recovery_admission_block(session)
        if command == "drop":
            _freeze_drop_observation_boundary(
                server_context,
                session=session,
                ticket=observation_ticket,
                reason="fault_recovery_incomplete",
            )
        raise


def _fail_closed_guarded_fault_recovery_command(
    server_context: ServerContext,
    *,
    session: RoastSession,
    reservation: DriverCommandReservation,
    command: Literal["drop", "start_cooling", "stop_cooling"],
) -> None:
    """Fence uncertain guarded recovery after any driver or completion failure.

    This function reacquires the lifecycle barrier only after the original
    guarded driver call returns. It serializes the replacement emergency stop
    and its stored result with a concurrent direct emergency stop, while the
    store reservation fences new-session admission.
    """
    prepared_fault: tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None] | None = None
    attempted_payload = default_emergency_safety_payload()
    try:
        with server_context.lifecycle_barrier:
            attempted_payload = run_driver_emergency_stop(
                server_context,
                reason=f"guarded fault recovery {command} failed",
            )
            try:
                prepared_fault = _record_fault_with_owned_mock_drop_turn(
                    session,
                    server_context=server_context,
                    reason=f"guarded fault recovery {command} failed",
                    safety_payload=attempted_payload,
                    allow_stopped_latest=True,
                )
            except Exception:  # noqa: BLE001 - containment must survive result-recording failure.
                server_context.session_store.retain_fault_recovery_block(
                    session,
                    reservation=reservation,
                    safety_payload=attempted_payload,
                )
            else:
                if not has_verified_emergency_containment(safety_payload=attempted_payload):
                    server_context.session_store.retain_fault_recovery_block(
                        session,
                        reservation=reservation,
                        safety_payload=attempted_payload,
                    )
        if prepared_fault is not None:
            _commit_prepared_fault_recording(
                server_context,
                session=session,
                prepared_fault=prepared_fault,
            )
    except Exception:  # noqa: BLE001 - containment must survive result-recording failure.
        with suppress(Exception):
            server_context.session_store.retain_fault_recovery_block(
                session,
                reservation=reservation,
                safety_payload=attempted_payload,
            )
    finally:
        with server_context.lifecycle_barrier:
            server_context.session_store.clear_driver_command_reservation(session, reservation)
            server_context.session_store.clear_fault_recovery_in_flight(session, reservation)


def _retain_guarded_emergency_stop_block(
    server_context: ServerContext,
    *,
    session: RoastSession,
    safety_payload: dict[str, EventPayloadValue],
) -> tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None] | None:
    """Retain a pessimistic stopped-fault block after an invalid guarded e-stop.

    A guarded emergency-stop response that reports incomplete containment
    cannot be returned as success. Its truthful result is retained while the
    fallback preserves a hard admission block through recording failure.
    """
    reason = (
        "guarded emergency stop reported retained heat"
        if _guarded_emergency_stop_reported_retained_heat(safety_payload)
        else "guarded emergency stop reported incomplete containment"
    )
    try:
        prepared_fault = _record_fault_with_owned_mock_drop_turn(
            session,
            server_context=server_context,
            reason=reason,
            safety_payload=safety_payload,
            allow_stopped_latest=True,
        )
    except Exception:  # noqa: BLE001 - the conservative admission block must survive logging.
        server_context.session_store.retain_fault_block(
            session,
            safety_payload=safety_payload,
        )
        return None
    else:
        server_context.session_store.retain_fault_block(
            session,
            safety_payload=safety_payload,
        )
        return prepared_fault


def _guarded_emergency_stop_failure_message(
    safety_payload: dict[str, EventPayloadValue],
) -> str:
    """Describe the containment evidence missing from a guarded emergency stop."""
    if _guarded_emergency_stop_reported_retained_heat(safety_payload):
        return "Emergency stop reported retained heat."
    return "Emergency stop did not report complete emergency containment."


def _record_fault_with_owned_mock_drop_turn(
    session: RoastSession,
    *,
    server_context: ServerContext,
    reason: str,
    safety_payload: dict[str, EventPayloadValue],
    allow_stopped_latest: bool,
) -> tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None]:
    """Prepare a fault and return its caller-owned mock append turn when needed."""
    store = server_context.session_store
    if (
        type(server_context.roaster_driver) is MockRoasterDriver
        and server_context.config.roaster.driver == "mock"
        and store.has_pending_normal_drop_persistence(session)
    ):
        event, ticket = store.prepare_mock_drop_emergency_stop(
            session,
            reason=reason,
            safety_payload=safety_payload,
            allow_stopped_latest=allow_stopped_latest,
        )
        # The caller must commit this row only after releasing every lifecycle
        # barrier. It owns this ticket; no other caller drains it.
        return event, store.get_session_snapshot(session_id=session.id), ticket
    event, snapshot = store.emergency_stop_snapshot(
        session,
        reason=reason,
        safety_payload=safety_payload,
        allow_stopped_latest=allow_stopped_latest,
    )
    return event, snapshot, None


def _commit_prepared_fault_recording(
    server_context: ServerContext,
    *,
    session: RoastSession,
    prepared_fault: tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None],
) -> tuple[RoastEvent, RoastSession]:
    """Commit only the caller's prepared fault row outside lifecycle locks."""
    event, snapshot, ticket = prepared_fault
    if ticket is None:
        return event, snapshot
    return event, server_context.session_store.commit_prepared_mock_drop_emergency_stop(
        session,
        event=event,
        ticket=ticket,
    )


def _guarded_emergency_stop_reported_retained_heat(
    safety_payload: dict[str, EventPayloadValue],
) -> bool:
    """Return whether a guarded emergency-stop payload explicitly retains heat."""
    heat_level_percent = safety_payload.get("heat_level_percent")
    return (
        isinstance(heat_level_percent, int)
        and not isinstance(heat_level_percent, bool)
        and heat_level_percent != 0
    )


def _complete_driver_control(
    server_context: ServerContext,
    session: RoastSession,
    *,
    reservation: DriverCommandReservation,
    driver_state: RoasterState,
    cooling_on: bool | None = None,
) -> RoastSession:
    """Apply one reserved driver control result to the session."""
    return server_context.session_store.complete_reserved_driver_control_snapshot(
        session,
        reservation=reservation,
        heat_level_percent=driver_state.heat_level_percent,
        fan_level_percent=driver_state.fan_level_percent,
        cooling_on=driver_state.cooling_on if cooling_on is None else cooling_on,
    )


def _fail_closed_after_stale_driver_command(
    server_context: ServerContext,
    *,
    reservation: DriverCommandReservation,
) -> None:
    """Reapply driver safety when a completed command no longer owns the session."""
    with server_context.lifecycle_barrier:
        active_session = server_context.session_store.get_active_session()
        if active_session is not None and active_session.id != reservation.session_id:
            return
        run_driver_emergency_stop(
            server_context,
            reason="stale driver command after session state changed",
        )


def run_driver_emergency_stop(
    server_context: ServerContext,
    *,
    reason: str,
) -> dict[str, EventPayloadValue]:
    """Run driver-owned emergency stop before taking the session-store lock."""
    try:
        return server_context.roaster_driver.emergency_stop(reason=reason).as_event_payload()
    except Exception as exc:
        return default_emergency_safety_payload(
            driver=server_context.config.roaster.driver,
            driver_error=f"{type(exc).__name__}: {exc}",
        )


def _snapshot_session(
    server_context: ServerContext,
    *,
    session_id: str | None = None,
) -> RoastSession:
    """Return a locked deep-copied session snapshot."""
    try:
        return server_context.session_store.get_session_snapshot(session_id=session_id)
    except SessionLifecycleError as exc:
        raise ValueError(str(exc)) from exc


def _read_current_driver_state(server_context: ServerContext) -> RoasterState:
    """Read the configured driver state for MCP output and telemetry capture."""
    try:
        return server_context.roaster_driver.read_state()
    except Exception as exc:
        driver_name = server_context.config.roaster.driver
        raise RuntimeError(
            f"Could not read current roaster state from driver {driver_name}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _observe_cold_characterisation_roast_fan(
    server_context: ServerContext,
    *,
    session: RoastSession,
    driver_state: RoasterState,
) -> ColdCharacterisationObservation | None:
    """Return the eligible cold-session roast-fan observation without mutation."""
    if session.purpose == "roast":
        return None

    if not _is_latest_active_cold_session(server_context, session=session):
        return ColdCharacterisationObservation("not_eligible", None)

    driver = server_context.roaster_driver
    if not isinstance(driver, LifecycleEvidenceDriver):
        return ColdCharacterisationObservation("unsupported", None)
    try:
        raw = driver.read_lifecycle_evidence()
    except Exception:  # noqa: BLE001 - driver boundaries vary.
        return ColdCharacterisationObservation("unreadable", None)

    try:
        if type(raw) is not DriverLifecycleEvidence:
            return ColdCharacterisationObservation("malformed", None)
        if type(raw.driver) is not str or raw.driver != driver_state.driver:
            return ColdCharacterisationObservation("malformed", None)
        roast_fan_level_percent = raw.roast_fan_level_percent
        if type(roast_fan_level_percent) is not int or not 0 <= roast_fan_level_percent <= 100:
            return ColdCharacterisationObservation("malformed", None)
    except Exception:  # noqa: BLE001 - malformed driver properties vary.
        return ColdCharacterisationObservation("malformed", None)
    return ColdCharacterisationObservation("observed", roast_fan_level_percent)


def _reconcile_cold_characterisation_observation(
    server_context: ServerContext,
    *,
    session: RoastSession,
    observation_source_session_id: str,
    observation: ColdCharacterisationObservation | None,
) -> ColdCharacterisationObservation | None:
    """Keep a cold observation bound to the final returned session snapshot."""
    if session.purpose == "roast":
        return None

    if not _is_cold_observation_readmissible(
        server_context,
        session=session,
        observation_source_session_id=observation_source_session_id,
    ):
        return ColdCharacterisationObservation("not_eligible", None)
    return observation


def _is_latest_active_cold_session(
    server_context: ServerContext,
    *,
    session: RoastSession,
) -> bool:
    """Return whether the session is the latest active cold-characterisation session."""
    latest = server_context.session_store.get_latest_session()
    return (
        latest is not None
        and latest.id == session.id
        and latest.active
        and latest.purpose == "cold_characterisation"
    )


def _is_cold_observation_readmissible(
    server_context: ServerContext,
    *,
    session: RoastSession,
    observation_source_session_id: str,
) -> bool:
    """Return whether a cold observation still binds to the final session snapshot."""
    return (
        session.active
        and session.id == observation_source_session_id
        and _is_latest_active_cold_session(server_context, session=session)
    )


def _project_cold_temperature_for_session(
    server_context: ServerContext,
    *,
    session: RoastSession,
    driver_state: RoasterState,
) -> ColdTemperatureProjection | None:
    """Project cold temperature facts from the already-read driver snapshot."""
    if session.purpose == "roast":
        return None
    if not _is_latest_active_cold_session(server_context, session=session):
        return COLD_TEMPERATURE_NOT_ELIGIBLE
    return project_cold_temperature(driver_state)


def _reconcile_cold_temperature_projection(
    server_context: ServerContext,
    *,
    session: RoastSession,
    observation_source_session_id: str,
    projection: ColdTemperatureProjection | None,
) -> ColdTemperatureProjection | None:
    """Keep a cold temperature projection bound to the final returned session snapshot."""
    if session.purpose == "roast":
        return None
    # A final cold session with no initial projection (initial purpose was roast)
    # fails closed rather than returning a stale outer `None`.
    if projection is None or not _is_cold_observation_readmissible(
        server_context,
        session=session,
        observation_source_session_id=observation_source_session_id,
    ):
        return COLD_TEMPERATURE_NOT_ELIGIBLE
    return projection


def _record_polling_telemetry_for_active_session(
    server_context: ServerContext,
    *,
    session: RoastSession,
    requested_session_id: str | None,
    driver_state: RoasterState,
) -> RoastSession:
    """Append one driver-state telemetry sample when polling the active session."""
    snapshot = server_context.session_store.record_active_telemetry_sample(
        session_id=requested_session_id,
        bean_temp_c=driver_state.bean_temp_c,
        env_temp_c=driver_state.env_temp_c,
        heat_level_percent=driver_state.heat_level_percent,
        fan_level_percent=driver_state.fan_level_percent,
        cooling_on=driver_state.cooling_on,
    )
    return session if snapshot is None else snapshot


def _sample_active_session_telemetry(
    server_context: ServerContext,
    *,
    session_id: str,
) -> bool:
    """Append one autonomous telemetry sample for the owning active session."""
    active_session = server_context.session_store.get_active_session()
    if active_session is None or active_session.id != session_id:
        return False
    if server_context.session_store.finalisation_blocks_session(session_id):
        return False

    sampler_admitted = False
    if _manual_first_crack_writer_composition(server_context):
        admitted_session = server_context.session_store.admit_manual_mock_sampler(session_id)
        if admitted_session is None:
            return False
        active_session = admitted_session
        sampler_admitted = True

    try:
        driver_state = server_context.roaster_driver.read_state()
        device_state = _serialize_device_state(driver_state)
        snapshot = server_context.session_store.record_active_telemetry_sample(
            session_id=session_id,
            bean_temp_c=driver_state.bean_temp_c,
            env_temp_c=driver_state.env_temp_c,
            heat_level_percent=driver_state.heat_level_percent,
            fan_level_percent=driver_state.fan_level_percent,
            cooling_on=driver_state.cooling_on,
        )
        if snapshot is None:
            return False

        try:
            auto_t0_recorded = _process_auto_t0_for_active_session(
                server_context,
                session_id=session_id,
                device_state=device_state,
                defer_canonical_settlement=_manual_first_crack_writer_composition(server_context),
            )
        except EventLogRecordingError:
            # The driver read and telemetry sample are already valid.  A
            # recorder-only auto-charge failure latches its own incomplete
            # evidence in the store and must not be recast as a sampler or
            # driver failure that dispatches emergency containment.
            auto_t0_recorded = False
        if auto_t0_recorded:
            server_context.first_crack_runtime.discard_queued_windows_for_session(
                session_id,
                reason="Dropped queued pre-T0 detector windows after automatic T0.",
            )
        _process_first_crack_runtime_for_active_session(server_context, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - background safety path must catch all failures.
        _fault_active_session_after_sampler_failure(
            server_context,
            session=active_session,
            error=exc,
        )
        return False
    finally:
        if sampler_admitted:
            server_context.session_store.settle_manual_mock_sampler(active_session)

    active_session = server_context.session_store.get_active_session()
    return active_session is not None and active_session.id == session_id


def _fault_active_session_after_sampler_failure(
    server_context: ServerContext,
    *,
    session: RoastSession,
    error: BaseException,
) -> None:
    """Fail closed when autonomous sampling cannot safely continue."""
    reason = f"autonomous telemetry sampler failed: {type(error).__name__}: {error}"
    observation_ticket: object | None = None
    safety_payload = default_emergency_safety_payload()
    prepared_fault: tuple[RoastEvent, RoastSession, MockDropPersistenceTicket | None] | None = None
    verified_emergency_driver_effect = False
    try:
        with server_context.lifecycle_barrier:
            latest_session = server_context.session_store.get_latest_session()
            terminal = getattr(getattr(session, "finalisation", None), "status", None)
            final_evidence = getattr(
                getattr(session, "finalisation", None), "final_driver_evidence", None
            )
            safely_finalised = getattr(final_evidence, "outcome", None) == "read" and getattr(
                getattr(final_evidence, "evidence", None), "safe_zero", False
            )
            if (
                latest_session is session
                and not session.active
                and terminal in ("clean", "completed_not_clean")
                and safely_finalised
            ):
                return
            if _manual_first_crack_writer_composition(server_context):
                server_context.session_store.admit_manual_first_crack_endpoint(session)
            else:
                server_context.session_store.revoke_unconsumed_manual_first_crack_live_publication(
                    session
                )
            if server_context.config.first_crack.event_authority == "manual":
                revoke_publication = getattr(
                    server_context.first_crack_runtime,
                    "revoke_human_mark_publication",
                    None,
                )
                if revoke_publication is not None:
                    revoke_publication(session)
            safety_payload = run_driver_emergency_stop(server_context, reason=reason)
            verified_emergency_driver_effect = has_verified_emergency_containment(
                safety_payload=safety_payload
            )
            if server_context.config.first_crack.event_authority == "manual":
                observation_ticket = server_context.first_crack_runtime.begin_observation_boundary(
                    session, reason="autonomous_telemetry_sampler_failure"
                )
            prepared_fault = _record_fault_with_owned_mock_drop_turn(
                session,
                server_context=server_context,
                reason=reason,
                safety_payload=safety_payload,
                allow_stopped_latest=True,
            )
        try:
            event, snapshot = _commit_prepared_fault_recording(
                server_context,
                session=session,
                prepared_fault=prepared_fault,
            )
        except Exception:  # noqa: BLE001 - a deferred mock fault row can fail after containment.
            # This is deliberately narrower than the legacy preparation path
            # above. Only the P2 deferred mock-turn commit may expose a
            # storage error (including its private uncertain-tail signal).
            # Its in-memory fault was already published, but its history was
            # not recorded; preserve containment without replaying that row.
            if prepared_fault[2] is None:
                raise
            server_context.session_store.retain_active_fault_block(
                session, safety_payload=safety_payload
            )
            if server_context.config.first_crack.event_authority == "manual":
                server_context.first_crack_runtime.freeze_observation_boundary(
                    session, observation_ticket, reason="sampler_fault_recording_failed"
                )
            return
    except EventLogRecordingError:
        if (
            _manual_first_crack_writer_composition(server_context)
            and verified_emergency_driver_effect
        ):
            # A refused canonical row does not undo a verified driver stop.
            # Contain the live store without replaying the emergency or
            # claiming that its fault row became durable.
            server_context.session_store.retain_active_verified_emergency_containment(
                session, safety_payload=safety_payload
            )
            if server_context.config.first_crack.event_authority == "manual":
                server_context.first_crack_runtime.freeze_observation_boundary(
                    session, observation_ticket, reason="sampler_fault_recording_failed"
                )
            return
        raise
    except SessionLifecycleError:
        if server_context.session_store.has_pending_normal_drop_persistence(
            session
        ) or server_context.session_store.normal_drop_persistence_is_torn(session):
            server_context.session_store.retain_active_fault_block(
                session, safety_payload=safety_payload
            )
        if server_context.config.first_crack.event_authority == "manual":
            server_context.first_crack_runtime.freeze_observation_boundary(
                session, observation_ticket, reason="sampler_fault_recording_failed"
            )
        return
    if server_context.config.first_crack.event_authority == "manual":
        server_context.first_crack_runtime.resolve_observation_boundary(
            session,
            observation_ticket,
            cutoff_monotonic_seconds=session.monotonic_start + event.monotonic_seconds,
        )
    else:
        server_context.first_crack_runtime.stop_for_session(
            snapshot.id,
            reason="autonomous telemetry sampler failure",
        )
    server_context.ambient_runtime.stop_for_session(
        snapshot.id,
        reason="autonomous telemetry sampler failure",
    )


def _read_driver_lifecycle_evidence(server_context: ServerContext) -> DriverEvidenceRead:
    """Capture one read-only lifecycle evidence result without driver mutation."""
    captured_at_utc = datetime.now(UTC).isoformat()
    driver = server_context.roaster_driver
    if not isinstance(driver, LifecycleEvidenceDriver):
        return DriverEvidenceRead(captured_at_utc, "unsupported", None, None)
    try:
        raw = driver.read_lifecycle_evidence()
    except Exception as exc:  # noqa: BLE001 - driver boundaries vary.
        return DriverEvidenceRead(
            captured_at_utc, "unreadable", f"{type(exc).__name__}: {exc}", None
        )
    if not isinstance(cast(object, raw), DriverLifecycleEvidence):
        return DriverEvidenceRead(captured_at_utc, "malformed", "Unexpected evidence type.", None)
    try:
        dimensions = tuple(
            name
            for name, non_zero in (
                ("heat_level_percent", raw.heat_level_percent != 0),
                ("roast_fan_level_percent", raw.roast_fan_level_percent != 0),
                ("main_fan_level_percent", raw.main_fan_level_percent != 0),
                ("drum_motor_on", raw.drum_motor_on),
                ("cooling_motor_on", raw.cooling_motor_on),
                ("solenoid_open", raw.solenoid_open),
            )
            if non_zero
        )
        evidence = DriverCommandStateEvidence(
            driver=raw.driver,
            connected=raw.connected,
            command_streaming_required=raw.command_streaming_required,
            command_loop_running=raw.command_loop_running,
            serial_open=raw.serial_open,
            heat_level_percent=raw.heat_level_percent,
            roast_fan_level_percent=raw.roast_fan_level_percent,
            main_fan_level_percent=raw.main_fan_level_percent,
            drum_motor_on=raw.drum_motor_on,
            cooling_motor_on=raw.cooling_motor_on,
            solenoid_open=raw.solenoid_open,
            safe_zero=not dimensions,
            non_zero_dimensions=dimensions,
            command_send_attempts=raw.command_send_attempts,
            command_write_count=raw.command_write_count,
            last_command_write_size=raw.last_command_write_size,
            command_loop_error_count=raw.command_loop_error_count,
            status_packet_count=raw.status_packet_count,
            status_read_error_count=raw.status_read_error_count,
        )
    except Exception as exc:  # noqa: BLE001 - malformed driver properties vary.
        return DriverEvidenceRead(
            captured_at_utc, "malformed", f"{type(exc).__name__}: {exc}", None
        )
    return DriverEvidenceRead(captured_at_utc, "read", None, evidence)


def _evidence_admission_rejection(
    evidence: DriverEvidenceRead,
) -> FinalisationRejectionReason | None:
    """Return the ordered admission rejection implied by evidence."""
    if evidence.outcome == "unsupported":
        return "driver_lifecycle_evidence_unsupported"
    if evidence.outcome == "unreadable":
        return "driver_state_unreadable"
    if evidence.outcome == "malformed":
        return "driver_state_malformed"
    current = evidence.evidence
    if current is None:
        return "driver_state_malformed"
    if not current.connected or (
        current.command_streaming_required
        and (not current.command_loop_running or not current.serial_open)
    ):
        return "driver_not_connected"
    if not current.safe_zero:
        return "driver_state_not_safe_zero"
    return None


def _initial_finalisation_result(
    session: RoastSession,
    generation: int,
    evidence: DriverEvidenceRead,
    server_context: ServerContext,
) -> SessionFinalisationResult:
    """Create the retained result after successful admission."""
    now = datetime.now(UTC).isoformat()
    elapsed = session.elapsed_monotonic_seconds(time.monotonic)
    stages = tuple(
        FinalisationStageResult(stage=name)
        for name in ("telemetry_sampler", "first_crack_runtime", "recording", "driver_disconnect")
    )
    return SessionFinalisationResult(
        session_id=session.id,
        session_purpose=session.purpose,
        status="partial",
        clean=False,
        rejection_reason=None,
        abort_reason=None,
        retained=True,
        reservation_generation=generation,
        attempt_number=0,
        recovered_after_failure=False,
        first_started_at_utc=now,
        first_started_session_elapsed_seconds=elapsed,
        last_ended_at_utc=None,
        last_ended_session_elapsed_seconds=None,
        emergency_stop_ordering="not_reached",
        stages=cast(FinalisationStages, stages),
        failures=(),
        admission_driver_evidence=evidence,
        pre_disconnect_driver_evidence=None,
        final_driver_evidence=None,
        sampler=None,
        pre_finalisation_first_crack_status=_serialize_first_crack_status(
            session,
            config=server_context.config,
            first_crack_runtime=server_context.first_crack_runtime.snapshot(),
        ),
        first_crack_runtime=None,
        recording=None,
        disconnect=DisconnectEvidence(),
        session_active_after=True,
        session_phase_after=session.phase,
    )


def _with_stage(
    result: SessionFinalisationResult,
    index: int,
    status: FinalisationStageStatus,
    detail: str | None = None,
) -> SessionFinalisationResult:
    """Return a result with one stage replaced without disturbing prior stages."""
    stages = list(result.stages)
    stage = stages[index]
    stages[index] = dataclasses.replace(
        stage,
        status=status,
        completed_at_utc=datetime.now(UTC).isoformat()
        if status in ("completed", "not_applicable", "failed")
        else None,
        completed_in_attempt=result.attempt_number
        if status in ("completed", "not_applicable", "failed")
        else None,
        detail=detail,
    )
    return dataclasses.replace(
        result,
        stages=cast(FinalisationStages, tuple(stages)),
    )


def _append_finalisation_failure(
    result: SessionFinalisationResult,
    stage: FinalisationStageName | Literal["revalidation"],
    code: str,
    message: str,
) -> SessionFinalisationResult:
    """Append immutable failure evidence without changing recovery state."""
    failure = FinalisationFailure(
        stage, code, message, result.attempt_number, datetime.now(UTC).isoformat()
    )
    return dataclasses.replace(result, failures=(*result.failures, failure))


def _recording_evidence(
    plan: RecordingArtifactPlan | None, recorder: object | None, *, expected: bool = False
) -> RecordingFinalisationEvidence:
    """Use only filesystem metadata to report required recording artifacts."""
    if plan is None:
        return RecordingFinalisationEvidence(
            expected,
            "failed" if expected else "not_configured",
            "Recording plan was unavailable." if expected else None,
            (),
        )
    minimum_sizes = {
        "primary_wav": 45,
        "recording_sidecar": 1,
        "annotation_session_sidecar": 1,
        "additional_wav": 45,
    }
    items: list[RecordingArtifact] = []
    for role, path in (
        ("primary_wav", plan.primary_wav),
        ("recording_sidecar", plan.recording_sidecar),
        ("annotation_session_sidecar", plan.annotation_session_sidecar),
    ):
        try:
            exists = path.is_file()
            size = path.stat().st_size if exists else None
        except OSError:
            exists, size = False, None
        items.append(
            RecordingArtifact(
                cast(RecordingArtifactRole, role),
                path.name,
                str(path),
                exists,
                size,
            )
        )
    for path in plan.additional_wavs:
        try:
            exists = path.is_file()
            size = path.stat().st_size if exists else None
        except OSError:
            exists, size = False, None
        items.append(
            RecordingArtifact(
                "additional_wav",
                path.name,
                str(path),
                exists,
                size,
            )
        )
    if recorder is None or getattr(recorder, "started_monotonic_seconds", None) is None:
        return RecordingFinalisationEvidence(
            True, "not_started", "Recorder did not start.", tuple(items)
        )
    complete = all(
        item.exists and item.size_bytes is not None and item.size_bytes >= minimum_sizes[item.role]
        for item in items
    )
    return RecordingFinalisationEvidence(
        True,
        "finalised" if complete else "failed",
        None if complete else "Required recording artifact is missing or empty.",
        tuple(items),
    )


def _finalise_cold_characterisation_session(
    server_context: ServerContext, session_id: str
) -> SessionFinalisationResult:
    """Run cold-session teardown without invoking any roaster control operation."""
    session, rejection, generation = server_context.session_store.begin_finalisation(session_id)
    if session is None or rejection is not None:
        existing = None if session is None else session.finalisation
        if existing is not None and getattr(existing, "status", None) == "aborted":
            assert session is not None
            return cast(
                SessionFinalisationResult, server_context.session_store.copy_finalisation(session)
            )
        purpose = None if session is None else session.purpose
        return _rejected_finalisation(
            session_id, purpose, cast(FinalisationRejectionReason, rejection), session=session
        )
    existing = session.finalisation
    if existing is not None:
        result = cast(SessionFinalisationResult, existing)
        if result.status in ("clean", "completed_not_clean", "aborted"):
            server_context.session_store.finish_finalisation_invocation(session)
            return cast(
                SessionFinalisationResult, server_context.session_store.copy_finalisation(session)
            )
    else:
        evidence = _read_driver_lifecycle_evidence(server_context)
        evidence_rejection = _evidence_admission_rejection(evidence)
        if evidence_rejection is not None:
            server_context.session_store.abandon_finalisation_admission(session)
            if session.active and session.purpose == "cold_characterisation":
                server_context.telemetry_sampler.start_for_session(session.id)
            return _rejected_finalisation(
                session.id, session.purpose, evidence_rejection, session=session
            )
        try:
            result = _initial_finalisation_result(
                session, cast(int, generation), evidence, server_context
            )
        except Exception:
            server_context.session_store.abandon_finalisation_admission(session)
            if session.active and session.purpose == "cold_characterisation":
                server_context.telemetry_sampler.start_for_session(session.id)
            raise
        server_context.session_store.attach_finalisation(session, result)
    try:
        aborted = server_context.session_store.abort_finalisation_if_invalid(
            session, result.reservation_generation
        )
        if aborted is not None:
            return cast(SessionFinalisationResult, aborted)
        result = dataclasses.replace(result, attempt_number=result.attempt_number + 1)
        if result.stages[0].status != "completed":
            sampler = server_context.telemetry_sampler.stop_and_join_for_finalisation(session.id)
            result = dataclasses.replace(result, sampler=sampler)
            if sampler.thread_alive_after_join:
                result = _append_finalisation_failure(
                    result,
                    "telemetry_sampler",
                    "join_timeout",
                    "Telemetry sampler is still running.",
                )
                result = _with_stage(result, 0, "incomplete", "Sampler join timed out.")
                return _persist_partial(session, result, server_context)
            result = _with_stage(result, 0, "completed")
            result = cast(
                SessionFinalisationResult,
                server_context.session_store.persist_finalisation(session, result),
            )
        if result.stages[1].status in ("pending", "incomplete"):
            outcome, error, capture_running = (
                server_context.first_crack_runtime.finalise_for_session(session.id)
            )
            first_crack = FirstCrackRuntimeFinalisationEvidence(
                outcome,
                error,
                capture_running,
                _serialize_first_crack_status(
                    session,
                    config=server_context.config,
                    first_crack_runtime=server_context.first_crack_runtime.snapshot(),
                ),
            )
            result = dataclasses.replace(result, first_crack_runtime=first_crack)
            if outcome == "capture_still_running" or (outcome == "stop_failed" and capture_running):
                result = _append_finalisation_failure(
                    result, "first_crack_runtime", outcome, error or outcome
                )
                result = _with_stage(result, 1, "incomplete", error or outcome)
                return _persist_partial(session, result, server_context)
            if outcome == "stop_failed":
                result = _append_finalisation_failure(
                    result, "first_crack_runtime", outcome, error or outcome
                )
                result = _with_stage(result, 1, "failed", error or outcome)
            else:
                result = _with_stage(
                    result, 1, "not_applicable" if outcome == "not_active" else "completed"
                )
            result = cast(
                SessionFinalisationResult,
                server_context.session_store.persist_finalisation(session, result),
            )
        if result.stages[2].status == "pending":
            recorder, plan = server_context.first_crack_runtime.recording_for_session(session.id)
            recording = _recording_evidence(
                plan,
                recorder,
                expected=(
                    server_context.config.recording.enabled
                    and server_context.config.recording.autocapture
                    and server_context.config.first_crack.mode == "audio"
                ),
            )
            result = dataclasses.replace(result, recording=recording)
            if recording.outcome in ("not_configured", "finalised"):
                result = _with_stage(
                    result,
                    2,
                    "not_applicable" if recording.outcome == "not_configured" else "completed",
                )
            else:
                result = _append_finalisation_failure(
                    result, "recording", recording.outcome, recording.reason or "Recording failed."
                )
                result = _with_stage(result, 2, "failed", recording.reason)
            result = cast(
                SessionFinalisationResult,
                server_context.session_store.persist_finalisation(session, result),
            )
        return _disconnect_finalisation(server_context, session, result)
    finally:
        server_context.session_store.finish_finalisation_invocation(session)


def _persist_partial(
    session: RoastSession, result: SessionFinalisationResult, server_context: ServerContext
) -> SessionFinalisationResult:
    """Persist a resumable result without releasing the reserved session."""
    result = dataclasses.replace(
        result,
        status="partial",
        clean=False,
        last_ended_at_utc=datetime.now(UTC).isoformat(),
        last_ended_session_elapsed_seconds=session.elapsed_monotonic_seconds(time.monotonic),
        session_active_after=True,
        session_phase_after=session.phase,
    )
    return cast(
        SessionFinalisationResult,
        server_context.session_store.persist_finalisation(session, result),
    )


def _disconnect_finalisation(
    server_context: ServerContext, session: RoastSession, result: SessionFinalisationResult
) -> SessionFinalisationResult:
    """Commit one ordered disconnect attempt and confirm it by evidence read."""
    with server_context.lifecycle_barrier:
        aborted = server_context.session_store.abort_finalisation_if_invalid(
            session, result.reservation_generation
        )
        if aborted is not None:
            return cast(SessionFinalisationResult, aborted)
        pre = _read_driver_lifecycle_evidence(server_context)
        if result.disconnect.attempt_count == 0:
            rejection = _evidence_admission_rejection(pre)
            if rejection is not None:
                result = _append_finalisation_failure(
                    result, "revalidation", rejection, "Pre-disconnect revalidation failed."
                )
                return _persist_partial(
                    session,
                    dataclasses.replace(result, pre_disconnect_driver_evidence=pre),
                    server_context,
                )
        now = datetime.now(UTC).isoformat()
        disconnect = dataclasses.replace(
            result.disconnect,
            attempt_count=result.disconnect.attempt_count + 1,
            first_attempted_at_utc=result.disconnect.first_attempted_at_utc or now,
            last_attempted_at_utc=now,
        )
        result = dataclasses.replace(
            result,
            pre_disconnect_driver_evidence=pre,
            disconnect=disconnect,
            emergency_stop_ordering="finalisation_committed_first",
        )
        result = cast(
            SessionFinalisationResult,
            server_context.session_store.persist_finalisation(session, result),
        )
        if result.status == "aborted":
            return result
        error: str | None = None
        try:
            server_context.roaster_driver.disconnect()
        except Exception as exc:  # noqa: BLE001 - confirmation decides retry.
            error = f"{type(exc).__name__}: {exc}"
        final = _read_driver_lifecycle_evidence(server_context)
        evidence = final.evidence
        confirmed = (
            final.outcome == "read"
            and evidence is not None
            and not evidence.connected
            and (
                not evidence.command_streaming_required
                or (not evidence.command_loop_running and not evidence.serial_open)
            )
            and error is None
        )
        disconnect = dataclasses.replace(
            disconnect,
            last_returned_without_error=error is None,
            last_error=error,
            connected_false_confirmed=bool(evidence is not None and not evidence.connected),
            command_loop_stopped="not_applicable"
            if evidence is not None and not evidence.command_streaming_required
            else (
                "confirmed"
                if evidence is not None and not evidence.command_loop_running
                else "not_confirmed"
            ),
            serial_closed="not_applicable"
            if evidence is not None and not evidence.command_streaming_required
            else (
                "confirmed"
                if evidence is not None and not evidence.serial_open
                else "not_confirmed"
            ),
        )
        result = dataclasses.replace(result, final_driver_evidence=final, disconnect=disconnect)
        if not confirmed:
            result = _append_finalisation_failure(
                result,
                "driver_disconnect",
                "disconnect_not_confirmed",
                error or "Disconnect state was not confirmed.",
            )
            return cast(
                SessionFinalisationResult,
                server_context.session_store.persist_finalisation(
                    session,
                    dataclasses.replace(
                        result,
                        status="disconnect_indeterminate",
                        clean=False,
                        last_ended_at_utc=datetime.now(UTC).isoformat(),
                        last_ended_session_elapsed_seconds=session.elapsed_monotonic_seconds(
                            time.monotonic
                        ),
                    ),
                ),
            )
        if final.evidence is not None and not final.evidence.safe_zero:
            result = _append_finalisation_failure(
                result,
                "driver_disconnect",
                "final_driver_not_safe_zero",
                "Final driver evidence was not safe zero.",
            )
            result = _with_stage(result, 3, "failed", "Final driver evidence was not safe zero.")
        else:
            result = _with_stage(result, 3, "completed")
        clean = (
            all(stage.status in ("completed", "not_applicable") for stage in result.stages)
            and all(stage.status != "failed" for stage in result.stages)
            and final.evidence is not None
            and final.evidence.safe_zero
        )
        final_result = dataclasses.replace(
            result,
            status="clean" if clean else "completed_not_clean",
            clean=clean,
            recovered_after_failure=clean and bool(result.failures),
            last_ended_at_utc=datetime.now(UTC).isoformat(),
            last_ended_session_elapsed_seconds=session.elapsed_monotonic_seconds(time.monotonic),
            session_active_after=False,
            session_phase_after=session.phase,
        )
        final_result = cast(
            SessionFinalisationResult,
            server_context.session_store.finish_finalisation_terminal(session, final_result),
        )
    server_context.ambient_runtime.stop_for_session(
        session.id, reason="cold characterisation finalised"
    )
    return final_result


def _rejected_finalisation(
    session_id: str,
    purpose: SessionPurpose | None,
    reason: FinalisationRejectionReason,
    *,
    session: RoastSession | None = None,
) -> SessionFinalisationResult:
    """Return a non-retained rejection without running any teardown stage."""
    stages = tuple(
        FinalisationStageResult(stage=name, status="skipped")
        for name in ("telemetry_sampler", "first_crack_runtime", "recording", "driver_disconnect")
    )
    return SessionFinalisationResult(
        session_id=session_id,
        session_purpose=purpose,
        status="rejected",
        clean=False,
        rejection_reason=reason,
        abort_reason=None,
        retained=False,
        reservation_generation=None,
        attempt_number=0,
        recovered_after_failure=False,
        first_started_at_utc=None,
        first_started_session_elapsed_seconds=None,
        last_ended_at_utc=None,
        last_ended_session_elapsed_seconds=None,
        emergency_stop_ordering="not_reached",
        stages=cast(FinalisationStages, stages),
        failures=(),
        admission_driver_evidence=None,
        pre_disconnect_driver_evidence=None,
        final_driver_evidence=None,
        sampler=None,
        pre_finalisation_first_crack_status=None,
        first_crack_runtime=None,
        recording=None,
        disconnect=DisconnectEvidence(),
        session_active_after=False if session is None else session.active,
        session_phase_after=None if session is None else session.phase,
    )


def _serialize_session_state(
    session: RoastSession,
    *,
    config: AppConfig,
    monotonic_now: Callable[[], float],
    device_state: RoasterDeviceState | None = None,
    first_crack_runtime: FirstCrackRuntimeSnapshot | None = None,
    first_crack_observation: ObservationSnapshot | None = None,
    ambient_runtime: AmbientRuntimeSnapshot | None = None,
    cold_characterisation_observation: ColdCharacterisationObservation | None = None,
    cold_temperature_projection: ColdTemperatureProjection | None = None,
) -> RoastSessionState:
    """Convert one in-memory session into an MCP-safe snapshot."""
    if session.active:
        sampled_monotonic_now = monotonic_now()

        def projection_clock() -> float:
            """Return this active projection's single sampled clock value."""
            return sampled_monotonic_now

    else:
        projection_clock = monotonic_now
    metrics = compute_roast_metrics(
        session,
        monotonic_now=projection_clock,
        ror_window_seconds=config.session.ror_window_seconds,
        ror_min_sample_seconds=config.session.ror_min_sample_seconds,
    )
    return RoastSessionState(
        session_id=session.id,
        active=session.active,
        phase=session.phase,
        created_at_utc=session.created_at_utc.isoformat(),
        stopped_at_utc=_iso_or_none(session.stopped_at_utc),
        elapsed_monotonic_seconds=round(session.elapsed_monotonic_seconds(projection_clock), 3),
        heat_level_percent=session.heat_level_percent,
        fan_level_percent=session.fan_level_percent,
        cooling_on=session.cooling_on,
        beans_added_at_utc=_iso_or_none(session.beans_added_at_utc),
        first_crack_at_utc=_iso_or_none(session.first_crack_at_utc),
        beans_dropped_at_utc=_iso_or_none(session.beans_dropped_at_utc),
        cooling_started_at_utc=_iso_or_none(session.cooling_started_at_utc),
        cooling_stopped_at_utc=_iso_or_none(session.cooling_stopped_at_utc),
        faulted_at_utc=_iso_or_none(session.faulted_at_utc),
        beans_added_monotonic_seconds=session.beans_added_monotonic_seconds,
        first_crack_monotonic_seconds=session.first_crack_monotonic_seconds,
        beans_dropped_monotonic_seconds=session.beans_dropped_monotonic_seconds,
        cooling_started_monotonic_seconds=session.cooling_started_monotonic_seconds,
        cooling_stopped_monotonic_seconds=session.cooling_stopped_monotonic_seconds,
        faulted_monotonic_seconds=session.faulted_monotonic_seconds,
        roast_elapsed_seconds=metrics.roast_elapsed_seconds,
        development_time_seconds=metrics.development_time_seconds,
        development_percent=metrics.development_percent,
        bean_temp_delta_60s_c=metrics.bean_temp_delta_60s_c,
        env_temp_delta_60s_c=metrics.env_temp_delta_60s_c,
        bean_ror_c_per_min=metrics.bean_ror_c_per_min,
        env_ror_c_per_min=metrics.env_ror_c_per_min,
        device_state=device_state,
        t0_status=_serialize_t0_status(session, config=config),
        first_crack_status=_serialize_first_crack_status(
            session,
            config=config,
            first_crack_runtime=first_crack_runtime,
        ),
        ambient_status=_serialize_ambient_status(
            config=config,
            ambient_runtime=ambient_runtime,
        ),
        events=tuple(_serialize_event(event) for event in session.event_timeline),
        log_dir=str(session.log_writer.log_dir.resolve())
        if session.log_writer is not None
        else None,
        session_purpose=session.purpose,
        cold_characterisation_observation=cold_characterisation_observation,
        cold_temperature_projection=cold_temperature_projection,
        first_crack_observation=first_crack_observation,
    )


def _serialize_device_state(driver_state: RoasterState) -> RoasterDeviceState:
    """Convert normalized driver state into an MCP-safe device snapshot."""
    return RoasterDeviceState(
        driver=driver_state.driver,
        connected=driver_state.connected,
        bean_temp_c=driver_state.bean_temp_c,
        env_temp_c=driver_state.env_temp_c,
        heat_level_percent=driver_state.heat_level_percent,
        fan_level_percent=driver_state.fan_level_percent,
        cooling_on=driver_state.cooling_on,
        raw_vendor_data=dict(driver_state.raw_vendor_data),
    )


def _serialize_first_crack_status(
    session: RoastSession,
    *,
    config: AppConfig,
    first_crack_runtime: FirstCrackRuntimeSnapshot | None = None,
) -> FirstCrackStatus:
    """Derive first-crack status from config and the session timeline."""
    if session.first_crack_at_utc is not None:
        runtime = _runtime_metrics(
            session_id=session.id,
            first_crack_runtime=first_crack_runtime,
        )
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="detected",
            detected_at_utc=session.first_crack_at_utc.isoformat(),
            detected_monotonic_seconds=session.first_crack_monotonic_seconds,
            allow_manual_override=config.first_crack.allow_manual_override,
            audio_running=runtime.audio_running,
            queued_window_count=runtime.queued_window_count,
            emitted_window_count=runtime.emitted_window_count,
            dropped_window_count=runtime.dropped_window_count,
            processed_window_count=runtime.processed_window_count,
            mic_peak_dbfs=runtime.mic_peak_dbfs,
            mic_rms_dbfs=runtime.mic_rms_dbfs,
            overflow_count_last_minute=runtime.overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=runtime.estimated_lost_audio_ms_last_minute,
            total_overflow_count=runtime.total_overflow_count,
            max_consecutive_overflow_count=runtime.max_consecutive_overflow_count,
            last_inference_duration_ms=runtime.last_inference_duration_ms,
            max_inference_duration_ms=runtime.max_inference_duration_ms,
            inference_overrun_count=runtime.inference_overrun_count,
        )
    if session.faulted_at_utc is not None:
        runtime = _runtime_metrics(
            session_id=session.id,
            first_crack_runtime=first_crack_runtime,
        )
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="faulted",
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason="Session faulted before first crack was recorded.",
            audio_running=runtime.audio_running,
            queued_window_count=runtime.queued_window_count,
            emitted_window_count=runtime.emitted_window_count,
            dropped_window_count=runtime.dropped_window_count,
            processed_window_count=runtime.processed_window_count,
            mic_peak_dbfs=runtime.mic_peak_dbfs,
            mic_rms_dbfs=runtime.mic_rms_dbfs,
            overflow_count_last_minute=runtime.overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=runtime.estimated_lost_audio_ms_last_minute,
            total_overflow_count=runtime.total_overflow_count,
            max_consecutive_overflow_count=runtime.max_consecutive_overflow_count,
            last_inference_duration_ms=runtime.last_inference_duration_ms,
            max_inference_duration_ms=runtime.max_inference_duration_ms,
            inference_overrun_count=runtime.inference_overrun_count,
        )
    if config.first_crack.mode == "disabled":
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="disabled",
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason="Automatic first-crack detection is disabled by configuration.",
        )
    if config.first_crack.mode == "manual":
        if not config.first_crack.allow_manual_override:
            return FirstCrackStatus(
                mode=config.first_crack.mode,
                status="unavailable",
                detected_at_utc=None,
                detected_monotonic_seconds=None,
                allow_manual_override=config.first_crack.allow_manual_override,
                reason=("Manual first-crack mode is configured, but manual override is disabled."),
            )
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="manual",
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason="Waiting for explicit mark_first_crack override.",
        )
    if (
        config.first_crack.mode == "audio"
        and first_crack_runtime is not None
        and first_crack_runtime.active_session_id == session.id
        and first_crack_runtime.status in {"faulted", "unavailable"}
    ):
        runtime = _runtime_metrics(
            session_id=session.id,
            first_crack_runtime=first_crack_runtime,
        )
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status=first_crack_runtime.status,
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason=first_crack_runtime.reason,
            audio_running=runtime.audio_running,
            queued_window_count=runtime.queued_window_count,
            emitted_window_count=runtime.emitted_window_count,
            dropped_window_count=runtime.dropped_window_count,
            processed_window_count=runtime.processed_window_count,
            mic_peak_dbfs=runtime.mic_peak_dbfs,
            mic_rms_dbfs=runtime.mic_rms_dbfs,
            overflow_count_last_minute=runtime.overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=runtime.estimated_lost_audio_ms_last_minute,
            total_overflow_count=runtime.total_overflow_count,
            max_consecutive_overflow_count=runtime.max_consecutive_overflow_count,
            last_inference_duration_ms=runtime.last_inference_duration_ms,
            max_inference_duration_ms=runtime.max_inference_duration_ms,
            inference_overrun_count=runtime.inference_overrun_count,
        )
    reason = "Audio first-crack detection has not recorded first crack for this session."
    if (
        config.first_crack.mode == "audio"
        and first_crack_runtime is not None
        and first_crack_runtime.active_session_id == session.id
        and first_crack_runtime.reason is not None
    ):
        reason = first_crack_runtime.reason
    runtime = _runtime_metrics(
        session_id=session.id,
        first_crack_runtime=first_crack_runtime,
    )
    return FirstCrackStatus(
        mode=config.first_crack.mode,
        status="pending",
        detected_at_utc=None,
        detected_monotonic_seconds=None,
        allow_manual_override=config.first_crack.allow_manual_override,
        reason=reason,
        audio_running=runtime.audio_running,
        queued_window_count=runtime.queued_window_count,
        emitted_window_count=runtime.emitted_window_count,
        dropped_window_count=runtime.dropped_window_count,
        processed_window_count=runtime.processed_window_count,
        mic_peak_dbfs=runtime.mic_peak_dbfs,
        mic_rms_dbfs=runtime.mic_rms_dbfs,
        overflow_count_last_minute=runtime.overflow_count_last_minute,
        estimated_lost_audio_ms_last_minute=runtime.estimated_lost_audio_ms_last_minute,
        total_overflow_count=runtime.total_overflow_count,
        max_consecutive_overflow_count=runtime.max_consecutive_overflow_count,
        last_inference_duration_ms=runtime.last_inference_duration_ms,
        max_inference_duration_ms=runtime.max_inference_duration_ms,
        inference_overrun_count=runtime.inference_overrun_count,
    )


def _runtime_metrics(
    *,
    session_id: str,
    first_crack_runtime: FirstCrackRuntimeSnapshot | None,
) -> FirstCrackRuntimeSnapshot:
    """Return runtime metrics or an empty disabled-style snapshot."""
    if first_crack_runtime is not None and first_crack_runtime.active_session_id == session_id:
        return first_crack_runtime
    return FirstCrackRuntimeSnapshot(
        status="disabled",
        active_session_id=None,
        active=False,
    )


def _serialize_ambient_status(
    *,
    config: AppConfig,
    ambient_runtime: AmbientRuntimeSnapshot | None,
) -> AmbientStatus:
    """Derive ambient sensor status from config and the runtime snapshot.

    Simpler state machine than first-crack's: `disabled` (config off) →
    `unavailable` (no runtime snapshot yet, or the probe could not be
    prepared/read) → `ok` (a live cached reading is available). Read-only
    corpus metadata, so there is no session-timeline-derived "detected" state
    to branch on.
    """
    if config.ambient.mode == "disabled":
        return AmbientStatus(
            mode=config.ambient.mode,
            status="disabled",
            reason="Ambient sensing is disabled by configuration.",
        )
    if ambient_runtime is None:
        return AmbientStatus(
            mode=config.ambient.mode,
            status="unavailable",
            reason="Ambient sensing has not started.",
        )
    return AmbientStatus(
        mode=config.ambient.mode,
        status=ambient_runtime.status,
        reason=ambient_runtime.reason,
        ambient_running=ambient_runtime.ambient_running,
        temperature_c=ambient_runtime.temperature_c,
        humidity_percent=ambient_runtime.humidity_percent,
        pressure_hpa=ambient_runtime.pressure_hpa,
        last_reading_monotonic_seconds=ambient_runtime.last_reading_monotonic_seconds,
    )


def _serialize_t0_status(session: RoastSession, *, config: AppConfig) -> T0Status:
    """Derive automatic T0 status from config and the session timeline."""
    beans_added_event = next(
        (event for event in session.event_timeline if event.kind == "beans_added"),
        None,
    )
    detected_temp = None
    if beans_added_event is not None:
        payload_value = beans_added_event.payload.get("detected_bean_temperature_c")
        if isinstance(payload_value, (int, float)) and not isinstance(payload_value, bool):
            detected_temp = float(payload_value)
    if session.beans_added_at_utc is not None:
        return T0Status(
            auto_detection_enabled=config.session.auto_t0_detection_enabled,
            status="detected",
            charge_temperature_c=_event_or_session_charge_temperature(
                beans_added_event,
                session,
            ),
            current_drop_c=session.auto_t0_current_drop_c,
            drop_threshold_c=config.session.auto_t0_drop_threshold_c,
            detected_bean_temperature_c=detected_temp,
        )
    if not config.session.auto_t0_detection_enabled:
        return T0Status(
            auto_detection_enabled=False,
            status="disabled",
            charge_temperature_c=session.auto_t0_charge_temperature_c,
            current_drop_c=session.auto_t0_current_drop_c,
            drop_threshold_c=config.session.auto_t0_drop_threshold_c,
            detected_bean_temperature_c=None,
            reason="Automatic T0 detection is disabled by configuration.",
        )
    if session.phase != "pre_roast":
        return T0Status(
            auto_detection_enabled=True,
            status="unavailable",
            charge_temperature_c=session.auto_t0_charge_temperature_c,
            current_drop_c=session.auto_t0_current_drop_c,
            drop_threshold_c=config.session.auto_t0_drop_threshold_c,
            detected_bean_temperature_c=None,
            reason=f"Automatic T0 is unavailable while phase is {session.phase}.",
        )
    reason = "Waiting for bean temperature to drop from tracked charge temperature."
    if session.auto_t0_preheat_sample_count == 0:
        reason = "Waiting for a valid preheat bean-temperature reading."
    elif session.auto_t0_preheat_sample_count == 1:
        reason = "Waiting for a second valid bean-temperature reading before detecting T0."
    return T0Status(
        auto_detection_enabled=True,
        status="pending",
        charge_temperature_c=session.auto_t0_charge_temperature_c,
        current_drop_c=session.auto_t0_current_drop_c,
        drop_threshold_c=config.session.auto_t0_drop_threshold_c,
        detected_bean_temperature_c=None,
        reason=reason,
    )


def _event_or_session_charge_temperature(
    event: RoastEvent | None,
    session: RoastSession,
) -> float | None:
    if event is not None:
        value = event.payload.get("charge_temperature_c")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return session.auto_t0_charge_temperature_c


def _serialize_control_result(session: RoastSession) -> ControlCommandResult:
    """Convert one control mutation into a stable tool response."""
    return ControlCommandResult(
        session_id=session.id,
        phase=session.phase,
        heat_level_percent=session.heat_level_percent,
        fan_level_percent=session.fan_level_percent,
        cooling_on=session.cooling_on,
    )


def _serialize_event_result(
    *,
    snapshot: RoastSession,
    event: RoastEvent,
) -> EventCommandResult:
    """Serialize the specific event produced or resolved for one command."""
    return EventCommandResult(
        session_id=snapshot.id,
        phase=snapshot.phase,
        event=_serialize_event(event),
        event_count=len(snapshot.event_timeline),
    )


def _serialize_event(event: RoastEvent) -> EventSnapshot:
    """Convert one roast event into a serializable snapshot."""
    return EventSnapshot(
        kind=event.kind,
        recorded_at_utc=event.recorded_at_utc.isoformat(),
        monotonic_seconds=event.monotonic_seconds,
        payload=dict(event.payload),
    )


def _iso_or_none(value: datetime | None) -> str | None:
    """Return one ISO8601 string when a datetime exists."""
    return value.isoformat() if value is not None else None
