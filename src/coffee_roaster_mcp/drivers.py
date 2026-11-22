"""Roaster driver contract and mock-safe implementation for RoastPilot."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from importlib import import_module
from math import isfinite
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal, Protocol, cast, runtime_checkable

from coffee_roaster_mcp.controls import validate_control_percent
from coffee_roaster_mcp.session import EventPayloadValue

ReportedTemperatureUnit = Literal["celsius", "unknown"]
HottopTemperatureUnit = Literal["celsius", "fahrenheit", "auto"]
ResolvedHottopTemperatureUnit = Literal["celsius", "fahrenheit"]
HOTTOP_DRIVER_NAME = "hottop_kn8828b_2k_plus"


def _raw_vendor_data_default() -> dict[str, EventPayloadValue]:
    """Return an empty raw vendor data mapping."""
    return {}


@dataclass(frozen=True)
class ControlRange:
    """Supported integer range for one roaster control.

    Attributes:
        minimum: Lowest accepted control value.
        maximum: Highest accepted control value.
        step: Smallest supported increment.
        unit: Human-readable control unit.
    """

    minimum: int
    maximum: int
    step: int
    unit: str = "percent"


@dataclass(frozen=True)
class SupportedActions:
    """Actions supported by one roaster driver.

    Attributes:
        heat_control: Whether heat percentage commands are supported.
        fan_control: Whether fan percentage commands are supported.
        bean_drop: Whether bean drop commands are supported.
        cooling_control: Whether cooling start and stop commands are supported.
        emergency_stop: Whether driver-owned emergency stop is supported.
    """

    heat_control: bool
    fan_control: bool
    bean_drop: bool
    cooling_control: bool
    emergency_stop: bool


@dataclass(frozen=True)
class SensorUnits:
    """Temperature units emitted by normalized driver state.

    Raw hardware may report Celsius or Fahrenheit, but `RoasterState`
    temperature fields are normalized to Celsius before crossing the driver
    boundary. `unknown` is reserved for drivers that cannot provide a sensor.

    Attributes:
        bean_temperature: Unit for normalized bean temperature readings.
        environment_temperature: Unit for normalized environment temperature readings.
    """

    bean_temperature: ReportedTemperatureUnit
    environment_temperature: ReportedTemperatureUnit


@dataclass(frozen=True)
class CommandStreaming:
    """Command streaming requirements for one roaster driver.

    Attributes:
        required: Whether the driver needs continuous command streaming.
        interval_seconds: Required command interval, when streaming is required.
    """

    required: bool
    interval_seconds: float | None = None

    def __post_init__(self) -> None:
        """Validate streaming settings are internally consistent."""
        if self.required and self.interval_seconds is None:
            raise ValueError("interval_seconds is required when command streaming is required.")
        if not self.required and self.interval_seconds is not None:
            raise ValueError(
                "interval_seconds must be None when command streaming is not required."
            )
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0.")


@dataclass(frozen=True)
class RoasterCapabilities:
    """Static capabilities exposed by one roaster driver.

    Attributes:
        driver: Stable driver identifier.
        heat: Supported heat range.
        fan: Supported fan range.
        actions: Supported control actions.
        sensor_units: Units used by normalized temperature readings.
        command_streaming: Driver command-streaming requirements.
        heat_containment_version: Supported fixed-zero containment declaration.
        manual_first_crack_version: Supported manual first-crack declaration.
        timing_evidence_version: Supported synthetic timing-evidence declaration.
        normal_drop_version: Supported exact normal-drop declaration.
    """

    driver: str
    heat: ControlRange
    fan: ControlRange
    actions: SupportedActions
    sensor_units: SensorUnits
    command_streaming: CommandStreaming
    heat_containment_version: int | None = None
    manual_first_crack_version: int | None = None
    timing_evidence_version: int | None = None
    normal_drop_version: int | None = None
    hottop_reception_candidate_version: int | None = None


@dataclass(frozen=True)
class ExactDropArbitration:
    """Per-call evidence required to atomically admit a mock normal drop.

    The driver verifies only its own two command-history counts. Operation and
    session identity remain store-owned, so they are carried for the caller
    without being interpreted at this boundary.
    """

    operation_id: str
    session_id: str
    cancelled: Event
    expected_drop_transition_count: int
    expected_emergency_count: int
    before_transition: Callable[[], None] | None = None


ExactDropCancellationDiscriminator = Literal[
    "emergency_cancelled",
    "emergency_count_changed",
    "transition_count_changed",
]


class ExactDropCancelled(Exception):
    """Report that an exact mock drop was refused before it changed state."""

    def __init__(self, discriminator: ExactDropCancellationDiscriminator) -> None:
        """Initialize a refusal with one closed cancellation discriminator."""
        if discriminator not in (
            "emergency_cancelled",
            "emergency_count_changed",
            "transition_count_changed",
        ):
            raise ValueError("Unsupported exact-drop cancellation discriminator.")
        self.discriminator = discriminator
        super().__init__(discriminator)


@dataclass(frozen=True)
class FixedZeroCommandedState:
    """Closed command-state evidence returned by fixed-zero containment.

    This value records driver command state only.  It is not a hardware
    acknowledgement, a transport-settlement record, or a physical-safety claim.
    """

    driver: str
    capability_version: int
    heat_level_percent: int
    roast_fan_level_percent: int
    main_fan_level_percent: int
    drum_motor_on: bool
    cooling_motor_on: bool
    solenoid_open: bool

    def __post_init__(self) -> None:
        """Validate the closed fixed-zero command-state grammar."""
        driver: Any = self.driver
        if not isinstance(driver, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", driver, flags=re.ASCII
        ):
            raise ValueError("driver must be an ASCII fixed-zero driver identifier.")
        capability_version: Any = self.capability_version
        if (
            isinstance(capability_version, bool)
            or not isinstance(capability_version, int)
            or capability_version != 1
        ):
            raise ValueError("capability_version must be the exact integer 1.")
        for name in (
            "heat_level_percent",
            "roast_fan_level_percent",
            "main_fan_level_percent",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an exact integer.")
            if name == "heat_level_percent" and value != 0:
                raise ValueError("heat_level_percent must be exactly 0.")
            if name != "heat_level_percent" and not 0 <= value <= 100:
                raise ValueError(f"{name} must be in the inclusive range 0..100.")
        for name in ("drum_motor_on", "cooling_motor_on", "solenoid_open"):
            _validate_exact_bool(getattr(self, name), label=name)

    def as_dict(self) -> dict[str, str | int | bool]:
        """Return the exact eight-key wire representation."""
        return {
            "driver": self.driver,
            "capability_version": self.capability_version,
            "heat_level_percent": self.heat_level_percent,
            "roast_fan_level_percent": self.roast_fan_level_percent,
            "main_fan_level_percent": self.main_fan_level_percent,
            "drum_motor_on": self.drum_motor_on,
            "cooling_motor_on": self.cooling_motor_on,
            "solenoid_open": self.solenoid_open,
        }


@dataclass(frozen=True)
class RoasterState:
    """Normalized roaster state returned by the driver boundary.

    Attributes:
        driver: Stable driver identifier.
        connected: Whether the driver is connected to its roaster backend.
        bean_temp_c: Normalized bean temperature in Celsius when available.
        env_temp_c: Normalized environment temperature in Celsius when available.
        heat_level_percent: Current heat control level.
        fan_level_percent: Current fan control level.
        cooling_on: Whether cooling is active.
        raw_vendor_data: Optional vendor-specific fields for diagnostics.
    """

    driver: str
    connected: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    raw_vendor_data: dict[str, EventPayloadValue] = field(default_factory=_raw_vendor_data_default)

    def __post_init__(self) -> None:
        """Validate normalized roaster state at the driver boundary."""
        _validate_driver_name(self.driver)
        _validate_exact_bool(self.connected, label="connected")
        _validate_exact_bool(self.cooling_on, label="cooling_on")
        object.__setattr__(
            self,
            "bean_temp_c",
            _validate_optional_temperature_c(self.bean_temp_c, label="bean_temp_c"),
        )
        object.__setattr__(
            self,
            "env_temp_c",
            _validate_optional_temperature_c(self.env_temp_c, label="env_temp_c"),
        )
        object.__setattr__(
            self,
            "heat_level_percent",
            validate_control_percent(
                self.heat_level_percent,
                label="heat_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "fan_level_percent",
            validate_control_percent(
                self.fan_level_percent,
                label="fan_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "raw_vendor_data",
            _validate_raw_vendor_data(self.raw_vendor_data),
        )


@dataclass(frozen=True)
class EmergencyStopResult:
    """Result returned by a driver-owned emergency stop action.

    Attributes:
        driver: Driver identifier that handled the safety action.
        safety_method: Driver method that was called.
        heat_level_percent: Heat level after the safety action.
        fan_level_percent: Fan level after the safety action.
        cooling_on: Cooling state after the safety action.
    """

    driver: str
    safety_method: str
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool

    def as_event_payload(self) -> dict[str, EventPayloadValue]:
        """Return event payload fields that prove the driver safety call ran."""
        return {
            "driver": self.driver,
            "driver_safety_method": self.safety_method,
            "driver_safety_method_called": True,
            "heat_level_percent": self.heat_level_percent,
            "fan_level_percent": self.fan_level_percent,
            "cooling_on": self.cooling_on,
        }


@dataclass(frozen=True)
class DriverLifecycleEvidence:
    """Non-actuating command-state evidence captured from one driver."""

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
    command_send_attempts: int | None
    command_write_count: int | None
    last_command_write_size: int | None
    command_loop_error_count: int | None
    status_packet_count: int | None
    status_read_error_count: int | None

    def __post_init__(self) -> None:
        """Validate that evidence is complete for its driver mode."""
        _validate_driver_name(self.driver)
        for name in (
            "connected",
            "command_streaming_required",
            "drum_motor_on",
            "cooling_motor_on",
            "solenoid_open",
        ):
            _validate_exact_bool(getattr(self, name), label=name)
        for name in (
            "heat_level_percent",
            "roast_fan_level_percent",
            "main_fan_level_percent",
        ):
            object.__setattr__(
                self, name, validate_control_percent(getattr(self, name), label=name)
            )
        optional = (
            "command_loop_running",
            "serial_open",
            "command_send_attempts",
            "command_write_count",
            "last_command_write_size",
            "command_loop_error_count",
            "status_packet_count",
            "status_read_error_count",
        )
        values = tuple(getattr(self, name) for name in optional)
        if self.command_streaming_required and any(value is None for value in values):
            raise ValueError("Streaming driver lifecycle evidence must be complete.")
        if not self.command_streaming_required and any(value is not None for value in values):
            raise ValueError("Non-streaming driver lifecycle evidence must omit lifecycle fields.")
        for name in ("command_loop_running", "serial_open"):
            value = getattr(self, name)
            if value is not None:
                _validate_exact_bool(value, label=name)
        for name in optional[2:]:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None.")


@runtime_checkable
class LifecycleEvidenceDriver(Protocol):
    """Optional read-only driver lifecycle-evidence capability."""

    def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
        """Return non-actuating driver command-state evidence."""
        ...


class RoasterDriver(Protocol):
    """Roaster driver interface used by the RoastPilot runtime."""

    name: str

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return static driver capabilities."""
        ...

    def connect(self) -> None:
        """Open the driver connection lifecycle."""
        ...

    def disconnect(self) -> None:
        """Close the driver connection lifecycle."""
        ...

    def read_state(self) -> RoasterState:
        """Return the latest normalized roaster state."""
        ...

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Apply a heat control update and return normalized state."""
        ...

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Apply a fan control update and return normalized state."""
        ...

    def drop_beans(self) -> RoasterState:
        """Trigger the bean drop action and return normalized state."""
        ...

    def start_cooling(self) -> RoasterState:
        """Start cooling and return normalized state."""
        ...

    def stop_cooling(self) -> RoasterState:
        """Stop cooling and return normalized state."""
        ...

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Apply the safest available driver-owned stop behavior."""
        ...


RoasterSafetyDriver = RoasterDriver


@runtime_checkable
class FixedZeroContainmentDriver(Protocol):
    """Optional explicit fixed-zero containment capability port."""

    def contain_heat_fixed_zero(self) -> FixedZeroCommandedState:
        """Set only commanded heat to zero and return closed command evidence."""
        ...


class SerialTransport(Protocol):
    """Minimal serial transport surface used by the Hottop lifecycle."""

    @property
    def is_open(self) -> bool:
        """Return whether the serial transport is open."""
        ...

    def close(self) -> None:
        """Close the serial transport."""
        ...

    def write(self, data: bytes) -> int:
        """Write command bytes to the serial transport."""
        ...


SerialTransportFactory = Callable[..., SerialTransport]
HottopCommandFrameProvider = Callable[[], bytes | None]
HOTTOP_PACKET_LENGTH = 36
HOTTOP_PACKET_PREFIX = bytes((0xA5, 0x96))
_HOTTOP_COMMAND_HEADER = bytes((0xA5, 0x96, 0xB0, 0xA0, 0x01, 0x01, 0x24))
_HOTTOP_COMMAND_HEAT_INDEX = 10
_HOTTOP_COMMAND_FAN_INDEX = 11
_HOTTOP_COMMAND_MAIN_FAN_INDEX = 12
_HOTTOP_COMMAND_SOLENOID_INDEX = 16
_HOTTOP_COMMAND_DRUM_MOTOR_INDEX = 17
_HOTTOP_COMMAND_COOLING_MOTOR_INDEX = 18
_HOTTOP_STATUS_ENV_TEMP_HIGH_INDEX = 23
_HOTTOP_STATUS_ENV_TEMP_LOW_INDEX = 24
_HOTTOP_STATUS_BEAN_TEMP_HIGH_INDEX = 25
_HOTTOP_STATUS_BEAN_TEMP_LOW_INDEX = 26
_HOTTOP_CHECKSUM_INDEX = HOTTOP_PACKET_LENGTH - 1
_HOTTOP_MIN_PLAUSIBLE_TEMP_C = 1.0
_HOTTOP_MAX_PLAUSIBLE_TEMP_C = 320.0
_HOTTOP_STATUS_READ_MAX_BYTES = HOTTOP_PACKET_LENGTH * 4

HottopReceiptReason = Literal[
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
]
HottopReceptionMode = Literal["off", "candidate/v1"]
HottopWriteResult = Literal["entering", "accepted_by_host", "unknown"]
HottopSettlementResult = Literal[
    "pending", "not_attempted", "entering", "accepted_by_host", "unknown"
]
HottopAttemptRole = Literal["ordinary", "drop", "emergency", "repeat"]


class HottopDropRefused(RuntimeError):
    """Raised when candidate-mode Drop admission retains uncertain ownership."""


@dataclass(frozen=True)
class HottopWriteAttempt:
    """Bounded host-side evidence for one candidate command write entry."""

    epoch: int
    handle_identity: int
    attempt_seq: int
    revision: int
    frame: bytes
    solenoid: int
    result: HottopWriteResult
    role: HottopAttemptRole = "ordinary"


@dataclass(frozen=True)
class HottopCommandSettlement:
    """First-attempt-only settlement for one Drop or emergency transition."""

    transition: Literal["drop", "emergency"]
    transition_seq: int
    transition_revision: int
    transition_epoch: int
    result: HottopSettlementResult
    physical_effect: Literal["not_established"] = "not_established"
    attempt_epoch: int | None = None
    attempt_handle_identity: int | None = None
    attempt_seq: int | None = None
    attempt_revision: int | None = None


@dataclass(frozen=True)
class HottopCommandPacket:
    """Hottop 36-byte command packet inputs.

    Attributes:
        heat_level_percent: Heater level in normalized 0-100 percent.
        fan_level_percent: Roast fan level in normalized 0-100 percent.
        main_fan_level_percent: Main fan level in normalized 0-100 percent.
        solenoid_open: Whether the drop solenoid path is active.
        drum_motor_on: Whether the drum motor command bit is active.
        cooling_motor_on: Whether the cooling motor command bit is active.
    """

    heat_level_percent: int = 0
    fan_level_percent: int = 0
    main_fan_level_percent: int = 0
    solenoid_open: bool = False
    drum_motor_on: bool = False
    cooling_motor_on: bool = False

    def __post_init__(self) -> None:
        """Validate normalized Hottop command packet fields."""
        object.__setattr__(
            self,
            "heat_level_percent",
            validate_control_percent(
                self.heat_level_percent,
                label="heat_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "fan_level_percent",
            validate_control_percent(
                self.fan_level_percent,
                label="fan_level_percent",
            ),
        )
        object.__setattr__(
            self,
            "main_fan_level_percent",
            validate_control_percent(
                self.main_fan_level_percent,
                label="main_fan_level_percent",
            ),
        )
        _validate_exact_bool(self.solenoid_open, label="solenoid_open")
        _validate_exact_bool(self.drum_motor_on, label="drum_motor_on")
        _validate_exact_bool(self.cooling_motor_on, label="cooling_motor_on")

    def to_bytes(self) -> bytes:
        """Build the Hottop 36-byte command packet with checksum."""
        packet = bytearray(HOTTOP_PACKET_LENGTH)
        packet[: len(_HOTTOP_COMMAND_HEADER)] = _HOTTOP_COMMAND_HEADER
        packet[_HOTTOP_COMMAND_HEAT_INDEX] = self.heat_level_percent
        packet[_HOTTOP_COMMAND_FAN_INDEX] = _percent_to_hottop_fan_scale(self.fan_level_percent)
        packet[_HOTTOP_COMMAND_MAIN_FAN_INDEX] = _percent_to_hottop_fan_scale(
            self.main_fan_level_percent
        )
        packet[_HOTTOP_COMMAND_SOLENOID_INDEX] = int(self.solenoid_open)
        packet[_HOTTOP_COMMAND_DRUM_MOTOR_INDEX] = int(self.drum_motor_on)
        packet[_HOTTOP_COMMAND_COOLING_MOTOR_INDEX] = int(self.cooling_motor_on)
        packet[_HOTTOP_CHECKSUM_INDEX] = calculate_hottop_packet_checksum(packet)
        return bytes(packet)


@dataclass(frozen=True)
class HottopStatusPacket:
    """Parsed Hottop 36-byte status packet.

    Attributes:
        bean_temp_c: Normalized plausible bean temperature in Celsius.
        env_temp_c: Normalized plausible environment temperature in Celsius.
        raw_bean_temperature: Raw bean temperature value from the packet.
        raw_env_temperature: Raw environment temperature value from the packet.
        temperature_unit: Configured Hottop raw temperature mode.
        resolved_temperature_unit: Raw unit selected for this packet, when plausible.
        raw_packet: Original validated 36-byte status packet.
    """

    bean_temp_c: float | None
    env_temp_c: float | None
    raw_bean_temperature: int
    raw_env_temperature: int
    temperature_unit: HottopTemperatureUnit
    resolved_temperature_unit: ResolvedHottopTemperatureUnit | None
    raw_packet: bytes


@dataclass(frozen=True)
class HottopStatusReadScan:
    """Pure status-read parse result with byte-ownership information.

    Attributes:
        packets: Complete, checksum-valid status packets in receive order.
        packet_offsets: Start offsets for `packets` in the combined parser input.
        skipped_byte_count: Bytes discarded while seeking valid packet boundaries.
        prior_tail_length: Bytes supplied from an earlier parser read.
        tail_length: Unconsumed trailing bytes retained for a later parser read.
        read_truncated: Whether the pre-read queue could not have been fully
            represented by the bounded returned bytes.
        decoder_temperature_unit: Unit used by the packet decoder.
        queued_before_read: Exact pre-read queue observation, when readable.
        requested_byte_count: Exact byte count requested from the transport.
        returned_byte_count: Exact byte count returned by the transport.
        tail_bytes: Bounded parser tail safe to provide to the next read.
        read_size_contradiction: Whether reported read sizes contradict the
            bounded read observation.
    """

    packets: tuple[HottopStatusPacket, ...]
    packet_offsets: tuple[int, ...]
    skipped_byte_count: int
    prior_tail_length: int
    tail_length: int
    read_truncated: bool
    decoder_temperature_unit: HottopTemperatureUnit = "celsius"
    queued_before_read: int | None = None
    requested_byte_count: int = _HOTTOP_STATUS_READ_MAX_BYTES
    returned_byte_count: int = 0
    tail_bytes: bytes = b""
    read_size_contradiction: bool = False

    def __post_init__(self) -> None:
        """Validate internally consistent scanner metadata."""
        if len(self.packets) != len(self.packet_offsets):
            raise ValueError("packet offsets must correspond to parsed packets.")
        for label, value in (
            ("skipped_byte_count", self.skipped_byte_count),
            ("prior_tail_length", self.prior_tail_length),
            ("tail_length", self.tail_length),
        ):
            _validate_nonnegative_exact_int(value, label=label)
        if any(isinstance(offset, bool) or offset < 0 for offset in self.packet_offsets):
            raise ValueError("packet offsets must be non-negative exact integers.")
        _validate_exact_bool(self.read_truncated, label="read_truncated")
        object.__setattr__(
            self,
            "decoder_temperature_unit",
            _validate_hottop_temperature_unit(self.decoder_temperature_unit),
        )
        if any(packet.temperature_unit != self.decoder_temperature_unit for packet in self.packets):
            raise ValueError("packet temperature_unit must match the scanner decoder unit.")
        if self.queued_before_read is not None:
            _validate_nonnegative_exact_int(
                self.queued_before_read,
                label="queued_before_read",
            )
        _validate_positive_exact_int(self.requested_byte_count, label="requested_byte_count")
        _validate_nonnegative_exact_int(self.returned_byte_count, label="returned_byte_count")
        if len(self.tail_bytes) != self.tail_length:
            raise ValueError("tail_bytes length must match tail_length.")
        if len(self.tail_bytes) > HOTTOP_PACKET_LENGTH - 1:
            raise ValueError("tail_bytes must be bounded to one partial packet.")
        _validate_exact_bool(self.read_size_contradiction, label="read_size_contradiction")


@dataclass(frozen=True)
class HottopDrainedFloor:
    """One same-epoch proof that the status parser and receive queue were drained.

    The identity is an opaque numeric token owned by the caller. It deliberately
    is not a transport object and is never a claim about physical acquisition
    time.

    Attributes:
        epoch: Reception epoch in which the drain was observed.
        handle_identity: Opaque numeric identity for the active handle.
        sequence: Receive sequence current when the drain was observed.
        observed_at: Host-clock time recorded before the zero-queue probe.
    """

    epoch: int
    handle_identity: int
    sequence: int
    observed_at: float

    def __post_init__(self) -> None:
        """Validate a finite, non-negative drain-floor observation."""
        _validate_nonnegative_exact_int(self.epoch, label="epoch")
        _validate_nonnegative_exact_int(self.handle_identity, label="handle_identity")
        _validate_nonnegative_exact_int(self.sequence, label="sequence")
        _validate_finite_time(self.observed_at, label="observed_at")


@dataclass(frozen=True)
class HottopReceivedPacket:
    """Typed host-receipt candidate derived from one bounded status read.

    `latest_pair_c` belongs only to the newest parsed packet. An invalid newest
    packet therefore never falls back to an earlier accepted pair.

    Attributes:
        epoch: Reception epoch associated with this bounded read.
        handle_identity: Opaque numeric identity for the read handle.
        sequence: Monotonic receipt sequence assigned by the caller.
        read_started_at: Host-clock time recorded before the pre-read probe.
        read_returned_at: Host-clock time recorded when the read returned.
        configured_temperature_unit: Configured raw Hottop temperature mode.
        queued_before_read: Exact pre-read queue observation, when readable.
        queued_after_read: Exact post-read queue observation, when readable.
        scan: Pure scanner output for this read.
        drained_floor: Earlier same-epoch fully-drained observation, if any.
        latest_pair_c: Newest packet's normalized Celsius pair, or `None`.
        latest_resolved_temperature_unit: Unit resolved for the newest packet.
    """

    epoch: int
    handle_identity: int
    sequence: int
    read_started_at: float
    read_returned_at: float
    configured_temperature_unit: HottopTemperatureUnit
    queued_before_read: int | None
    queued_after_read: int | None
    scan: HottopStatusReadScan
    drained_floor: HottopDrainedFloor | None
    latest_pair_c: tuple[float, float] | None
    latest_resolved_temperature_unit: ResolvedHottopTemperatureUnit | None

    def __post_init__(self) -> None:
        """Validate receipt fields without accepting a transport instance."""
        _validate_nonnegative_exact_int(self.epoch, label="epoch")
        _validate_nonnegative_exact_int(self.handle_identity, label="handle_identity")
        _validate_nonnegative_exact_int(self.sequence, label="sequence")
        _validate_finite_time(self.read_started_at, label="read_started_at")
        _validate_finite_time(self.read_returned_at, label="read_returned_at")
        object.__setattr__(
            self,
            "configured_temperature_unit",
            _validate_hottop_temperature_unit(self.configured_temperature_unit),
        )
        for label, value in (
            ("queued_before_read", self.queued_before_read),
            ("queued_after_read", self.queued_after_read),
        ):
            if value is not None:
                _validate_nonnegative_exact_int(value, label=label)
        if self.latest_pair_c is not None and (
            len(self.latest_pair_c) != 2
            or not all(
                not isinstance(value, bool) and isfinite(value) for value in self.latest_pair_c
            )
        ):
            raise ValueError("latest_pair_c must contain two finite temperatures.")
        if self.latest_resolved_temperature_unit not in ("celsius", "fahrenheit", None):
            raise ValueError("latest_resolved_temperature_unit is invalid.")
        latest_packet = self.scan.packets[-1] if self.scan.packets else None
        expected_pair = (
            (latest_packet.bean_temp_c, latest_packet.env_temp_c)
            if latest_packet is not None
            and latest_packet.bean_temp_c is not None
            and latest_packet.env_temp_c is not None
            else None
        )
        expected_unit = (
            latest_packet.resolved_temperature_unit if latest_packet is not None else None
        )
        if self.latest_pair_c != expected_pair:
            raise ValueError("latest_pair_c must be derived from the newest scanned packet.")
        if self.latest_resolved_temperature_unit != expected_unit:
            raise ValueError(
                "latest_resolved_temperature_unit must be derived from the newest scanned packet."
            )
        if self.configured_temperature_unit != self.scan.decoder_temperature_unit:
            raise ValueError("configured_temperature_unit must match the scanner decoder unit.")
        if self.queued_before_read != self.scan.queued_before_read:
            raise ValueError("queued_before_read must match the scanner observation.")


@dataclass(frozen=True)
class HottopReceiptClassification:
    """Pure eligibility result for a typed Hottop host-receipt candidate.

    The result intentionally leaves sensor age unknown: this software-only
    receipt has host timing only and does not claim a physical sample time or
    device-unit verification. It is evaluated only against the supplied host
    observations; a caller using a receipt at a later time must classify it
    again with its current observations.

    Attributes:
        eligible: Whether every host-receipt requirement was established.
        reasons: Ineligibility reasons in the fixed public precedence order.
        drained_floor_age_seconds: Host age measured from the drained floor.
        read_returned_age_seconds: Host age measured from read completion.
        sensor_age: Physical sensor age, always unknown for this receipt.
    """

    eligible: bool
    reasons: tuple[HottopReceiptReason, ...]
    drained_floor_age_seconds: float | None
    read_returned_age_seconds: float | None
    sensor_age: Literal["unknown"] = "unknown"

    def __post_init__(self) -> None:
        """Keep directly constructed classifications consistent with their reasons."""
        _validate_exact_bool(self.eligible, label="eligible")
        if self.eligible != (not self.reasons):
            raise ValueError("eligible must agree with whether reasons are empty.")

    @property
    def primary_reason(self) -> HottopReceiptReason | None:
        """Return the first fixed-order ineligibility reason, when present."""
        return self.reasons[0] if self.reasons else None


def scan_hottop_status_read(
    read_bytes: bytes,
    *,
    prior_tail: bytes = b"",
    queued_before_read: object,
    requested_byte_count: int = _HOTTOP_STATUS_READ_MAX_BYTES,
    temperature_unit: HottopTemperatureUnit = "celsius",
) -> HottopStatusReadScan:
    """Scan one bounded Hottop read without changing driver or parser state.

    Args:
        read_bytes: Bytes returned by the bounded transport read.
        prior_tail: Parser tail retained before this read, if any.
        queued_before_read: Explicit pre-read queue observation.
        requested_byte_count: Exact maximum requested from the transport.
        temperature_unit: Raw unit passed to the existing packet parser.

    Returns:
        Packet offsets, parsing ownership metadata, and truncation evidence.
    """
    _validate_positive_exact_int(requested_byte_count, label="requested_byte_count")
    normalized_temperature_unit = _validate_hottop_temperature_unit(temperature_unit)
    buffer = prior_tail + read_bytes
    packet_offsets: list[int] = []
    packets: list[HottopStatusPacket] = []
    cursor = 0
    skipped_byte_count = 0

    while cursor + HOTTOP_PACKET_LENGTH <= len(buffer):
        if not buffer.startswith(HOTTOP_PACKET_PREFIX, cursor):
            cursor += 1
            skipped_byte_count += 1
            continue
        candidate = buffer[cursor : cursor + HOTTOP_PACKET_LENGTH]
        if is_hottop_command_packet(candidate) or not validate_hottop_packet_checksum(candidate):
            cursor += 1
            skipped_byte_count += 1
            continue
        packets.append(
            parse_hottop_status_packet(
                candidate,
                temperature_unit=normalized_temperature_unit,
            )
        )
        packet_offsets.append(cursor)
        cursor += HOTTOP_PACKET_LENGTH

    tail_length = len(buffer) - cursor
    if tail_length > HOTTOP_PACKET_LENGTH - 1:
        skipped_byte_count += tail_length - (HOTTOP_PACKET_LENGTH - 1)
        tail_length = HOTTOP_PACKET_LENGTH - 1
    tail_bytes = buffer[-tail_length:] if tail_length else b""
    queue_observation = _queue_observation_or_none(queued_before_read)
    returned_byte_count = len(read_bytes)
    read_size_contradiction = returned_byte_count > requested_byte_count or (
        queue_observation is not None and returned_byte_count > queue_observation
    )
    read_truncated = False
    if queue_observation is not None:
        read_truncated = queue_observation > requested_byte_count or returned_byte_count < min(
            queue_observation,
            requested_byte_count,
        )
    return HottopStatusReadScan(
        packets=tuple(packets),
        packet_offsets=tuple(packet_offsets),
        skipped_byte_count=skipped_byte_count,
        prior_tail_length=len(prior_tail),
        tail_length=tail_length,
        read_truncated=read_truncated,
        decoder_temperature_unit=normalized_temperature_unit,
        queued_before_read=queue_observation,
        requested_byte_count=requested_byte_count,
        returned_byte_count=returned_byte_count,
        tail_bytes=tail_bytes,
        read_size_contradiction=read_size_contradiction,
    )


def build_hottop_received_packet(
    *,
    epoch: int,
    handle_identity: int,
    sequence: int,
    read_started_at: float,
    read_returned_at: float,
    configured_temperature_unit: HottopTemperatureUnit,
    queued_before_read: object,
    queued_after_read: object,
    scan: HottopStatusReadScan,
    drained_floor: HottopDrainedFloor | None,
) -> HottopReceivedPacket:
    """Build one typed receipt using only the newest parsed packet.

    Args:
        epoch: Explicit current reception epoch.
        handle_identity: Explicit opaque current-handle identity.
        sequence: Explicit monotonically assigned receipt sequence.
        read_started_at: Host time immediately before the pre-read probe.
        read_returned_at: Host time when the bounded read returned.
        configured_temperature_unit: Configured raw Hottop unit mode.
        queued_before_read: Explicit pre-read queue observation.
        queued_after_read: Explicit post-read queue observation.
        scan: Scanner result for these bytes.
        drained_floor: Earlier same-epoch fully-drained floor, if established.

    Returns:
        A typed receipt with the newest packet's pair or `None`.
    """
    normalized_configured_unit = _validate_hottop_temperature_unit(configured_temperature_unit)
    queue_observation = _queue_observation_or_none(queued_before_read)
    if normalized_configured_unit != scan.decoder_temperature_unit:
        raise ValueError("configured_temperature_unit must match the scanner decoder unit.")
    if queue_observation != scan.queued_before_read:
        raise ValueError("queued_before_read must match the scanner observation.")
    latest_packet = scan.packets[-1] if scan.packets else None
    latest_pair_c = (
        (latest_packet.bean_temp_c, latest_packet.env_temp_c)
        if latest_packet is not None
        and latest_packet.bean_temp_c is not None
        and latest_packet.env_temp_c is not None
        else None
    )
    return HottopReceivedPacket(
        epoch=epoch,
        handle_identity=handle_identity,
        sequence=sequence,
        read_started_at=read_started_at,
        read_returned_at=read_returned_at,
        configured_temperature_unit=normalized_configured_unit,
        queued_before_read=queue_observation,
        queued_after_read=_queue_observation_or_none(queued_after_read),
        scan=scan,
        drained_floor=drained_floor,
        latest_pair_c=latest_pair_c,
        latest_resolved_temperature_unit=(
            latest_packet.resolved_temperature_unit if latest_packet is not None else None
        ),
    )


def classify_hottop_received_packet(
    receipt: HottopReceivedPacket | None,
    *,
    candidate_configured: bool,
    current_epoch: int,
    current_handle_identity: int,
    current_sequence: int,
    now: float,
    receipt_age_bound_seconds: float | None,
) -> HottopReceiptClassification:
    """Classify a receipt from explicit host observations in fixed reason order.

    Args:
        receipt: Typed receipt candidate, or `None` when no receipt exists.
        candidate_configured: Whether the candidate feature is explicitly enabled.
        current_epoch: Epoch current at publication time.
        current_handle_identity: Opaque active-handle identity at publication time.
        current_sequence: Latest receipt sequence at publication time.
        now: Explicit host clock observation used for age comparison.
        receipt_age_bound_seconds: Positive host-receipt age bound, if configured.

    Returns:
        Eligibility, all applicable fixed-order reasons, and host receipt ages.
    """
    _validate_exact_bool(candidate_configured, label="candidate_configured")
    _validate_nonnegative_exact_int(current_epoch, label="current_epoch")
    _validate_nonnegative_exact_int(current_handle_identity, label="current_handle_identity")
    _validate_nonnegative_exact_int(current_sequence, label="current_sequence")
    _validate_finite_time(now, label="now")
    if receipt_age_bound_seconds is not None:
        _validate_positive_finite_bound(
            receipt_age_bound_seconds,
            label="receipt_age_bound_seconds",
        )

    reasons: list[HottopReceiptReason] = []
    if not candidate_configured:
        reasons.append("candidate_not_configured")
    if receipt is None:
        reasons.append("no_receipt")
        if receipt_age_bound_seconds is None:
            reasons.append("receipt_age_bound_unset")
        return HottopReceiptClassification(
            eligible=False,
            reasons=tuple(reasons),
            drained_floor_age_seconds=None,
            read_returned_age_seconds=None,
        )

    epoch_or_handle_changed = (
        receipt.epoch != current_epoch or receipt.handle_identity != current_handle_identity
    )
    if epoch_or_handle_changed:
        reasons.append("epoch_or_handle_changed")
    if receipt.sequence != current_sequence:
        reasons.append("superseded")
    if receipt.configured_temperature_unit != "celsius" or (
        receipt.latest_pair_c is not None and receipt.latest_resolved_temperature_unit != "celsius"
    ):
        reasons.append("unit_not_explicit_celsius")
    queue_readable = (
        receipt.queued_before_read is not None and receipt.queued_after_read is not None
    )
    if not queue_readable:
        reasons.append("queue_unreadable")
    if receipt.scan.read_size_contradiction:
        reasons.append("read_size_contradiction")
    if receipt.scan.read_truncated:
        reasons.append("read_truncated")
    if receipt.scan.skipped_byte_count != 0:
        reasons.append("misaligned_or_skipped")
    if receipt.scan.prior_tail_length != 0 or receipt.scan.tail_length != 0:
        reasons.append("parser_remainder")
    if queue_readable and receipt.queued_after_read != 0:
        reasons.append("backlog_visible")
    if receipt.latest_pair_c is None:
        reasons.append("latest_pair_rejected")
    elif any(
        packet.bean_temp_c is None or packet.env_temp_c is None for packet in receipt.scan.packets
    ):
        reasons.append("invalid_packet_history")

    floor = receipt.drained_floor
    floor_is_prior = (
        floor is not None
        and floor.epoch == receipt.epoch
        and floor.handle_identity == receipt.handle_identity
        and floor.sequence < receipt.sequence
        and floor.observed_at < receipt.read_started_at
    )
    if not floor_is_prior:
        reasons.append("no_drained_floor")

    clock_regression = (
        receipt.read_returned_at < receipt.read_started_at
        or now < receipt.read_returned_at
        or (floor is not None and now < floor.observed_at)
    )
    if clock_regression:
        reasons.append("clock_regression")
    if receipt_age_bound_seconds is None:
        reasons.append("receipt_age_bound_unset")

    drained_floor_age_seconds: float | None = None
    read_returned_age_seconds: float | None = None
    if floor_is_prior and not clock_regression:
        assert floor is not None
        drained_floor_age_seconds = now - floor.observed_at
        read_returned_age_seconds = now - receipt.read_returned_at
        if (
            receipt_age_bound_seconds is not None
            and drained_floor_age_seconds > receipt_age_bound_seconds
        ):
            reasons.append("receipt_age_exceeds_bound")

    return HottopReceiptClassification(
        eligible=not reasons,
        reasons=tuple(reasons),
        drained_floor_age_seconds=drained_floor_age_seconds,
        read_returned_age_seconds=read_returned_age_seconds,
    )


def build_hottop_command_packet(
    *,
    heat_level_percent: int = 0,
    fan_level_percent: int = 0,
    main_fan_level_percent: int = 0,
    solenoid_open: bool = False,
    drum_motor_on: bool = False,
    cooling_motor_on: bool = False,
) -> bytes:
    """Build a Hottop 36-byte command packet from normalized controls."""
    return HottopCommandPacket(
        heat_level_percent=heat_level_percent,
        fan_level_percent=fan_level_percent,
        main_fan_level_percent=main_fan_level_percent,
        solenoid_open=solenoid_open,
        drum_motor_on=drum_motor_on,
        cooling_motor_on=cooling_motor_on,
    ).to_bytes()


def parse_hottop_status_packet(
    packet: bytes,
    *,
    temperature_unit: HottopTemperatureUnit = "celsius",
) -> HottopStatusPacket:
    """Parse and validate one exact Hottop 36-byte status packet."""
    _validate_hottop_packet(packet)
    normalized_temperature_unit = _validate_hottop_temperature_unit(temperature_unit)
    raw_env_temperature = _read_big_endian_uint16(
        packet,
        high_index=_HOTTOP_STATUS_ENV_TEMP_HIGH_INDEX,
        low_index=_HOTTOP_STATUS_ENV_TEMP_LOW_INDEX,
    )
    raw_bean_temperature = _read_big_endian_uint16(
        packet,
        high_index=_HOTTOP_STATUS_BEAN_TEMP_HIGH_INDEX,
        low_index=_HOTTOP_STATUS_BEAN_TEMP_LOW_INDEX,
    )
    bean_temp_c, env_temp_c, resolved_unit = _normalize_hottop_temperatures(
        raw_bean_temperature=raw_bean_temperature,
        raw_env_temperature=raw_env_temperature,
        temperature_unit=normalized_temperature_unit,
    )
    return HottopStatusPacket(
        bean_temp_c=bean_temp_c,
        env_temp_c=env_temp_c,
        raw_bean_temperature=raw_bean_temperature,
        raw_env_temperature=raw_env_temperature,
        temperature_unit=normalized_temperature_unit,
        resolved_temperature_unit=resolved_unit,
        raw_packet=bytes(packet),
    )


def find_hottop_status_packet(
    buffer: bytes,
    *,
    temperature_unit: HottopTemperatureUnit = "celsius",
) -> HottopStatusPacket | None:
    """Return the first valid Hottop status packet found in serial bytes."""
    result = _find_hottop_status_packet_in_buffer(
        buffer,
        temperature_unit=temperature_unit,
    )
    if result is None:
        return None
    return result[0]


def _find_hottop_status_packet_in_buffer(
    buffer: bytes,
    *,
    temperature_unit: HottopTemperatureUnit,
) -> tuple[HottopStatusPacket, int] | None:
    """Return the first valid Hottop packet plus its exclusive end offset."""
    normalized_temperature_unit = _validate_hottop_temperature_unit(temperature_unit)
    search_limit = len(buffer) - HOTTOP_PACKET_LENGTH + 1
    for offset in range(max(0, search_limit)):
        if not buffer.startswith(HOTTOP_PACKET_PREFIX, offset):
            continue
        candidate = buffer[offset : offset + HOTTOP_PACKET_LENGTH]
        if is_hottop_command_packet(candidate):
            continue
        if validate_hottop_packet_checksum(candidate):
            status = parse_hottop_status_packet(
                candidate,
                temperature_unit=normalized_temperature_unit,
            )
            return status, offset + HOTTOP_PACKET_LENGTH
    return None


def _parse_hottop_status_buffer(
    buffer: bytes,
    *,
    temperature_unit: HottopTemperatureUnit,
) -> tuple[list[HottopStatusPacket], bytes]:
    """Parse all complete Hottop status packets and return the unconsumed tail."""
    statuses: list[HottopStatusPacket] = []
    remaining = buffer
    while True:
        result = _find_hottop_status_packet_in_buffer(
            remaining,
            temperature_unit=temperature_unit,
        )
        if result is None:
            return statuses, remaining[-(HOTTOP_PACKET_LENGTH - 1) :]
        status, end_offset = result
        statuses.append(status)
        remaining = remaining[end_offset:]


def calculate_hottop_packet_checksum(packet: bytes | bytearray) -> int:
    """Calculate the Hottop checksum over the first 35 packet bytes."""
    if len(packet) != HOTTOP_PACKET_LENGTH:
        raise ValueError(f"Hottop packet must be {HOTTOP_PACKET_LENGTH} bytes.")
    return sum(packet[:_HOTTOP_CHECKSUM_INDEX]) & 0xFF


def validate_hottop_packet_checksum(packet: bytes) -> bool:
    """Return whether a Hottop packet has a valid checksum byte."""
    if len(packet) != HOTTOP_PACKET_LENGTH:
        return False
    return packet[_HOTTOP_CHECKSUM_INDEX] == calculate_hottop_packet_checksum(packet)


def is_hottop_command_packet(packet: bytes) -> bool:
    """Return whether a packet has the Hottop command-frame header."""
    return len(packet) == HOTTOP_PACKET_LENGTH and packet.startswith(_HOTTOP_COMMAND_HEADER)


@dataclass(frozen=True)
class _MockThermalSample:
    """One completed mock thermal observation, kept private to the driver."""

    index: int
    bean_temp_c: float
    env_temp_c: float


@dataclass(frozen=True)
class _MockCommandState:
    """One captured mock command projection for a normalized response."""

    connected: bool
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    beans_dropped: bool


class MockRoasterDriver:
    """Mock roaster driver with deterministic local-only telemetry and controls."""

    name = "mock"
    _SAMPLE_INTERVAL_SECONDS = 1.0
    _INITIAL_TEMP_C = 20.0
    _MIN_TEMP_C = 15.0
    _MAX_ENV_TEMP_C = 300.0
    _MAX_BEAN_TEMP_C = 250.0
    _MAX_HEAT_RATE_C_PER_SEC = 2.0
    _MAX_FAN_COOLING_C_PER_SEC = 0.5
    _COOLING_MODE_RATE_C_PER_SEC = 5.0
    _BEAN_THERMAL_LAG_FACTOR = 0.1

    def __init__(self) -> None:
        """Initialize deterministic mock roaster state."""
        self._connected = False
        self._command_lock = Lock()
        self._drop_transition_count = 0
        self._emergency_count = 0
        self._thermal_writer_lock = Lock()
        self._thermal_sample_lock = Lock()
        self._thermal_sample = _MockThermalSample(
            index=0,
            bean_temp_c=self._INITIAL_TEMP_C,
            env_temp_c=self._INITIAL_TEMP_C,
        )
        self._heat_level_percent = 0
        self._fan_level_percent = 0
        self._cooling_on = False
        self._beans_dropped = False
        self._drum_motor_on = False
        self._solenoid_open = False

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return mock-safe capabilities for local development."""
        return RoasterCapabilities(
            driver=self.name,
            heat=ControlRange(minimum=0, maximum=100, step=1),
            fan=ControlRange(minimum=0, maximum=100, step=1),
            actions=SupportedActions(
                heat_control=True,
                fan_control=True,
                bean_drop=True,
                cooling_control=True,
                emergency_stop=True,
            ),
            sensor_units=SensorUnits(
                bean_temperature="celsius",
                environment_temperature="celsius",
            ),
            command_streaming=CommandStreaming(required=False),
            heat_containment_version=1,
            manual_first_crack_version=1,
            timing_evidence_version=1,
            normal_drop_version=1,
        )

    @property
    def drop_transition_count(self) -> int:
        """Return the non-telemetry count of admitted mock drop transitions."""
        with self._command_lock:
            return self._drop_transition_count

    @property
    def emergency_count(self) -> int:
        """Return the non-telemetry count of attempted mock emergency stops."""
        with self._command_lock:
            return self._emergency_count

    def connect(self) -> None:
        """Mark the mock driver connected."""
        self._connected = True

    def disconnect(self) -> None:
        """Mark the mock driver disconnected."""
        self._connected = False

    def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
        """Return mock command-state evidence without advancing telemetry."""
        return DriverLifecycleEvidence(
            driver=self.name,
            connected=self._connected,
            command_streaming_required=False,
            command_loop_running=None,
            serial_open=None,
            heat_level_percent=self._heat_level_percent,
            roast_fan_level_percent=0,
            main_fan_level_percent=self._fan_level_percent,
            drum_motor_on=self._drum_motor_on,
            cooling_motor_on=self._cooling_on,
            solenoid_open=self._solenoid_open,
            command_send_attempts=None,
            command_write_count=None,
            last_command_write_size=None,
            command_loop_error_count=None,
            status_packet_count=None,
            status_read_error_count=None,
        )

    def read_state(self) -> RoasterState:
        """Return deterministic mock roaster state."""
        return self._state_snapshot(self._advance_telemetry())

    def _state_snapshot(self, thermal_sample: _MockThermalSample | None = None) -> RoasterState:
        """Return the current mock roaster state without advancing telemetry."""
        if thermal_sample is None:
            with self._thermal_sample_lock:
                thermal_sample = self._thermal_sample
        return self._state_snapshot_from_command(
            thermal_sample=thermal_sample,
            command_state=self._capture_command_state(),
        )

    def _capture_command_state(self) -> _MockCommandState:
        """Capture mutable command fields for one normalized mock response."""
        return _MockCommandState(
            connected=self._connected,
            heat_level_percent=self._heat_level_percent,
            fan_level_percent=self._fan_level_percent,
            cooling_on=self._cooling_on,
            beans_dropped=self._beans_dropped,
        )

    def _state_snapshot_from_command(
        self,
        *,
        thermal_sample: _MockThermalSample,
        command_state: _MockCommandState,
    ) -> RoasterState:
        """Assemble a response from captured command and thermal observations."""
        return RoasterState(
            driver=self.name,
            connected=command_state.connected,
            bean_temp_c=thermal_sample.bean_temp_c,
            env_temp_c=thermal_sample.env_temp_c,
            heat_level_percent=command_state.heat_level_percent,
            fan_level_percent=command_state.fan_level_percent,
            cooling_on=command_state.cooling_on,
            raw_vendor_data={
                "beans_dropped": command_state.beans_dropped,
                "sample_index": thermal_sample.index,
                "telemetry_model": "fixed_step_thermal_v1",
            },
        )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Set mock heat and return normalized state."""
        self._heat_level_percent = validate_control_percent(
            heat_level_percent,
            label="heat_level_percent",
        )
        if self._heat_level_percent > 0:
            self._drum_motor_on = True
        return self._state_snapshot()

    def contain_heat_fixed_zero(self) -> FixedZeroCommandedState:
        """Set only mock commanded heat to zero and return command evidence."""
        self._heat_level_percent = 0
        return FixedZeroCommandedState(
            driver=self.name,
            capability_version=1,
            heat_level_percent=self._heat_level_percent,
            roast_fan_level_percent=0,
            main_fan_level_percent=self._fan_level_percent,
            drum_motor_on=self._drum_motor_on,
            cooling_motor_on=self._cooling_on,
            solenoid_open=self._solenoid_open,
        )

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Set mock fan and return normalized state."""
        self._fan_level_percent = validate_control_percent(
            fan_level_percent,
            label="fan_level_percent",
        )
        return self._state_snapshot()

    def drop_beans(self, *, exact_drop: ExactDropArbitration | None = None) -> RoasterState:
        """Record a mock bean drop and enter cooling.

        Exact arbitration is mock-only and proves that neither a cancellation,
        emergency stop, nor competing drop transition intervened before this
        transition's atomic command-state change.
        """
        if exact_drop is not None and exact_drop.before_transition is not None:
            exact_drop.before_transition()
        with self._command_lock:
            if exact_drop is not None:
                if exact_drop.cancelled.is_set():
                    raise ExactDropCancelled("emergency_cancelled")
                if self._emergency_count != exact_drop.expected_emergency_count:
                    raise ExactDropCancelled("emergency_count_changed")
                if self._drop_transition_count != exact_drop.expected_drop_transition_count:
                    raise ExactDropCancelled("transition_count_changed")
            self._drop_transition_count += 1
            self._beans_dropped = True
            self._heat_level_percent = 0
            self._fan_level_percent = 100
            self._cooling_on = True
            self._drum_motor_on = True
            self._solenoid_open = True
            command_state = self._capture_command_state()
        with self._thermal_sample_lock:
            thermal_sample = self._thermal_sample
        return self._state_snapshot_from_command(
            thermal_sample=thermal_sample,
            command_state=command_state,
        )

    def start_cooling(self) -> RoasterState:
        """Start mock cooling and return normalized state."""
        self._cooling_on = True
        self._fan_level_percent = 100
        return self._state_snapshot()

    def stop_cooling(self) -> RoasterState:
        """Stop mock cooling and return normalized state."""
        self._cooling_on = False
        self._fan_level_percent = 0
        self._drum_motor_on = False
        self._solenoid_open = False
        return self._state_snapshot()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Return deterministic mock-safe emergency stop controls."""
        _ = reason
        with self._command_lock:
            self._emergency_count += 1
            self._heat_level_percent = 0
            self._fan_level_percent = 100
            self._cooling_on = True
            self._drum_motor_on = False
            self._solenoid_open = False
            heat_level_percent = self._heat_level_percent
            fan_level_percent = self._fan_level_percent
            cooling_on = self._cooling_on
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=heat_level_percent,
            fan_level_percent=fan_level_percent,
            cooling_on=cooling_on,
        )

    def _advance_telemetry(self) -> _MockThermalSample:
        """Advance, publish, and return one deterministic mock telemetry sample."""
        with self._thermal_writer_lock:
            with self._thermal_sample_lock:
                predecessor = self._thermal_sample
            env_delta_c = self._environment_delta_c()
            env_temp_c = _clamp_float(
                predecessor.env_temp_c + env_delta_c,
                minimum=self._MIN_TEMP_C,
                maximum=self._MAX_ENV_TEMP_C,
            )
            bean_delta_c = (
                (env_temp_c - predecessor.bean_temp_c)
                * self._BEAN_THERMAL_LAG_FACTOR
                * self._SAMPLE_INTERVAL_SECONDS
            )
            completed = _MockThermalSample(
                index=predecessor.index + 1,
                bean_temp_c=_clamp_float(
                    predecessor.bean_temp_c + bean_delta_c,
                    minimum=self._MIN_TEMP_C,
                    maximum=self._MAX_BEAN_TEMP_C,
                ),
                env_temp_c=env_temp_c,
            )
            with self._thermal_sample_lock:
                self._thermal_sample = completed
            return completed

    def _environment_delta_c(self) -> float:
        """Return one fixed-step environment temperature delta."""
        heat_effect = (self._heat_level_percent / 100.0) * self._MAX_HEAT_RATE_C_PER_SEC
        fan_effect = (self._fan_level_percent / 100.0) * self._MAX_FAN_COOLING_C_PER_SEC
        cooling_effect = self._COOLING_MODE_RATE_C_PER_SEC if self._cooling_on else 0.0
        return (heat_effect - fan_effect - cooling_effect) * self._SAMPLE_INTERVAL_SECONDS


class HottopStartPhase(Enum):
    """Private staged-start lifecycle; no phase permits command streaming."""

    RESERVED = "reserved"
    OPENING = "opening"
    OPEN_INHIBITED = "open_inhibited"
    PROMOTING = "promoting"
    PROMOTED = "promoted"
    CLAIMED = "claimed"
    ENTERING = "entering"
    WRITTEN = "written"
    CLOSING = "closing"
    UNRESOLVED = "unresolved"


class HottopStartSettlement(Enum):
    """Evidence of whether one exact staged attempt has been disposed."""

    NOT_ENTERED = "not_entered"
    CLEAN_CLOSED = "clean_closed"
    UNRESOLVED = "unresolved"


class HottopFirstWriteDisposition(Enum):
    """Private evidence retained for the staged first native write."""

    NOT_CLAIMED = "not_claimed"
    WRITTEN = "written"
    UNKNOWN = "unknown"


@dataclass(frozen=True, eq=False)
class HottopStartOwner:
    """Driver-minted identity bound to one server reservation and generation."""

    server_token: str
    generation: int


@dataclass(frozen=True)
class _HottopFirstWriteClaim:
    """Immutable, exact-owner packet prepared for one staged native write."""

    owner: HottopStartOwner
    session: object
    handle: SerialTransport
    revision: int
    frame: bytes


class HottopRoasterDriver:
    """Hottop KN-8828B-2K+ driver lifecycle and command-state owner.

    The driver owns Hottop command state and streams the latest verified packet
    whenever the serial command loop is connected. Command methods are safe to
    call before connect; no serial bytes are written until the lifecycle is
    explicitly opened.
    """

    name = HOTTOP_DRIVER_NAME

    def __init__(
        self,
        *,
        port: str | None = None,
        baudrate: int = 115_200,
        temperature_unit: HottopTemperatureUnit = "celsius",
        command_interval_seconds: float = 0.3,
        serial_factory: SerialTransportFactory | None = None,
        join_timeout_seconds: float = 1.0,
        command_loop_iteration_hook: Callable[[], None] | None = None,
        command_frame_provider: HottopCommandFrameProvider | None = None,
        hottop_reception: HottopReceptionMode = "off",
        reception_clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize Hottop lifecycle dependencies.

        Args:
            port: Serial port path for the Hottop controller.
            baudrate: Serial baudrate.
            temperature_unit: Raw Hottop temperature unit mode.
            command_interval_seconds: Command-loop cadence.
            serial_factory: Optional injectable serial transport factory for tests.
            join_timeout_seconds: Maximum disconnect wait for command-loop cleanup.
            command_loop_iteration_hook: Optional hook called after a loop iteration.
            command_frame_provider: Optional command-frame source for each loop tick.
            hottop_reception: Opt-in host-receipt candidate mode.
            reception_clock: Optional injected host clock for simulated candidate tests.
        """
        if command_interval_seconds <= 0:
            raise ValueError("command_interval_seconds must be greater than 0.")
        if join_timeout_seconds <= 0:
            raise ValueError("join_timeout_seconds must be greater than 0.")
        normalized_temperature_unit = _validate_hottop_temperature_unit(temperature_unit)
        if hottop_reception not in {"off", "candidate/v1"}:
            raise ValueError("hottop_reception must be exactly 'off' or 'candidate/v1'.")
        if hottop_reception == "candidate/v1" and serial_factory is None:
            raise ValueError(
                "candidate/v1 requires an injected simulated transport; "
                "physical transport not qualified"
            )
        if hottop_reception == "candidate/v1" and normalized_temperature_unit != "celsius":
            raise ValueError("candidate/v1 requires temperature_unit celsius.")
        if hottop_reception == "candidate/v1" and command_frame_provider is not None:
            raise ValueError("candidate/v1 does not permit command_frame_provider.")
        self._port = port
        self._baudrate = baudrate
        self._temperature_unit: HottopTemperatureUnit = normalized_temperature_unit
        self._command_interval_seconds = command_interval_seconds
        self._serial_factory = serial_factory or _create_pyserial_transport
        self._hottop_reception: HottopReceptionMode = hottop_reception
        self._reception_clock = reception_clock or monotonic
        self._join_timeout_seconds = join_timeout_seconds
        self._serial: SerialTransport | None = None
        self._connected = False
        self._start_owner: HottopStartOwner | None = None
        self._start_phase: HottopStartPhase | None = None
        self._start_handle: SerialTransport | None = None
        self._start_generation = 0
        self._start_session: object | None = None
        self._start_claim: _HottopFirstWriteClaim | None = None
        self._start_write_disposition = HottopFirstWriteDisposition.NOT_CLAIMED
        self._stop_event = Event()
        self._disconnect_requested = Event()
        self._command_thread: Thread | None = None
        self._lifecycle_lock = Lock()
        self._state_lock = Lock()
        self._command_write_lock = Lock()
        self._write_timeout_seconds = command_interval_seconds
        self._command_loop_iterations = 0
        self._command_frame_poll_count = 0
        self._command_send_attempts = 0
        self._command_write_count = 0
        self._last_command_write_size = 0
        self._command_loop_error_count = 0
        self._command_loop_iteration_hook = command_loop_iteration_hook
        self._command_frame_provider = command_frame_provider or self._current_command_frame
        self._heat_level_percent = 0
        self._roast_fan_level_percent = 0
        self._main_fan_level_percent = 0
        self._solenoid_open = False
        self._drum_motor_on = False
        self._cooling_motor_on = False
        self._command_state_revision = 0
        self._latest_bean_temp_c: float | None = None
        self._latest_env_temp_c: float | None = None
        self._latest_raw_bean_temperature: int | None = None
        self._latest_raw_env_temperature: int | None = None
        self._latest_resolved_temperature_unit: ResolvedHottopTemperatureUnit | None = None
        self._status_packet_count = 0
        self._ignored_temperature_packet_count = 0
        self._status_read_error_count = 0
        self._status_buffer = b""
        self._reception_epoch = 0
        self._reception_sequence = 0
        self._reception_floor: HottopDrainedFloor | None = None
        self._reception_record: HottopReceivedPacket | None = None
        self._reception_discard_count = 0
        self._attempt_sequence = 0
        self._attempt_history: list[HottopWriteAttempt] = []
        self._drop_transition_count = 0
        self._emergency_count = 0
        self._drop_settlement: HottopCommandSettlement | None = None
        self._emergency_settlement: HottopCommandSettlement | None = None

    @property
    def capabilities(self) -> RoasterCapabilities:
        """Return static Hottop capabilities for command validation."""
        return RoasterCapabilities(
            driver=self.name,
            heat=ControlRange(minimum=0, maximum=100, step=1),
            fan=ControlRange(minimum=0, maximum=100, step=1),
            actions=SupportedActions(
                heat_control=True,
                fan_control=True,
                bean_drop=True,
                cooling_control=True,
                emergency_stop=True,
            ),
            sensor_units=SensorUnits(
                bean_temperature="celsius",
                environment_temperature="celsius",
            ),
            command_streaming=CommandStreaming(
                required=True,
                interval_seconds=self._command_interval_seconds,
            ),
            heat_containment_version=1,
            hottop_reception_candidate_version=(
                1 if self._hottop_reception == "candidate/v1" else None
            ),
        )

    def inspect_inhibited_start(
        self,
    ) -> tuple[HottopStartOwner | None, HottopStartPhase | None]:
        """Read ownership without waiting for native open or close."""
        with self._state_lock:
            return self._start_owner, self._start_phase

    def reserve_inhibited_start(self, server_token: str) -> HottopStartOwner:
        """Allocate exact ownership before any native factory entry."""
        if self._hottop_reception == "candidate/v1":
            raise RuntimeError("candidate/v1 does not support inhibited Start.")
        self._refuse_staged_connect()
        with self._lifecycle_lock, self._state_lock:
            if self._start_owner is not None:
                raise RuntimeError("A staged Hottop start already owns this driver.")
            if self._connected or self._serial is not None or self._command_thread is not None:
                raise RuntimeError("Legacy Hottop lifecycle must be absent before staging.")
            self._start_generation += 1
            owner = HottopStartOwner(server_token, self._start_generation)
            self._start_owner = owner
            self._start_phase = HottopStartPhase.RESERVED
            self._stop_event.set()
            return owner

    def open_inhibited_start(self, owner: HottopStartOwner) -> None:
        """Open one owned transport without starting a command loop or writing."""
        with self._lifecycle_lock:
            with self._state_lock:
                if (
                    self._start_owner is not owner
                    or self._start_phase is not HottopStartPhase.RESERVED
                ):
                    raise RuntimeError("Staged Hottop owner is not reserved.")
                if self._port is None:
                    raise ValueError("Hottop serial port is required before connect.")
                port = self._port
                self._start_phase = HottopStartPhase.OPENING
            try:
                handle = self._serial_factory(
                    port,
                    baudrate=self._baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=0.5,
                    write_timeout=self._write_timeout_seconds,
                )
            except BaseException:
                with self._state_lock:
                    self._start_phase = HottopStartPhase.UNRESOLVED
                raise
            with self._state_lock:
                self._start_handle = handle
                self._start_phase = HottopStartPhase.OPEN_INHIBITED

    def abort_inhibited_start(self, owner: HottopStartOwner) -> HottopStartSettlement:
        """Dispose one exact attempt once; retain identity on uncertain native outcomes."""
        with self._lifecycle_lock:
            with self._state_lock:
                if self._start_owner is not owner:
                    return HottopStartSettlement.UNRESOLVED
                if self._start_phase is HottopStartPhase.RESERVED:
                    self._start_owner = None
                    self._start_phase = None
                    self._start_session = None
                    self._start_claim = None
                    return HottopStartSettlement.NOT_ENTERED
                if self._start_phase is not HottopStartPhase.OPEN_INHIBITED:
                    return HottopStartSettlement.UNRESOLVED
                handle = self._start_handle
                self._start_phase = HottopStartPhase.CLOSING
            closed = False
            try:
                if handle is not None:
                    handle.close()
                    closed = handle.is_open is False
            except Exception:
                # Native failure provides no exact positive disposal evidence.
                pass
            except BaseException:
                with self._state_lock:
                    self._start_phase = HottopStartPhase.UNRESOLVED
                raise
            with self._state_lock:
                if self._start_owner is owner and self._start_handle is handle and closed:
                    self._start_owner = None
                    self._start_phase = None
                    self._start_handle = None
                    self._start_session = None
                    self._start_claim = None
                    return HottopStartSettlement.CLEAN_CLOSED
                self._start_phase = HottopStartPhase.UNRESOLVED
                return HottopStartSettlement.UNRESOLVED

    def begin_inhibited_start_promotion(self, owner: HottopStartOwner) -> None:
        """Move one exact open owner into private publication preparation."""
        with self._state_lock:
            if (
                self._start_owner is not owner
                or self._start_phase is not HottopStartPhase.OPEN_INHIBITED
                or self._start_handle is None
            ):
                raise RuntimeError("Staged Hottop owner is not open and inhibited.")
            self._start_phase = HottopStartPhase.PROMOTING

    def restore_inhibited_start_after_unpublished_promotion(self, owner: HottopStartOwner) -> bool:
        """Restore only an exact unpublished promotion so S3a disposal can run."""
        with self._state_lock:
            if (
                self._start_owner is not owner
                or self._start_phase is not HottopStartPhase.PROMOTING
                or self._start_session is not None
            ):
                return False
            self._start_phase = HottopStartPhase.OPEN_INHIBITED
            return True

    def bind_promoted_inhibited_start(self, owner: HottopStartOwner, session: object) -> None:
        """Bind an already-published exact session to its staged driver owner."""
        with self._state_lock:
            if (
                self._start_owner is not owner
                or self._start_phase is not HottopStartPhase.PROMOTING
                or self._start_handle is None
            ):
                raise RuntimeError("Staged Hottop owner cannot bind the published session.")
            self._start_session = session
            self._start_phase = HottopStartPhase.PROMOTED

    def prepare_promoted_first_write(
        self, owner: HottopStartOwner, session: object
    ) -> _HottopFirstWriteClaim:
        """Capture one immutable first-write frame without granting entry."""
        with self._state_lock:
            if (
                self._start_owner is not owner
                or self._start_session is not session
                or self._start_phase is not HottopStartPhase.PROMOTED
                or self._start_handle is None
            ):
                raise RuntimeError("Staged Hottop owner cannot claim a first write.")
            revision = self._command_state_revision
            values = (
                self._heat_level_percent,
                self._roast_fan_level_percent,
                self._main_fan_level_percent,
                self._solenoid_open,
                self._drum_motor_on,
                self._cooling_motor_on,
            )
            handle = self._start_handle
        frame = build_hottop_command_packet(
            heat_level_percent=values[0],
            fan_level_percent=values[1],
            main_fan_level_percent=values[2],
            solenoid_open=values[3],
            drum_motor_on=values[4],
            cooling_motor_on=values[5],
        )
        return _HottopFirstWriteClaim(owner, session, handle, revision, frame)

    def claim_prepared_first_write(
        self, prepared: _HottopFirstWriteClaim
    ) -> _HottopFirstWriteClaim:
        """Publish a prepared exact-owner possible-entry claim once."""
        with self._state_lock:
            if (
                self._start_owner is not prepared.owner
                or self._start_session is not prepared.session
                or self._start_phase is not HottopStartPhase.PROMOTED
                or self._start_handle is not prepared.handle
                or self._command_state_revision != prepared.revision
            ):
                raise RuntimeError("Staged Hottop command state changed before first-write entry.")
            self._start_claim = prepared
            self._start_write_disposition = HottopFirstWriteDisposition.NOT_CLAIMED
            self._start_phase = HottopStartPhase.CLAIMED
            return prepared

    def dispatch_claimed_first_write(
        self, claim: _HottopFirstWriteClaim
    ) -> HottopFirstWriteDisposition:
        """Invoke exactly one claimed native write and retain its strict receipt evidence."""
        with self._state_lock:
            if (
                self._start_owner is not claim.owner
                or self._start_session is not claim.session
                or self._start_claim is not claim
                or self._start_phase is not HottopStartPhase.CLAIMED
                or self._start_handle is not claim.handle
            ):
                raise RuntimeError("Staged Hottop first-write claim is no longer current.")
            # CLAIMED proves a frame may be considered for entry; ENTERING consumes
            # that right before releasing this lock for an unbounded native write.
            self._start_phase = HottopStartPhase.ENTERING
        try:
            receipt = claim.handle.write(claim.frame)
        except BaseException:
            with self._state_lock:
                if self._start_claim is claim and self._start_phase is HottopStartPhase.ENTERING:
                    self._start_write_disposition = HottopFirstWriteDisposition.UNKNOWN
                    self._start_phase = HottopStartPhase.UNRESOLVED
            raise
        with self._state_lock:
            if self._start_claim is not claim or self._start_phase is not HottopStartPhase.ENTERING:
                raise RuntimeError("Staged Hottop first-write entry is no longer current.")
            if type(receipt) is int and receipt == len(claim.frame):
                self._start_write_disposition = HottopFirstWriteDisposition.WRITTEN
                self._start_phase = HottopStartPhase.WRITTEN
                return HottopFirstWriteDisposition.WRITTEN
            self._start_write_disposition = HottopFirstWriteDisposition.UNKNOWN
            self._start_phase = HottopStartPhase.UNRESOLVED
            return HottopFirstWriteDisposition.UNKNOWN

    def asserted_written_start(self, owner: HottopStartOwner, session: object) -> bool:
        """Return whether the exact owner retains one complete first-write receipt."""
        with self._state_lock:
            return bool(
                self._start_owner is owner
                and self._start_session is session
                and self._start_phase is HottopStartPhase.WRITTEN
                and self._start_write_disposition is HottopFirstWriteDisposition.WRITTEN
                and self._start_claim is not None
                and self._command_state_revision == self._start_claim.revision
            )

    def _refuse_staged_connect(self) -> None:
        """Refuse unrelated legacy reuse before waiting on native lifecycle work."""
        with self._state_lock:
            if self._start_owner is not None:
                raise RuntimeError("A staged Hottop start retains transport authority.")

    def connect(self) -> None:
        """Open serial transport and start the Hottop command-loop lifecycle."""
        self._refuse_staged_connect()
        with self._lifecycle_lock:
            self._refuse_staged_connect()
            with self._state_lock:
                if self._connected:
                    return
                if self._command_thread is not None and self._command_thread.is_alive():
                    raise RuntimeError("Previous Hottop command loop is still stopping.")
                if self._port is None:
                    raise ValueError("Hottop serial port is required before connect.")
                port = self._port

            serial_transport = self._serial_factory(
                port,
                baudrate=self._baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.5,
                write_timeout=self._write_timeout_seconds,
            )

            with self._state_lock:
                self._serial = serial_transport
                if self._hottop_reception == "candidate/v1":
                    self._begin_reception_epoch(serial_transport)
                self._connected = True
                self._disconnect_requested.clear()
                self._stop_event.clear()
                self._command_thread = Thread(
                    target=self._command_loop,
                    daemon=True,
                    name="HottopCommandLoop",
                )
                self._command_thread.start()

    def disconnect(self) -> None:
        """Stop command loop and close serial transport."""
        with self._lifecycle_lock:
            command_thread, serial_transport = self._request_command_loop_stop()

            if command_thread is not None and command_thread.is_alive():
                command_thread.join(timeout=self._join_timeout_seconds)
            command_loop_still_running = command_thread is not None and command_thread.is_alive()

            if serial_transport is not None and serial_transport.is_open:
                self._close_serial_transport(serial_transport)

            with self._state_lock:
                if not command_loop_still_running:
                    self._command_thread = None
                self._serial = None

            if command_loop_still_running:
                raise RuntimeError("Hottop command loop did not stop during disconnect.")

    def _request_command_loop_stop(
        self,
    ) -> tuple[Thread | None, SerialTransport | None]:
        """Publish stop state while coordinating with the command write path."""
        with self._state_lock:
            serial_transport = self._serial

        close_failed = False
        write_lock_acquired = self._command_write_lock.acquire(timeout=self._write_timeout_seconds)
        if not write_lock_acquired and serial_transport is not None and serial_transport.is_open:
            try:
                serial_transport.close()
            except Exception:
                close_failed = True
                with self._state_lock:
                    self._command_loop_error_count += 1
            write_lock_acquired = self._command_write_lock.acquire(
                timeout=self._write_timeout_seconds
            )

        try:
            with self._state_lock:
                self._disconnect_requested.set()
                self._connected = False
                self._stop_event.set()
                if close_failed:
                    self._last_command_write_size = 0
                return self._command_thread, self._serial
        finally:
            if write_lock_acquired:
                self._command_write_lock.release()

    def _close_serial_transport(self, serial_transport: SerialTransport) -> None:
        """Close serial transport without waiting behind a blocked write."""
        if self._command_write_lock.acquire(blocking=False):
            try:
                if serial_transport.is_open:
                    serial_transport.close()
            finally:
                self._command_write_lock.release()
            return
        serial_transport.close()

    def read_state(self) -> RoasterState:
        """Return the current normalized Hottop lifecycle state."""
        with self._state_lock:
            command_loop_running = (
                self._command_thread is not None and self._command_thread.is_alive()
            )
            return RoasterState(
                driver=self.name,
                connected=self._connected,
                bean_temp_c=self._latest_bean_temp_c,
                env_temp_c=self._latest_env_temp_c,
                heat_level_percent=self._heat_level_percent,
                fan_level_percent=self._main_fan_level_percent,
                cooling_on=self._cooling_motor_on,
                raw_vendor_data={
                    "port": self._port,
                    "baudrate": self._baudrate,
                    "temperature_unit": self._temperature_unit,
                    "resolved_temperature_unit": self._latest_resolved_temperature_unit,
                    "raw_bean_temperature": self._latest_raw_bean_temperature,
                    "raw_env_temperature": self._latest_raw_env_temperature,
                    "status_packet_count": self._status_packet_count,
                    "ignored_temperature_packet_count": self._ignored_temperature_packet_count,
                    "status_read_error_count": self._status_read_error_count,
                    "command_interval_seconds": self._command_interval_seconds,
                    "command_loop_running": command_loop_running,
                    "command_loop_iterations": self._command_loop_iterations,
                    "command_frame_poll_count": self._command_frame_poll_count,
                    "command_send_attempts": self._command_send_attempts,
                    "command_write_count": self._command_write_count,
                    "last_command_write_size": self._last_command_write_size,
                    "command_loop_error_count": self._command_loop_error_count,
                    "roast_fan_level_percent": self._roast_fan_level_percent,
                    "main_fan_level_percent": self._main_fan_level_percent,
                    "solenoid_open": self._solenoid_open,
                    "drum_motor_on": self._drum_motor_on,
                    "cooling_motor_on": self._cooling_motor_on,
                },
            )

    def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
        """Read command lifecycle state under one state-lock hold."""
        with self._state_lock:
            serial_open = self._serial is not None and self._serial.is_open
            return DriverLifecycleEvidence(
                driver=self.name,
                connected=self._connected,
                command_streaming_required=True,
                command_loop_running=(
                    self._command_thread is not None and self._command_thread.is_alive()
                ),
                serial_open=serial_open,
                heat_level_percent=self._heat_level_percent,
                roast_fan_level_percent=self._roast_fan_level_percent,
                main_fan_level_percent=self._main_fan_level_percent,
                drum_motor_on=self._drum_motor_on,
                cooling_motor_on=self._cooling_motor_on,
                solenoid_open=self._solenoid_open,
                command_send_attempts=self._command_send_attempts,
                command_write_count=self._command_write_count,
                last_command_write_size=self._last_command_write_size,
                command_loop_error_count=self._command_loop_error_count,
                status_packet_count=self._status_packet_count,
                status_read_error_count=self._status_read_error_count,
            )

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Set Hottop heater command state and return normalized state."""
        heat_level = validate_control_percent(
            heat_level_percent,
            label="heat_level_percent",
        )
        with self._state_lock:
            self._heat_level_percent = heat_level
            self._command_state_revision += 1
            if heat_level > 0:
                self._drum_motor_on = True
        return self.read_state()

    def contain_heat_fixed_zero(self) -> FixedZeroCommandedState:
        """Set only Hottop commanded heat to zero and return command evidence."""
        with self._state_lock:
            self._heat_level_percent = 0
            self._command_state_revision += 1
            return FixedZeroCommandedState(
                driver=self.name,
                capability_version=1,
                heat_level_percent=self._heat_level_percent,
                roast_fan_level_percent=self._roast_fan_level_percent,
                main_fan_level_percent=self._main_fan_level_percent,
                drum_motor_on=self._drum_motor_on,
                cooling_motor_on=self._cooling_motor_on,
                solenoid_open=self._solenoid_open,
            )

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Set Hottop main-fan command state and return normalized state."""
        fan_level = validate_control_percent(
            fan_level_percent,
            label="fan_level_percent",
        )
        with self._state_lock:
            self._main_fan_level_percent = fan_level
            self._command_state_revision += 1
        return self.read_state()

    def drop_beans(self) -> RoasterState:
        """Apply Hottop compound bean-drop command state."""
        with self._state_lock:
            if self._hottop_reception == "candidate/v1" and (
                self._solenoid_open
                or (
                    self._drop_settlement is not None
                    and self._drop_settlement.result in {"pending", "entering", "unknown"}
                )
            ):
                raise HottopDropRefused("candidate Drop retains an unresolved command settlement.")
            self._heat_level_percent = 0
            # Keep the drum motor RUNNING through the drop so the rotating drum
            # tumbles beans out the open chute. A stopped drum traps ~half the
            # charge below the chute (coffee-roaster-mcp#163). stop_cooling is
            # the end-of-roast action that stops the drum once beans are clear.
            self._drum_motor_on = True
            self._solenoid_open = True
            self._cooling_motor_on = True
            self._main_fan_level_percent = 100
            self._command_state_revision += 1
            if self._hottop_reception == "candidate/v1":
                self._drop_transition_count += 1
                self._drop_settlement = HottopCommandSettlement(
                    transition="drop",
                    transition_seq=self._drop_transition_count,
                    transition_revision=self._command_state_revision,
                    transition_epoch=self._reception_epoch,
                    result="pending",
                )
        return self.read_state()

    def start_cooling(self) -> RoasterState:
        """Start Hottop cooling motor and main fan."""
        with self._state_lock:
            self._cooling_motor_on = True
            self._main_fan_level_percent = 100
            self._command_state_revision += 1
        return self.read_state()

    def stop_cooling(self) -> RoasterState:
        """Stop Hottop cooling, close drop path, stop the drum, and clear main fan."""
        with self._state_lock:
            self._cooling_motor_on = False
            self._solenoid_open = False
            # drop_beans now leaves the drum running through cooling to fully
            # clear the beans (coffee-roaster-mcp#163). stop_cooling is the
            # end-of-roast action, so it must stop the drum here; otherwise the
            # drum would spin indefinitely after cooling ends.
            self._drum_motor_on = False
            self._main_fan_level_percent = 0
            self._command_state_revision += 1
            self._close_unattempted_drop_settlement()
        return self.read_state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Apply safe Hottop emergency-stop command state."""
        _ = reason
        with self._state_lock:
            self._heat_level_percent = 0
            self._drum_motor_on = False
            self._solenoid_open = False
            self._cooling_motor_on = True
            self._main_fan_level_percent = 100
            self._command_state_revision += 1
            if self._hottop_reception == "candidate/v1":
                self._emergency_count += 1
                self._emergency_settlement = HottopCommandSettlement(
                    transition="emergency",
                    transition_seq=self._emergency_count,
                    transition_revision=self._command_state_revision,
                    transition_epoch=self._reception_epoch,
                    result="pending",
                )
                self._close_unattempted_drop_settlement()
            heat_level_percent = self._heat_level_percent
            fan_level_percent = self._main_fan_level_percent
            cooling_on = self._cooling_motor_on
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=heat_level_percent,
            fan_level_percent=fan_level_percent,
            cooling_on=cooling_on,
        )

    def _command_loop(self) -> None:
        """Stream current Hottop command frames until disconnect requests shutdown."""
        while not self._stop_event.wait(self._command_interval_seconds):
            self._send_command_frame()
            self._run_command_loop_iteration_hook()

    def _run_command_loop_iteration_hook(self) -> None:
        """Run the optional test hook without letting it kill the loop."""
        if self._command_loop_iteration_hook is None:
            return
        try:
            self._command_loop_iteration_hook()
        except Exception:
            serial_transport: SerialTransport | None
            with self._state_lock:
                self._command_loop_error_count += 1
                self._connected = False
                self._stop_event.set()
                serial_transport = self._serial
            if serial_transport is not None and serial_transport.is_open:
                self._close_serial_transport(serial_transport)

    def _send_command_frame(self) -> None:
        """Send one command-frame tick if packet construction is available."""
        if self._hottop_reception == "candidate/v1":
            self._send_command_frame_candidate()
            return
        try:
            with self._state_lock:
                self._command_loop_iterations += 1
                self._command_frame_poll_count += 1
            frame = self._command_frame_provider()
            if frame is None:
                return
            with self._command_write_lock:
                with self._state_lock:
                    serial_transport = self._serial
                    stop_requested = self._stop_event.is_set()
                    if (
                        stop_requested
                        or self._disconnect_requested.is_set()
                        or not self._connected
                        or serial_transport is None
                        or not serial_transport.is_open
                    ):
                        return
                    self._command_send_attempts += 1
                bytes_written = serial_transport.write(frame)
                with self._state_lock:
                    self._last_command_write_size = bytes_written
                    if bytes_written == len(frame):
                        self._command_write_count += 1
                    else:
                        self._command_loop_error_count += 1
            self._read_status_packet(serial_transport)
        except Exception:
            with self._state_lock:
                self._command_loop_error_count += 1
                self._last_command_write_size = 0

    def _send_command_frame_candidate(self) -> None:
        """Send one candidate frame after atomically recording its host entry evidence."""
        try:
            with self._state_lock:
                self._command_loop_iterations += 1
                self._command_frame_poll_count += 1
            with self._command_write_lock:
                with self._state_lock:
                    serial_transport = self._serial
                    if (
                        self._stop_event.is_set()
                        or self._disconnect_requested.is_set()
                        or not self._connected
                        or serial_transport is None
                        or not serial_transport.is_open
                    ):
                        return
                    revision = self._command_state_revision
                    frame = build_hottop_command_packet(
                        heat_level_percent=self._heat_level_percent,
                        fan_level_percent=self._roast_fan_level_percent,
                        main_fan_level_percent=self._main_fan_level_percent,
                        solenoid_open=self._solenoid_open,
                        drum_motor_on=self._drum_motor_on,
                        cooling_motor_on=self._cooling_motor_on,
                    )
                    self._command_send_attempts += 1
                    self._attempt_sequence += 1
                    role = self._candidate_attempt_role(frame)
                    attempt = HottopWriteAttempt(
                        epoch=self._reception_epoch,
                        handle_identity=id(serial_transport),
                        attempt_seq=self._attempt_sequence,
                        revision=revision,
                        frame=frame,
                        solenoid=frame[_HOTTOP_COMMAND_SOLENOID_INDEX],
                        result="entering",
                        role=role,
                    )
                    self._append_write_attempt(attempt)
                    self._settle_entering_attempt(attempt)
                try:
                    receipt = serial_transport.write(frame)
                except BaseException:
                    with self._state_lock:
                        self._last_command_write_size = 0
                        self._settle_attempt(attempt, "unknown")
                    raise
                with self._state_lock:
                    self._last_command_write_size = receipt if type(receipt) is int else 0
                    result: HottopWriteResult = (
                        "accepted_by_host"
                        if type(receipt) is int and receipt == len(frame) == HOTTOP_PACKET_LENGTH
                        else "unknown"
                    )
                    if result == "accepted_by_host":
                        self._command_write_count += 1
                    else:
                        self._command_loop_error_count += 1
                    self._settle_attempt(attempt, result)
            self._read_status_packet_candidate(serial_transport)
        except Exception:
            with self._state_lock:
                self._command_loop_error_count += 1

    def _candidate_attempt_role(self, frame: bytes) -> HottopAttemptRole:
        """Classify a candidate attempt without treating repeated frames as evidence."""
        if frame[_HOTTOP_COMMAND_SOLENOID_INDEX] == 1:
            if self._drop_settlement is not None and self._drop_settlement.result == "pending":
                return "drop"
            return "repeat"
        if (
            self._emergency_settlement is not None
            and self._emergency_settlement.result == "pending"
        ):
            return "emergency"
        return "ordinary"

    def _append_write_attempt(self, attempt: HottopWriteAttempt) -> None:
        """Retain a bounded current-operation attempt history."""
        self._attempt_history.append(attempt)
        if len(self._attempt_history) > 64:
            del self._attempt_history[0]

    def _settle_entering_attempt(self, attempt: HottopWriteAttempt) -> None:
        """Fix the first eligible transition attempt at host write entry."""
        if attempt.role == "drop" and self._drop_settlement is not None:
            self._drop_settlement = replace(
                self._drop_settlement,
                result="entering",
                attempt_epoch=attempt.epoch,
                attempt_handle_identity=attempt.handle_identity,
                attempt_seq=attempt.attempt_seq,
                attempt_revision=attempt.revision,
            )
        elif attempt.role == "emergency" and self._emergency_settlement is not None:
            self._emergency_settlement = replace(
                self._emergency_settlement,
                result="entering",
                attempt_epoch=attempt.epoch,
                attempt_handle_identity=attempt.handle_identity,
                attempt_seq=attempt.attempt_seq,
                attempt_revision=attempt.revision,
            )

    def _settle_attempt(self, attempt: HottopWriteAttempt, result: HottopWriteResult) -> None:
        """Finalize only the attempt that fixed a settlement's entry identity."""
        for attribute in ("_drop_settlement", "_emergency_settlement"):
            settlement = cast(HottopCommandSettlement | None, getattr(self, attribute))
            if settlement is None or settlement.attempt_seq != attempt.attempt_seq:
                continue
            setattr(self, attribute, replace(settlement, result=result))
        for index, recorded in enumerate(self._attempt_history):
            if recorded.attempt_seq == attempt.attempt_seq:
                self._attempt_history[index] = replace(recorded, result=result)
                break

    def _close_unattempted_drop_settlement(self) -> None:
        """Finalize a pending Drop only while closing its commanded solenoid state."""
        if self._hottop_reception != "candidate/v1" or self._drop_settlement is None:
            return
        if self._drop_settlement.result == "pending":
            self._drop_settlement = replace(self._drop_settlement, result="not_attempted")

    def _read_status_packet(self, serial_transport: SerialTransport) -> None:
        """Read available Hottop status bytes and update normalized temperatures."""
        try:
            waiting = getattr(serial_transport, "in_waiting", 0)
            if not isinstance(waiting, int) or waiting <= 0:
                return
            read = getattr(serial_transport, "read", None)
            if not callable(read):
                return
            chunk = read(min(waiting, _HOTTOP_STATUS_READ_MAX_BYTES))
            if not isinstance(chunk, bytes) or len(chunk) == 0:
                return
            with self._state_lock:
                status_buffer = self._status_buffer + chunk
                temperature_unit = self._temperature_unit
            statuses, remaining_buffer = _parse_hottop_status_buffer(
                status_buffer,
                temperature_unit=temperature_unit,
            )
            with self._state_lock:
                self._status_buffer = remaining_buffer
                for status in statuses:
                    self._status_packet_count += 1
                    self._latest_raw_bean_temperature = status.raw_bean_temperature
                    self._latest_raw_env_temperature = status.raw_env_temperature
                    if status.bean_temp_c is None or status.env_temp_c is None:
                        self._ignored_temperature_packet_count += 1
                        self._latest_resolved_temperature_unit = status.resolved_temperature_unit
                        continue
                    self._latest_bean_temp_c = status.bean_temp_c
                    self._latest_env_temp_c = status.env_temp_c
                    self._latest_resolved_temperature_unit = status.resolved_temperature_unit
        except Exception:
            with self._state_lock:
                self._status_read_error_count += 1

    def _begin_reception_epoch(self, handle: SerialTransport) -> None:
        """Start a receive epoch while the new handle is atomically published."""
        self._reception_epoch += 1
        self._status_buffer = b""
        self._reception_floor = None
        self._reception_record = None
        _ = handle

    def _read_status_packet_candidate(self, serial_transport: SerialTransport) -> None:
        """Read one bounded simulated candidate chunk with host-receipt provenance."""
        with self._state_lock:
            epoch = self._reception_epoch
            if self._serial is not serial_transport:
                self._reception_discard_count += 1
                return
            prior_tail = self._status_buffer
            temperature_unit = self._temperature_unit

        started_at = self._reception_clock()
        queued_before = self._candidate_queue_probe(serial_transport)
        if queued_before == 0:
            self._publish_drained_floor(serial_transport, epoch, started_at)
            post_probe_time = self._reception_clock()
            if self._candidate_queue_probe(serial_transport) == 0:
                self._publish_drained_floor(serial_transport, epoch, post_probe_time)
            return
        read = getattr(serial_transport, "read", None)
        if not callable(read):
            _ = self._reception_clock()
            self._candidate_queue_probe(serial_transport)
            return
        requested = (
            min(queued_before, _HOTTOP_STATUS_READ_MAX_BYTES)
            if queued_before is not None and queued_before > 0
            else _HOTTOP_STATUS_READ_MAX_BYTES
        )
        try:
            chunk = read(requested)
        except Exception:
            with self._state_lock:
                self._status_read_error_count += 1
            _ = self._reception_clock()
            self._candidate_queue_probe(serial_transport)
            return
        returned_at = self._reception_clock()
        post_probe_time = self._reception_clock()
        queued_after = self._candidate_queue_probe(serial_transport)
        if not isinstance(chunk, bytes):
            chunk = b""
        scan = scan_hottop_status_read(
            chunk,
            prior_tail=prior_tail,
            queued_before_read=queued_before,
            requested_byte_count=requested,
            temperature_unit=temperature_unit,
        )
        with self._state_lock:
            if self._serial is not serial_transport or self._reception_epoch != epoch:
                self._reception_discard_count += 1
                return
            self._reception_sequence += 1
            receipt = build_hottop_received_packet(
                epoch=epoch,
                handle_identity=id(serial_transport),
                sequence=self._reception_sequence,
                read_started_at=started_at,
                read_returned_at=returned_at,
                configured_temperature_unit=temperature_unit,
                queued_before_read=queued_before,
                queued_after_read=queued_after,
                scan=scan,
                drained_floor=self._reception_floor,
            )
            self._status_buffer = (
                (prior_tail + chunk)[-scan.tail_length :] if scan.tail_length else b""
            )
            self._reception_record = receipt
            for status in scan.packets:
                self._status_packet_count += 1
                self._latest_raw_bean_temperature = status.raw_bean_temperature
                self._latest_raw_env_temperature = status.raw_env_temperature
                if status.bean_temp_c is None or status.env_temp_c is None:
                    self._ignored_temperature_packet_count += 1
                else:
                    self._latest_bean_temp_c = status.bean_temp_c
                    self._latest_env_temp_c = status.env_temp_c
                self._latest_resolved_temperature_unit = status.resolved_temperature_unit
        if queued_after == 0:
            self._publish_drained_floor(serial_transport, epoch, post_probe_time)

    def _candidate_queue_probe(self, serial_transport: SerialTransport) -> int | None:
        """Return only an exact non-negative queue observation from a fake transport."""
        try:
            value: object = getattr(serial_transport, "in_waiting", None)
            return _queue_observation_or_none(value)
        except Exception:
            return None

    def _publish_drained_floor(
        self, serial_transport: SerialTransport, epoch: int, observed_at: float
    ) -> None:
        """Publish a floor only for the still-current empty parser and queue state."""
        with self._state_lock:
            if (
                self._serial is serial_transport
                and self._reception_epoch == epoch
                and not self._status_buffer
            ):
                self._reception_floor = HottopDrainedFloor(
                    epoch=epoch,
                    handle_identity=id(serial_transport),
                    sequence=self._reception_sequence,
                    observed_at=observed_at,
                )

    def _current_command_frame(self) -> bytes:
        """Return the current Hottop command-state packet."""
        with self._state_lock:
            return build_hottop_command_packet(
                heat_level_percent=self._heat_level_percent,
                fan_level_percent=self._roast_fan_level_percent,
                main_fan_level_percent=self._main_fan_level_percent,
                solenoid_open=self._solenoid_open,
                drum_motor_on=self._drum_motor_on,
                cooling_motor_on=self._cooling_motor_on,
            )


def _percent_to_hottop_fan_scale(value: int) -> int:
    """Map normalized fan percentage to the Hottop 0-10 byte scale."""
    return (value + 5) // 10


def _normalize_hottop_temperatures(
    *,
    raw_bean_temperature: int,
    raw_env_temperature: int,
    temperature_unit: HottopTemperatureUnit,
) -> tuple[float | None, float | None, ResolvedHottopTemperatureUnit | None]:
    """Normalize raw Hottop temperature readings to plausible Celsius values."""
    if temperature_unit == "celsius":
        return _normalize_explicit_hottop_temperatures(
            raw_bean_temperature=raw_bean_temperature,
            raw_env_temperature=raw_env_temperature,
            resolved_unit="celsius",
        )
    if temperature_unit == "fahrenheit":
        return _normalize_explicit_hottop_temperatures(
            raw_bean_temperature=raw_bean_temperature,
            raw_env_temperature=raw_env_temperature,
            resolved_unit="fahrenheit",
        )

    celsius_values = _normalize_explicit_hottop_temperatures(
        raw_bean_temperature=raw_bean_temperature,
        raw_env_temperature=raw_env_temperature,
        resolved_unit="celsius",
    )
    if celsius_values[2] is not None:
        return celsius_values
    return _normalize_explicit_hottop_temperatures(
        raw_bean_temperature=raw_bean_temperature,
        raw_env_temperature=raw_env_temperature,
        resolved_unit="fahrenheit",
    )


def _normalize_explicit_hottop_temperatures(
    *,
    raw_bean_temperature: int,
    raw_env_temperature: int,
    resolved_unit: ResolvedHottopTemperatureUnit,
) -> tuple[float | None, float | None, ResolvedHottopTemperatureUnit | None]:
    """Normalize one explicit Hottop raw unit mode."""
    bean_temp_c = _normalize_hottop_temperature(
        raw_bean_temperature,
        resolved_unit=resolved_unit,
    )
    env_temp_c = _normalize_hottop_temperature(
        raw_env_temperature,
        resolved_unit=resolved_unit,
    )
    if bean_temp_c is None or env_temp_c is None:
        return None, None, None
    return bean_temp_c, env_temp_c, resolved_unit


def _normalize_hottop_temperature(
    raw_temperature: int,
    *,
    resolved_unit: ResolvedHottopTemperatureUnit,
) -> float | None:
    """Normalize one raw Hottop temperature reading to plausible Celsius."""
    if resolved_unit == "fahrenheit":
        temperature_c = (raw_temperature - 32.0) * 5.0 / 9.0
    else:
        temperature_c = float(raw_temperature)
    if not (_HOTTOP_MIN_PLAUSIBLE_TEMP_C <= temperature_c <= _HOTTOP_MAX_PLAUSIBLE_TEMP_C):
        return None
    return round(temperature_c, 1)


def _validate_hottop_temperature_unit(value: object) -> HottopTemperatureUnit:
    """Validate a configured Hottop temperature unit mode."""
    if not isinstance(value, str):
        raise TypeError("temperature_unit must be a string.")
    normalized = value.strip().lower()
    if normalized not in {"celsius", "fahrenheit", "auto"}:
        raise ValueError("temperature_unit must be one of: celsius, fahrenheit, auto.")
    return cast(HottopTemperatureUnit, normalized)


def _validate_hottop_packet(packet: bytes) -> None:
    """Validate exact Hottop status packet length, prefix, and checksum."""
    if len(packet) != HOTTOP_PACKET_LENGTH:
        raise ValueError(f"Hottop packet must be {HOTTOP_PACKET_LENGTH} bytes.")
    if not packet.startswith(HOTTOP_PACKET_PREFIX):
        raise ValueError("Hottop packet must start with A5 96.")
    if is_hottop_command_packet(packet):
        raise ValueError("Hottop status packet must not use the command header.")
    if not validate_hottop_packet_checksum(packet):
        raise ValueError("Hottop packet checksum is invalid.")


def _read_big_endian_uint16(packet: bytes, *, high_index: int, low_index: int) -> int:
    """Read one unsigned big-endian 16-bit value from packet bytes."""
    return packet[high_index] * 256 + packet[low_index]


def _clamp_float(value: float, *, minimum: float, maximum: float) -> float:
    """Clamp a float and keep telemetry output stable at one decimal place."""
    return round(max(minimum, min(maximum, value)), 1)


def _validate_exact_bool(value: object, *, label: str) -> None:
    """Validate that a state field is exactly a bool."""
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean.")


def _is_nonnegative_exact_int(value: object) -> bool:
    """Return whether a value is an exact non-negative integer, excluding bool."""
    return type(value) is int and value >= 0


def _queue_observation_or_none(value: object) -> int | None:
    """Retain an exact queue observation without preserving an arbitrary object."""
    return cast(int, value) if _is_nonnegative_exact_int(value) else None


def _validate_nonnegative_exact_int(value: object, *, label: str) -> None:
    """Validate one exact non-negative integer field."""
    if not _is_nonnegative_exact_int(value):
        raise ValueError(f"{label} must be a non-negative exact integer.")


def _validate_positive_exact_int(value: object, *, label: str) -> None:
    """Validate one exact positive integer bound."""
    if not _is_nonnegative_exact_int(value) or value == 0:
        raise ValueError(f"{label} must be a positive exact integer.")


def _validate_finite_time(value: object, *, label: str) -> None:
    """Validate one finite host-clock observation without reading a clock."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{label} must be a finite host-clock time.")


def _validate_positive_finite_bound(value: object, *, label: str) -> None:
    """Validate one finite positive host-receipt age bound."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be a finite positive bound.")


def _validate_driver_name(value: object) -> None:
    """Validate the stable driver identifier on normalized state."""
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("driver must be a non-empty string.")


def _validate_optional_temperature_c(value: object, *, label: str) -> float | None:
    """Validate one normalized Celsius temperature value."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite Celsius temperature or None.")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{label} must be finite.")
    return normalized


def _validate_raw_vendor_data(
    raw_vendor_data: object,
) -> dict[str, EventPayloadValue]:
    """Validate and copy raw vendor diagnostics for normalized state."""
    if not isinstance(raw_vendor_data, dict):
        raise TypeError("raw_vendor_data must be a dictionary.")
    raw_vendor_mapping = cast(dict[object, object], raw_vendor_data)
    copied: dict[str, EventPayloadValue] = {}
    allowed_value_types = (str, int, float, bool, type(None))
    for key, value in raw_vendor_mapping.items():
        if not isinstance(key, str):
            raise TypeError("raw_vendor_data keys must be strings.")
        if not isinstance(value, allowed_value_types):
            raise TypeError(
                "raw_vendor_data values must be strings, integers, floats, booleans, or None."
            )
        copied[key] = value
    return copied


def _create_pyserial_transport(*args: object, **kwargs: object) -> SerialTransport:
    """Create a pyserial transport lazily for the Hottop driver."""
    serial_module = import_module("serial")
    serial_class = serial_module.Serial
    transport = serial_class(*args, **kwargs)
    return cast(SerialTransport, transport)


def create_roaster_driver(
    driver_name: str,
    *,
    port: str | None = None,
    baudrate: int = 115_200,
    temperature_unit: HottopTemperatureUnit = "celsius",
    command_interval_seconds: float = 0.3,
    hottop_reception: HottopReceptionMode = "off",
    serial_factory: SerialTransportFactory | None = None,
) -> RoasterDriver:
    """Create the configured roaster driver.

    Args:
        driver_name: Roaster driver name from configuration.
        port: Optional roaster serial port.
        baudrate: Configured roaster baudrate.
        temperature_unit: Configured roaster temperature unit.
        command_interval_seconds: Configured command interval.
        hottop_reception: Opt-in Hottop receipt candidate mode.
        serial_factory: Trusted injected simulated serial transport factory.

    Returns:
        Driver adapter for the configured driver.

    Raises:
        ValueError: If the driver is not implemented in the current runtime.
    """
    if driver_name == "mock":
        if hottop_reception == "candidate/v1":
            raise ValueError("candidate/v1 is not supported by the mock driver.")
        return MockRoasterDriver()
    if driver_name == HOTTOP_DRIVER_NAME:
        return HottopRoasterDriver(
            port=port,
            baudrate=baudrate,
            temperature_unit=temperature_unit,
            command_interval_seconds=command_interval_seconds,
            hottop_reception=hottop_reception,
            serial_factory=serial_factory,
        )
    raise ValueError(
        f"Roaster driver {driver_name!r} is not implemented in this bootstrap runtime."
    )


def create_roaster_safety_driver(driver_name: str) -> RoasterDriver:
    """Create the configured roaster driver for E2 compatibility."""
    return create_roaster_driver(driver_name)
