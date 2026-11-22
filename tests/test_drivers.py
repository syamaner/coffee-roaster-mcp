"""Roaster driver contract, capability, and safety behavior coverage."""

from collections.abc import Callable
from dataclasses import replace
from enum import IntEnum
from threading import Event, Lock, Thread, get_ident
from time import monotonic, sleep
from typing import Any, Never, cast

import pytest

import coffee_roaster_mcp.drivers as drivers_module
from coffee_roaster_mcp.cold_temperature import ColdTemperatureProjection, project_cold_temperature
from coffee_roaster_mcp.drivers import (
    HOTTOP_DRIVER_NAME,
    HOTTOP_PACKET_LENGTH,
    HOTTOP_PACKET_PREFIX,
    CommandStreaming,
    DriverLifecycleEvidence,
    ExactDropArbitration,
    FixedZeroCommandedState,
    HottopDrainedFloor,
    HottopDropRefused,
    HottopExactDropArbitration,
    HottopExactDropRefused,
    HottopFirstWriteDisposition,
    HottopReceiptClassification,
    HottopReceivedPacket,
    HottopRoasterDriver,
    HottopStartOwner,
    HottopStartPhase,
    HottopStartSettlement,
    HottopStatusReadScan,
    HottopTemperatureUnit,
    MockRoasterDriver,
    RoasterCapabilities,
    RoasterDriver,
    RoasterState,
    SerialTransport,
    SerialTransportFactory,
    build_hottop_command_packet,
    build_hottop_received_packet,
    calculate_hottop_packet_checksum,
    classify_hottop_received_packet,
    create_roaster_driver,
    create_roaster_safety_driver,
    find_hottop_status_packet,
    is_hottop_command_packet,
    parse_hottop_status_packet,
    scan_hottop_status_read,
    validate_hottop_packet_checksum,
)


class FakeSerialTransport:
    """Mock serial transport for Hottop lifecycle tests."""

    def __init__(self) -> None:
        self.is_open = True
        self.close_calls = 0
        self.closed = Event()
        self.writes: list[bytes] = []
        self.read_sizes: list[int] = []
        self._read_buffer = bytearray()
        self._write_lock = Lock()
        self._read_lock = Lock()

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False
        self.closed.set()

    def write(self, data: bytes) -> int:
        with self._write_lock:
            self.writes.append(data)
        return len(data)

    @property
    def in_waiting(self) -> int:
        """Return the number of queued bytes available for reads."""
        with self._read_lock:
            return len(self._read_buffer)

    def queue_read_data(self, data: bytes) -> None:
        """Queue bytes that the fake transport will return from read."""
        with self._read_lock:
            self._read_buffer.extend(data)

    def read(self, size: int) -> bytes:
        """Read queued bytes from the fake transport."""
        with self._read_lock:
            self.read_sizes.append(size)
            chunk = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            return chunk

    def writes_snapshot(self) -> list[bytes]:
        """Return command writes recorded by the fake transport."""
        with self._write_lock:
            return list(self.writes)


class FailingSerialTransport(FakeSerialTransport):
    """Fake serial transport that raises on write."""

    def write(self, data: bytes) -> int:
        _ = data
        raise OSError("serial write failed")


class OneSuccessThenFailSerialTransport(FakeSerialTransport):
    """Fake serial transport that succeeds once and then raises."""

    def write(self, data: bytes) -> int:
        with self._write_lock:
            self.writes.append(data)
            if len(self.writes) == 1:
                return len(data)
        raise OSError("serial write failed")


class PartialWriteSerialTransport(FakeSerialTransport):
    """Fake serial transport that records partial writes."""

    def write(self, data: bytes) -> int:
        with self._write_lock:
            self.writes.append(data)
        return len(data) - 1


class BlockingWriteSerialTransport(FakeSerialTransport):
    """Fake serial transport that blocks inside write until released."""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def write(self, data: bytes) -> int:
        with self._write_lock:
            self.writes.append(data)
        self.started.set()
        self.release.wait(timeout=1.0)
        return len(data)


class BlockingWriteFailingCloseTransport(BlockingWriteSerialTransport):
    """Blocking write transport whose first close attempt raises."""

    def __init__(self) -> None:
        super().__init__()
        self._close_failures_remaining = 1

    def close(self) -> None:
        self.close_calls += 1
        if self._close_failures_remaining > 0:
            self._close_failures_remaining -= 1
            raise OSError("serial close failed")
        self.is_open = False
        self.closed.set()


class RaisingHook:
    """Command-loop hook that raises after proving it ran."""

    def __init__(self) -> None:
        self.called = Event()

    def __call__(self) -> None:
        self.called.set()
        raise RuntimeError("hook failed")


class LoopIterationProbe:
    """Thread-safe command-loop iteration probe for deterministic tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._target = 0
        self._calls = 0
        self._target_reached = Event()

    def __call__(self) -> None:
        with self._lock:
            self._calls += 1
            if self._target > 0 and self._calls >= self._target:
                self._target_reached.set()

    def wait_for_calls(self, count: int, *, timeout: float = 1.0) -> bool:
        """Wait until the command-loop hook has been called enough times."""
        with self._lock:
            if self._calls >= count:
                return True
            self._target = count
            self._target_reached.clear()
        return self._target_reached.wait(timeout=timeout)


class BlockingFrameProvider:
    """Command-frame provider that blocks until the test releases it."""

    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.started = Event()
        self.release = Event()

    def __call__(self) -> bytes:
        self.started.set()
        self.release.wait(timeout=1.0)
        return self.frame


class StopAwareFrameProvider:
    """Command-frame provider that lets the test request disconnect before write."""

    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.started = Event()
        self.release = Event()

    def __call__(self) -> bytes:
        self.started.set()
        self.release.wait(timeout=1.0)
        return self.frame


class FakeSerialFactory:
    """Callable serial factory that records constructor arguments."""

    def __init__(self, transport: FakeSerialTransport | None = None) -> None:
        self.transport = transport or FakeSerialTransport()
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeSerialTransport:
        self.calls.append((args, kwargs))
        return self.transport


class SequentialSerialFactory:
    """Factory that returns its supplied fake transports in connection order."""

    def __init__(self, transports: list[FakeSerialTransport]) -> None:
        self.transports = transports
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeSerialTransport:
        self.calls.append((args, kwargs))
        return self.transports[len(self.calls) - 1]


class BlockingSerialFactory:
    """Serial factory that blocks until the test releases it."""

    def __init__(self) -> None:
        self.transport = FakeSerialTransport()
        self.started = Event()
        self.release = Event()

    def __call__(self, *args: object, **kwargs: object) -> FakeSerialTransport:
        _ = args, kwargs
        self.started.set()
        self.release.wait(timeout=1.0)
        return self.transport


class StuckHottopRoasterDriver(HottopRoasterDriver):
    """Hottop test driver with a command loop that ignores stop signals."""

    def __init__(
        self,
        *,
        port: str,
        command_interval_seconds: float,
        join_timeout_seconds: float,
        serial_factory: SerialTransportFactory,
    ) -> None:
        super().__init__(
            port=port,
            command_interval_seconds=command_interval_seconds,
            join_timeout_seconds=join_timeout_seconds,
            serial_factory=serial_factory,
        )
        self._release_command_loop = Event()

    def release_command_loop(self) -> None:
        """Allow the test command loop to exit."""
        self._release_command_loop.set()

    def _command_loop(
        self,
        admission: Event | None = None,
        expected_transport: SerialTransport | None = None,
    ) -> None:
        """Keep running so disconnect timeout handling can be tested."""
        _ = admission
        _ = expected_transport
        self._release_command_loop.wait()


def _build_hottop_status_packet(
    *,
    raw_env_temperature: int,
    raw_bean_temperature: int,
) -> bytes:
    """Build a minimal valid Hottop status packet for parser tests."""
    packet = bytearray(HOTTOP_PACKET_LENGTH)
    packet[: len(HOTTOP_PACKET_PREFIX)] = HOTTOP_PACKET_PREFIX
    packet[23] = raw_env_temperature // 256
    packet[24] = raw_env_temperature % 256
    packet[25] = raw_bean_temperature // 256
    packet[26] = raw_bean_temperature % 256
    packet[35] = calculate_hottop_packet_checksum(packet)
    return bytes(packet)


def _build_received_packet(
    *,
    scan: HottopStatusReadScan,
    queued_before_read: object,
    queued_after_read: object,
    configured_temperature_unit: HottopTemperatureUnit = "celsius",
    sequence: int = 2,
) -> HottopReceivedPacket:
    """Build one synthetic host-receipt candidate from a pure scanner result."""
    return build_hottop_received_packet(
        epoch=4,
        handle_identity=9,
        sequence=sequence,
        read_started_at=11.0,
        read_returned_at=12.0,
        configured_temperature_unit=configured_temperature_unit,
        queued_before_read=queued_before_read,
        queued_after_read=queued_after_read,
        scan=scan,
        drained_floor=HottopDrainedFloor(
            epoch=4,
            handle_identity=9,
            sequence=1,
            observed_at=10.0,
        ),
    )


def _classify_received_packet(
    receipt: HottopReceivedPacket | None,
    *,
    candidate_configured: bool = True,
    current_sequence: int = 2,
    now: float = 13.0,
    receipt_age_bound_seconds: float | None = 3.0,
) -> HottopReceiptClassification:
    """Classify one synthetic receipt using only injected host-clock values."""
    return classify_hottop_received_packet(
        receipt,
        candidate_configured=candidate_configured,
        current_epoch=4,
        current_handle_identity=9,
        current_sequence=current_sequence,
        now=now,
        receipt_age_bound_seconds=receipt_age_bound_seconds,
    )


def test_hottop_received_packet_classifier_accepts_only_complete_bounded_newest_pair() -> None:
    """A fully drained bounded read accepts its newest contiguous packet pair only."""
    reads = b"".join(
        (
            _build_hottop_status_packet(raw_env_temperature=199, raw_bean_temperature=194),
            _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
            _build_hottop_status_packet(raw_env_temperature=201, raw_bean_temperature=196),
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
        )
    )
    scan = scan_hottop_status_read(
        reads,
        queued_before_read=144,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=144,
        queued_after_read=0,
    )

    result = _classify_received_packet(receipt)

    assert scan.packet_offsets == (0, 36, 72, 108)
    assert scan.skipped_byte_count == 0
    assert scan.tail_length == 0
    assert scan.read_truncated is False
    assert receipt.latest_pair_c == (197.0, 202.0)
    assert result.eligible is True
    assert result.reasons == ()
    assert result.drained_floor_age_seconds == 3.0
    assert result.read_returned_age_seconds == 1.0
    assert result.sensor_age == "unknown"


def test_hottop_received_packet_classifier_rejects_backlog_and_supersedes_old_receipt() -> None:
    """A clipped read is rejected, while its later complete successor supersedes it."""
    first_read = b"".join(
        _build_hottop_status_packet(
            raw_env_temperature=197 + index,
            raw_bean_temperature=192 + index,
        )
        for index in range(4)
    )
    backlog_scan = scan_hottop_status_read(
        first_read,
        queued_before_read=216,
        requested_byte_count=144,
    )
    backlog_receipt = _build_received_packet(
        scan=backlog_scan,
        queued_before_read=216,
        queued_after_read=72,
    )

    backlog_result = _classify_received_packet(backlog_receipt)

    assert backlog_scan.read_truncated is True
    assert backlog_receipt.latest_pair_c == (195.0, 200.0)
    assert backlog_result.reasons == ("read_truncated", "backlog_visible")

    successor_read = b"".join(
        (
            _build_hottop_status_packet(raw_env_temperature=201, raw_bean_temperature=196),
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
        )
    )
    successor_scan = scan_hottop_status_read(
        successor_read,
        queued_before_read=72,
        requested_byte_count=144,
    )
    successor_receipt = _build_received_packet(
        scan=successor_scan,
        queued_before_read=72,
        queued_after_read=0,
        sequence=3,
    )

    stale_result = _classify_received_packet(backlog_receipt, current_sequence=3)
    successor_result = _classify_received_packet(successor_receipt, current_sequence=3)

    assert stale_result.primary_reason == "superseded"
    assert successor_receipt.latest_pair_c == (197.0, 202.0)
    assert successor_result.eligible is True


@pytest.mark.parametrize(
    ("prior_tail", "read_bytes", "expected_tail_length"),
    [
        (
            b"",
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)
            + _build_hottop_status_packet(raw_env_temperature=203, raw_bean_temperature=198)[:12],
            12,
        ),
        (
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)[:12],
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)[12:],
            0,
        ),
    ],
)
def test_hottop_received_packet_classifier_rejects_prior_or_trailing_parser_remainder(
    prior_tail: bytes,
    read_bytes: bytes,
    expected_tail_length: int,
) -> None:
    """Parser ownership is incomplete for both carried and trailing partial frames."""
    scan = scan_hottop_status_read(
        read_bytes,
        prior_tail=prior_tail,
        queued_before_read=len(read_bytes),
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=len(read_bytes),
        queued_after_read=0,
    )

    result = _classify_received_packet(receipt)

    assert scan.prior_tail_length == len(prior_tail)
    assert scan.tail_length == expected_tail_length
    assert result.reasons == ("parser_remainder",)


def test_hottop_received_packet_classifier_rejects_missing_or_invalid_newest_pair() -> None:
    """No receipt and an invalid newest frame never inherit an earlier valid pair."""
    no_receipt = _classify_received_packet(None)
    valid_then_invalid = b"".join(
        (
            _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
            _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0),
        )
    )
    scan = scan_hottop_status_read(
        valid_then_invalid,
        queued_before_read=72,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=72,
        queued_after_read=0,
    )

    invalid_newest = _classify_received_packet(receipt)

    assert no_receipt.reasons == ("no_receipt",)
    assert receipt.latest_pair_c is None
    assert invalid_newest.reasons == ("latest_pair_rejected",)


def test_hottop_status_scan_binds_decoder_queue_sizes_and_tail_bytes() -> None:
    """Scanner output retains only the observations needed to bind a receipt."""
    packet = _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)
    scan = scan_hottop_status_read(
        packet + b"\xa5\x96",
        queued_before_read=38,
        requested_byte_count=40,
        temperature_unit="celsius",
    )

    assert scan.decoder_temperature_unit == "celsius"
    assert scan.queued_before_read == 38
    assert scan.requested_byte_count == 40
    assert scan.returned_byte_count == 38
    assert scan.tail_length == 2
    assert scan.tail_bytes == b"\xa5\x96"


@pytest.mark.parametrize(
    ("scan_unit", "builder_queue", "replacement"),
    [
        ("auto", 36, None),
        ("celsius", 35, None),
        ("celsius", 36, (196.0, 201.0)),
    ],
)
def test_hottop_received_packet_rejects_conflicting_scanner_bindings(
    scan_unit: HottopTemperatureUnit,
    builder_queue: int,
    replacement: tuple[float, float] | None,
) -> None:
    """A receipt cannot relabel auto decoding, queue evidence, or newest values."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=36,
        requested_byte_count=144,
        temperature_unit=scan_unit,
    )
    if scan_unit == "auto" or builder_queue != 36:
        with pytest.raises(ValueError):
            _build_received_packet(
                scan=scan,
                queued_before_read=builder_queue,
                queued_after_read=0,
            )
        return

    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=builder_queue,
        queued_after_read=0,
    )
    with pytest.raises(ValueError, match="latest_pair_c"):
        replace(receipt, latest_pair_c=replacement)


@pytest.mark.parametrize(
    ("scenario", "scan_unit", "queue_observation"),
    [
        pytest.param("decoder_unit", "auto", 36, id="decoder-unit"),
        pytest.param("queue_observation", "celsius", "unreadable", id="queue-observation"),
    ],
)
def test_hottop_bound_evidence_cannot_be_relabelled_eligible(
    scenario: str,
    scan_unit: HottopTemperatureUnit,
    queue_observation: object,
) -> None:
    """Replacing one bound field cannot make a refused receipt eligible."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=queue_observation,
        requested_byte_count=144,
        temperature_unit=scan_unit,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=queue_observation,
        queued_after_read=0,
        configured_temperature_unit=scan_unit,
    )

    assert _classify_received_packet(receipt).eligible is False
    if scenario == "decoder_unit":
        with pytest.raises(ValueError, match="packet temperature_unit"):
            replace(scan, decoder_temperature_unit="celsius")
    else:
        with pytest.raises(ValueError, match="queued_before_read"):
            replace(receipt, queued_before_read=36)


@pytest.mark.parametrize(
    ("configured_unit", "raw_bean_temperature", "raw_env_temperature", "expected_pair"),
    [
        pytest.param("auto", 195, 200, (195.0, 200.0), id="auto"),
        pytest.param("fahrenheit", 383, 392, (195.0, 200.0), id="fahrenheit"),
    ],
)
def test_hottop_received_packet_preserves_auto_and_fahrenheit_scan_bindings(
    configured_unit: HottopTemperatureUnit,
    raw_bean_temperature: int,
    raw_env_temperature: int,
    expected_pair: tuple[float, float],
) -> None:
    """Non-Celsius scan and build units remain bound and are classified as ineligible."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(
            raw_env_temperature=raw_env_temperature,
            raw_bean_temperature=raw_bean_temperature,
        ),
        queued_before_read=36,
        requested_byte_count=144,
        temperature_unit=configured_unit,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=36,
        queued_after_read=0,
        configured_temperature_unit=configured_unit,
    )

    result = _classify_received_packet(receipt)

    assert scan.decoder_temperature_unit == configured_unit
    assert scan.packets[-1].temperature_unit == configured_unit
    assert receipt.configured_temperature_unit == configured_unit
    assert receipt.latest_pair_c == expected_pair
    assert result.reasons == ("unit_not_explicit_celsius",)


def test_hottop_received_packet_constructor_refuses_missing_or_invented_newest_pair() -> None:
    """A constructor cannot erase a valid newest pair or invent an invalid one."""
    valid_scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=36,
        requested_byte_count=144,
    )
    invalid_scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0),
        queued_before_read=36,
        requested_byte_count=144,
    )
    valid_receipt = _build_received_packet(
        scan=valid_scan,
        queued_before_read=36,
        queued_after_read=0,
    )
    invalid_receipt = _build_received_packet(
        scan=invalid_scan,
        queued_before_read=36,
        queued_after_read=0,
    )

    with pytest.raises(ValueError, match="latest_pair_c"):
        replace(valid_receipt, latest_pair_c=None)
    with pytest.raises(ValueError, match="latest_pair_c"):
        replace(invalid_receipt, latest_pair_c=(195.0, 200.0))


def test_hottop_received_packet_classifier_records_disabled_candidate_reason() -> None:
    """A complete receipt remains ineligible when the candidate is not configured."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=36,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(scan=scan, queued_before_read=36, queued_after_read=0)

    result = _classify_received_packet(receipt, candidate_configured=False)

    assert result.eligible is False
    assert result.reasons == ("candidate_not_configured",)


def test_hottop_receipt_classification_rejects_inconsistent_eligibility() -> None:
    """Direct construction cannot contradict a non-empty refusal reason list."""
    with pytest.raises(ValueError, match="eligible"):
        HottopReceiptClassification(
            eligible=True,
            reasons=("candidate_not_configured",),
            drained_floor_age_seconds=None,
            read_returned_age_seconds=None,
        )


@pytest.mark.parametrize(
    ("read_bytes", "expected_tail", "expected_packet_count"),
    [
        pytest.param(
            b"noise"
            + _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)
            + b"tail",
            b"tail",
            1,
            id="noise",
        ),
        pytest.param(
            build_hottop_command_packet(heat_level_percent=20)
            + _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
            b"",
            1,
            id="command-echo",
        ),
        pytest.param(
            bytes(
                bytearray(
                    _build_hottop_status_packet(
                        raw_env_temperature=200,
                        raw_bean_temperature=195,
                    )
                )[:-1]
                + b"\x00"
            )
            + _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
            b"",
            1,
            id="checksum",
        ),
        pytest.param(
            _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)[:12],
            _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)[:12],
            0,
            id="split-frame",
        ),
    ],
)
def test_hottop_status_scan_matches_protected_packet_scanner(
    read_bytes: bytes,
    expected_tail: bytes,
    expected_packet_count: int,
) -> None:
    """Scanner packet decoding and retained tail match the established scanner cases."""
    scan = scan_hottop_status_read(
        read_bytes,
        queued_before_read=len(read_bytes),
        requested_byte_count=144,
    )
    protected_packet = find_hottop_status_packet(read_bytes)
    scanned_packet = scan.packets[0] if scan.packets else None

    assert len(scan.packets) == expected_packet_count
    assert scan.tail_bytes == expected_tail
    assert scanned_packet == protected_packet


class _QueueIntEnum(IntEnum):
    """Non-exact queue-like integer used to prove strict queue evidence."""

    ONE = 1


@pytest.mark.parametrize("queue_observation", [True, _QueueIntEnum.ONE, -1, "1"])
def test_hottop_received_packet_classifier_refuses_non_exact_queue_observations(
    queue_observation: object,
) -> None:
    """Only built-in ints establish a queue observation."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=queue_observation,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=queue_observation,
        queued_after_read=0,
    )

    assert scan.queued_before_read is None
    assert _classify_received_packet(receipt).primary_reason == "queue_unreadable"


@pytest.mark.parametrize(
    ("queued_before_read", "requested_byte_count", "read_bytes"),
    [
        (36, 35, _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)),
        (35, 144, _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)),
    ],
)
def test_hottop_received_packet_classifier_refuses_read_size_contradictions(
    queued_before_read: int,
    requested_byte_count: int,
    read_bytes: bytes,
) -> None:
    """A returned byte count cannot exceed either owned read bound."""
    scan = scan_hottop_status_read(
        read_bytes,
        queued_before_read=queued_before_read,
        requested_byte_count=requested_byte_count,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=queued_before_read,
        queued_after_read=0,
    )

    assert scan.read_size_contradiction is True
    assert "read_size_contradiction" in _classify_received_packet(receipt).reasons


def test_hottop_received_packet_classifier_refuses_invalid_intervening_history() -> None:
    """A later plausible pair cannot erase an implausible packet in the read."""
    scan = scan_hottop_status_read(
        b"".join(
            (
                _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
                _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0),
                _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
            )
        ),
        queued_before_read=108,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(scan=scan, queued_before_read=108, queued_after_read=0)

    result = _classify_received_packet(receipt)

    assert receipt.latest_pair_c == (197.0, 202.0)
    assert result.eligible is False
    assert result.reasons == ("invalid_packet_history",)


def test_hottop_received_packet_classifier_preserves_no_receipt_primary_reason() -> None:
    """An unset bound supplements, but does not replace, absent receipt evidence."""
    result = _classify_received_packet(None, receipt_age_bound_seconds=None)

    assert result.reasons == ("no_receipt", "receipt_age_bound_unset")


@pytest.mark.parametrize(
    ("receipt_change", "now", "receipt_age_bound_seconds", "expected_reason"),
    [
        ({"epoch": 3}, 13.0, 3.0, "epoch_or_handle_changed"),
        ({"handle_identity": 8}, 13.0, 3.0, "epoch_or_handle_changed"),
        ({"sequence": 1}, 13.0, 3.0, "superseded"),
        ({"drained_floor": None}, 13.0, 3.0, "no_drained_floor"),
        (
            {
                "drained_floor": HottopDrainedFloor(
                    epoch=3,
                    handle_identity=9,
                    sequence=1,
                    observed_at=10.0,
                )
            },
            13.0,
            3.0,
            "no_drained_floor",
        ),
        ({}, 11.5, 3.0, "clock_regression"),
        ({}, 13.0, None, "receipt_age_bound_unset"),
        ({}, 13.1, 3.0, "receipt_age_exceeds_bound"),
    ],
)
def test_hottop_received_packet_classifier_primary_refusals(
    receipt_change: dict[str, object],
    now: float,
    receipt_age_bound_seconds: float | None,
    expected_reason: str,
) -> None:
    """Epoch, sequence, floor, and host-clock evidence have stable refusal order."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=36,
        requested_byte_count=144,
    )
    receipt = replace(
        _build_received_packet(scan=scan, queued_before_read=36, queued_after_read=0),
        **receipt_change,
    )

    result = _classify_received_packet(
        receipt,
        now=now,
        receipt_age_bound_seconds=receipt_age_bound_seconds,
    )

    assert result.primary_reason == expected_reason


def test_hottop_received_packet_classifier_accepts_exact_age_bound() -> None:
    """The host receipt age bound is inclusive at its exact endpoint."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
        queued_before_read=36,
        requested_byte_count=144,
    )

    assert _classify_received_packet(
        _build_received_packet(scan=scan, queued_before_read=36, queued_after_read=0)
    ).eligible


@pytest.mark.parametrize(
    ("read_bytes", "expected_reason"),
    [
        (
            b"\x00"
            + _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195),
            "misaligned_or_skipped",
        ),
        (
            _build_hottop_status_packet(raw_env_temperature=200, raw_bean_temperature=195)[:12],
            "parser_remainder",
        ),
    ],
)
def test_hottop_received_packet_classifier_refuses_resync_and_partial_history(
    read_bytes: bytes,
    expected_reason: str,
) -> None:
    """Noise and incomplete bytes never become a contiguous receipt selection."""
    scan = scan_hottop_status_read(
        read_bytes,
        queued_before_read=len(read_bytes),
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=len(read_bytes),
        queued_after_read=0,
    )

    assert _classify_received_packet(receipt).primary_reason == expected_reason


@pytest.mark.parametrize("invalid_bound", [0, -1, True, float("inf"), float("nan")])
def test_hottop_received_packet_classifier_rejects_malformed_receipt_age_bound(
    invalid_bound: object,
) -> None:
    """A malformed age bound has no path to a receipt eligibility result."""
    scan = scan_hottop_status_read(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197),
        queued_before_read=36,
        requested_byte_count=144,
    )
    receipt = _build_received_packet(
        scan=scan,
        queued_before_read=36,
        queued_after_read=0,
    )

    with pytest.raises(ValueError, match="receipt_age_bound_seconds"):
        _classify_received_packet(
            receipt,
            receipt_age_bound_seconds=cast(float | None, invalid_bound),
        )


def _wait_for_hottop_write(
    transport: FakeSerialTransport,
    predicate: Callable[[bytes], bool],
    *,
    start_index: int = 0,
    timeout: float = 1.0,
) -> bytes:
    """Wait for a Hottop serial write matching the expected command state."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        for write in transport.writes_snapshot()[start_index:]:
            if predicate(write):
                return write
        sleep(0.01)
    pytest.fail("Timed out waiting for expected Hottop command write.")


def _wait_for_hottop_status_count(
    driver: HottopRoasterDriver,
    expected_count: int,
    *,
    timeout: float = 1.0,
) -> RoasterState:
    """Wait until the Hottop driver has recorded status packets."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        state = driver.read_state()
        status_count = state.raw_vendor_data["status_packet_count"]
        assert isinstance(status_count, int)
        if status_count >= expected_count:
            return state
        sleep(0.01)
    pytest.fail("Timed out waiting for expected Hottop status packet count.")


def _assert_roaster_driver_contract(driver: RoasterDriver) -> None:
    """Exercise the E3 roaster driver contract against one implementation."""
    capabilities = driver.capabilities
    assert capabilities.driver == "mock"
    assert capabilities.heat.minimum == 0
    assert capabilities.heat.maximum == 100
    assert capabilities.heat.step == 1
    assert capabilities.fan.minimum == 0
    assert capabilities.fan.maximum == 100
    assert capabilities.actions.heat_control is True
    assert capabilities.actions.fan_control is True
    assert capabilities.actions.bean_drop is True
    assert capabilities.actions.cooling_control is True
    assert capabilities.actions.emergency_stop is True
    assert capabilities.sensor_units.bean_temperature == "celsius"
    assert capabilities.sensor_units.environment_temperature == "celsius"
    assert capabilities.command_streaming.required is False
    assert capabilities.command_streaming.interval_seconds is None

    initial_state = driver.read_state()
    assert initial_state.driver == "mock"
    assert initial_state.connected is False
    assert initial_state.bean_temp_c == 20.0
    assert initial_state.env_temp_c == 20.0
    assert initial_state.heat_level_percent == 0
    assert initial_state.fan_level_percent == 0
    assert initial_state.cooling_on is False
    assert initial_state.raw_vendor_data["sample_index"] == 1

    driver.connect()
    connected_state = driver.read_state()
    assert connected_state.connected is True
    assert connected_state.raw_vendor_data["sample_index"] == 2

    heat_state = driver.set_heat(heat_level_percent=55)
    assert heat_state.heat_level_percent == 55
    assert heat_state.fan_level_percent == 0
    assert heat_state.raw_vendor_data["sample_index"] == 2

    fan_state = driver.set_fan(fan_level_percent=35)
    assert fan_state.heat_level_percent == 55
    assert fan_state.fan_level_percent == 35
    assert fan_state.raw_vendor_data["sample_index"] == 2

    dropped_state = driver.drop_beans()
    assert dropped_state.heat_level_percent == 0
    assert dropped_state.raw_vendor_data["beans_dropped"] is True
    assert dropped_state.raw_vendor_data["sample_index"] == 2

    cooling_state = driver.start_cooling()
    assert cooling_state.cooling_on is True

    stopped_cooling_state = driver.stop_cooling()
    assert stopped_cooling_state.cooling_on is False
    assert stopped_cooling_state.fan_level_percent == 0

    driver.disconnect()
    disconnected_state = driver.read_state()
    assert disconnected_state.connected is False


def _read_temperature_sequence(
    driver: RoasterDriver,
    *,
    sample_count: int,
) -> list[tuple[float | None, float | None]]:
    """Read a deterministic sequence of bean and environment temperatures."""
    sequence: list[tuple[float | None, float | None]] = []
    for _ in range(sample_count):
        state = driver.read_state()
        sequence.append((state.bean_temp_c, state.env_temp_c))
    return sequence


def test_create_roaster_driver_returns_mock_driver() -> None:
    driver = create_roaster_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_create_roaster_driver_returns_hottop_driver() -> None:
    driver = create_roaster_driver(
        "hottop_kn8828b_2k_plus",
        temperature_unit="fahrenheit",
    )

    assert isinstance(driver, HottopRoasterDriver)
    assert driver.read_state().raw_vendor_data["temperature_unit"] == "fahrenheit"


def test_candidate_factory_requires_simulated_transport_and_declares_capability() -> None:
    """The candidate cannot select pyserial and is declared only with fake composition."""
    with pytest.raises(ValueError, match="injected simulated transport"):
        create_roaster_driver(
            HOTTOP_DRIVER_NAME,
            temperature_unit="celsius",
            hottop_reception="candidate/v1",
        )
    factory = FakeSerialFactory()
    driver = create_roaster_driver(
        HOTTOP_DRIVER_NAME,
        temperature_unit="celsius",
        hottop_reception="candidate/v1",
        serial_factory=factory,
    )

    assert isinstance(driver, HottopRoasterDriver)
    assert driver.capabilities.hottop_reception_candidate_version == 1
    assert factory.calls == []


def test_candidate_refuses_provider_non_celsius_and_mock_composition() -> None:
    """The candidate's simulated boundary has no fallback to an unsafe composition."""
    factory = FakeSerialFactory()
    with pytest.raises(ValueError, match="temperature_unit celsius"):
        HottopRoasterDriver(
            serial_factory=factory,
            temperature_unit="auto",
            hottop_reception="candidate/v1",
        )
    with pytest.raises(ValueError, match="command_frame_provider"):
        HottopRoasterDriver(
            serial_factory=factory,
            hottop_reception="candidate/v1",
            command_frame_provider=lambda: b"",
        )
    with pytest.raises(ValueError, match="mock driver"):
        create_roaster_driver("mock", hottop_reception="candidate/v1")


class MutableReceptionClock:
    """Deterministic injected host clock for candidate exact-Drop tests."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        """Return the current test-controlled host time."""
        return self.value


class ReceiptLoopGate:
    """Observe ordered live-loop receipt completion without a competing reader."""

    def __init__(self) -> None:
        self.calls = 0
        self.drained_floor_complete = Event()
        self.receipt_complete = Event()
        self.next_receipt_complete = Event()
        self.release_after_receipt = Event()

    def __call__(self) -> None:
        """Publish each completed loop stage and hold the verified receipt stable."""
        self.calls += 1
        if self.calls == 1:
            self.drained_floor_complete.set()
            return
        if self.calls == 2:
            self.receipt_complete.set()
            self.release_after_receipt.wait(timeout=1.0)
            return
        self.next_receipt_complete.set()


def _candidate_exact_drop_driver(
    transport: FakeSerialTransport | None = None,
) -> tuple[HottopRoasterDriver, FakeSerialTransport, MutableReceptionClock, ReceiptLoopGate]:
    """Build one staged live stream with a current, eligible typed receipt."""
    transport = transport or FakeSerialTransport()
    clock = MutableReceptionClock(10.0)
    driver = create_roaster_driver(
        HOTTOP_DRIVER_NAME,
        port="fake",
        temperature_unit="celsius",
        command_interval_seconds=0.05,
        hottop_reception="candidate/v1",
        serial_factory=FakeSerialFactory(transport),
        reception_clock=clock,
        simulation_receipt_age_bound_seconds=3.0,
    )
    assert isinstance(driver, HottopRoasterDriver)
    gate = ReceiptLoopGate()
    driver._command_loop_iteration_hook = gate  # pyright: ignore[reportPrivateUsage]
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    driver.hand_off_written_start_to_stream(owner, session)
    assert gate.drained_floor_complete.wait(timeout=1.0)
    clock.value = 11.0
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=195)
    )
    assert gate.receipt_complete.wait(timeout=1.0)
    return driver, transport, clock, gate


def _release_candidate_loop(driver: HottopRoasterDriver, gate: ReceiptLoopGate) -> None:
    """Release a receipt-held live loop before its normal deterministic cleanup."""
    gate.release_after_receipt.set()
    driver.disconnect()


def _exact_drop_arbitration(driver: HottopRoasterDriver) -> HottopExactDropArbitration:
    """Bind one opaque store operation to the driver's current receipt snapshot."""
    return HottopExactDropArbitration(
        operation_id="operation-1",
        session_id="session-1",
        operation_token="token-1",
        candidate=driver.snapshot_exact_drop_candidate(),
    )


def test_candidate_exact_drop_admits_one_current_receipt_and_one_command_revision() -> None:
    """One current eligible receipt admits exactly the shared Drop transition."""
    driver, transport, _clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    revision_before = driver._command_state_revision  # pyright: ignore[reportPrivateUsage]

    gate.release_after_receipt.set()
    state = driver.drop_beans(exact_drop=arbitration)

    frame = driver._current_command_frame()  # pyright: ignore[reportPrivateUsage]
    streamed_drop = _wait_for_hottop_write(
        transport,
        lambda frame: frame[16] == 1,
        start_index=1,
    )
    assert arbitration.candidate.streaming is True
    assert arbitration.candidate.receipt_classification.eligible is True
    assert state.raw_vendor_data["solenoid_open"] is True
    assert driver._command_state_revision == revision_before + 1  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_transition_count == 1  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.transition_revision == revision_before + 1  # pyright: ignore[reportPrivateUsage]
    assert frame[10] == 0
    assert frame[16] == 1
    assert streamed_drop[16] == 1
    writes = transport.writes_snapshot()
    rising_edges = [
        frame
        for previous, frame in zip(writes, writes[1:], strict=False)
        if previous[16] == 0 and frame[16] == 1
    ]
    assert len(rising_edges) == 1
    driver.disconnect()


def test_candidate_exact_drop_refuses_mock_only_arbitration_without_receipt_identity() -> None:
    """Mock arbitration cannot substitute for an evaluated candidate receipt."""
    driver, _transport, _clock, gate = _candidate_exact_drop_driver()
    mock_only = ExactDropArbitration(
        operation_id="operation-1",
        session_id="session-1",
        cancelled=Event(),
        expected_drop_transition_count=0,
        expected_emergency_count=0,
    )

    with pytest.raises(TypeError, match="HottopExactDropArbitration"):
        driver.drop_beans(exact_drop=mock_only)

    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_receipt_replaced_after_snapshot() -> None:
    """A later producer receipt supersedes the exact snapshot before admission."""
    driver, transport, clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    clock.value = 12.0
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=203, raw_bean_temperature=194)
    )
    gate.release_after_receipt.set()
    assert gate.next_receipt_complete.wait(timeout=1.0)

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "receipt_replaced"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    writes = transport.writes_snapshot()
    assert not any(
        previous[16] == 0 and frame[16] == 1
        for previous, frame in zip(writes, writes[1:], strict=False)
    )
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_record_disappearing_after_snapshot() -> None:
    """A current stream still refuses once its exact receipt is discarded."""
    driver, transport, _clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    driver._invalidate_candidate_receipt(  # pyright: ignore[reportPrivateUsage]
        transport,
        driver._reception_epoch,  # pyright: ignore[reportPrivateUsage]
    )

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "receipt_missing"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_expired_same_receipt() -> None:
    """The admission lock reclassifies the unchanged receipt with the injected clock."""
    driver, _transport, clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    clock.value = 14.1

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "receipt_expired"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_disconnect_epoch_change_after_snapshot() -> None:
    """A revoked stream cannot use an otherwise eligible prior receipt."""
    driver, _transport, _clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    _release_candidate_loop(driver, gate)

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "stream_lost"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]


def test_candidate_exact_drop_refuses_stop_linearised_first() -> None:
    """An independent Stop before admission prevents an additional Drop transition."""
    driver, _transport, _clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    driver.emergency_stop(reason="test")
    revision_after_stop = driver._command_state_revision  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "emergency_since_session_baseline"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    assert driver._emergency_count == 1  # pyright: ignore[reportPrivateUsage]
    assert driver._command_state_revision == revision_after_stop  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_changed_drop_history_without_replacing_receipt() -> None:
    """A settled prior Drop remains distinct from a replaced receipt snapshot."""
    driver, transport, clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    receipt = arbitration.candidate.receipt

    driver.drop_beans(exact_drop=arbitration)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    writes_after_drop = transport.writes_snapshot()
    assert any(
        previous[16] == 0 and frame[16] == 1
        for previous, frame in zip(writes_after_drop, writes_after_drop[1:], strict=False)
    )
    driver.stop_cooling()
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    assert receipt is driver._reception_record  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(HottopExactDropRefused) as unchanged_receipt:
        driver.drop_beans(exact_drop=arbitration)
    assert unchanged_receipt.value.evidence.reason == "prior_drop_transition_unaccounted"
    replacement_start = len(transport.writes_snapshot())
    clock.value = 12.0
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=203, raw_bean_temperature=194)
    )
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    replacement_receipt = driver._reception_record  # pyright: ignore[reportPrivateUsage]
    assert replacement_receipt is not None
    assert replacement_receipt.latest_pair_c == (194.0, 203.0)

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]

    assert raised.value.evidence.reason == "prior_drop_transition_unaccounted"
    assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.result == "accepted_by_host"  # pyright: ignore[reportPrivateUsage]
    replacement_writes = transport.writes_snapshot()[replacement_start:]
    assert len(replacement_writes) >= 2
    assert not any(
        previous[16] == 0 and frame[16] == 1
        for previous, frame in zip(replacement_writes, replacement_writes[1:], strict=False)
    )
    assert receipt is not replacement_receipt
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_accepted_open_drop_with_prior_evidence() -> None:
    """An accepted commanded-open Drop refuses a duplicate before mutation."""
    driver, _transport, _clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    driver.drop_beans(exact_drop=arbitration)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    revision = driver._command_state_revision  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "prior_drop_open"
    assert raised.value.evidence.retained_prior_drop is not None
    assert raised.value.evidence.retained_prior_drop.result == "accepted_by_host"
    assert driver._command_state_revision == revision  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_transition_count == 1  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_stop_after_unknown_exact_drop_write_preserves_history_and_refuses_repeat() -> (
    None
):
    """An uncertain first write retains its Drop and independent Stop history."""

    class PartialTransport(FakeSerialTransport):
        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data) if len(self.writes) == 1 else len(data) - 1

    transport = PartialTransport()
    driver, _transport, _clock, gate = _candidate_exact_drop_driver(transport)
    arbitration = _exact_drop_arbitration(driver)
    driver.drop_beans(exact_drop=arbitration)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    driver.emergency_stop(reason="test")

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "prior_unresolved"
    assert raised.value.evidence.retained_prior_drop is not None
    assert raised.value.evidence.retained_prior_drop.result == "unknown"
    assert driver._drop_transition_count == 1  # pyright: ignore[reportPrivateUsage]
    assert driver._emergency_count == 1  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.result == "unknown"  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_cannot_upgrade_an_originally_ineligible_receipt() -> None:
    """A backwards host clock cannot make an ineligible snapshot admissible."""
    driver, _transport, clock, gate = _candidate_exact_drop_driver()
    clock.value = 14.1
    arbitration = _exact_drop_arbitration(driver)
    assert arbitration.candidate.receipt_classification.eligible is False
    clock.value = 12.0

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "receipt_ineligible"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_exact_drop_refuses_backward_clock_for_originally_eligible_receipt() -> None:
    """A backward host clock cannot upgrade an originally eligible snapshot."""
    driver, _transport, clock, gate = _candidate_exact_drop_driver()
    arbitration = _exact_drop_arbitration(driver)
    assert arbitration.candidate.receipt_classification.eligible is True
    clock.value = 10.0

    with pytest.raises(HottopExactDropRefused) as raised:
        driver.drop_beans(exact_drop=arbitration)

    assert raised.value.evidence.reason == "receipt_ineligible"
    assert driver._drop_transition_count == 0  # pyright: ignore[reportPrivateUsage]
    _release_candidate_loop(driver, gate)


def test_candidate_reconnect_refuses_pending_exact_drop_before_opening_replacement() -> None:
    """A disconnected pending exact Drop cannot replay on a later transport."""

    class HoldSecondWriteTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.second_write_entered = Event()
            self.release_second_write = Event()

        def write(self, data: bytes) -> int:
            with self._write_lock:
                self.writes.append(data)
                write_number = len(self.writes)
            if write_number == 2:
                self.second_write_entered.set()
                assert self.release_second_write.wait(timeout=1.0)
            return len(data)

    old_transport = HoldSecondWriteTransport()
    replacement_transport = FakeSerialTransport()
    factory = SequentialSerialFactory([old_transport, replacement_transport])
    clock = MutableReceptionClock(10.0)
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=1.0,
        serial_factory=factory,
        hottop_reception="candidate/v1",
        reception_clock=clock,
        simulation_receipt_age_bound_seconds=3.0,
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    driver.hand_off_written_start_to_stream(owner, session)
    driver._write_timeout_seconds = 0.01  # pyright: ignore[reportPrivateUsage]
    driver._read_status_packet_candidate(old_transport)  # pyright: ignore[reportPrivateUsage]
    clock.value = 11.0
    old_transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=195)
    )
    driver._read_status_packet_candidate(old_transport)  # pyright: ignore[reportPrivateUsage]
    arbitration = _exact_drop_arbitration(driver)
    ordinary_write = Thread(target=driver._send_command_frame_candidate)  # pyright: ignore[reportPrivateUsage]
    ordinary_write.start()
    assert old_transport.second_write_entered.wait(timeout=1.0)
    driver.drop_beans(exact_drop=arbitration)

    disconnect_error: list[BaseException] = []

    def disconnect() -> None:
        try:
            driver.disconnect()
        except BaseException as error:
            disconnect_error.append(error)

    disconnect_worker = Thread(target=disconnect)
    disconnect_worker.start()
    assert old_transport.closed.wait(timeout=1.0)
    old_transport.release_second_write.set()
    ordinary_write.join(timeout=1.0)
    disconnect_worker.join(timeout=1.0)

    assert not ordinary_write.is_alive()
    assert not disconnect_worker.is_alive()
    assert disconnect_error == []
    assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.result == "pending"  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.attempt_epoch is None  # pyright: ignore[reportPrivateUsage]
    assert sum(frame[16] == 1 for frame in old_transport.writes_snapshot()) == 0

    with pytest.raises(RuntimeError):
        driver.connect()

    assert len(factory.calls) == 1
    assert replacement_transport.writes_snapshot() == []


def test_candidate_reconnect_refuses_unknown_closed_drop_before_factory_open() -> None:
    """A closed chute does not clear an unknown Drop settlement for reconnect."""

    class ShortDropTransport(FakeSerialTransport):
        def write(self, data: bytes) -> int:
            receipt = super().write(data)
            return receipt - 1 if data[16] == 1 else receipt

    old_transport = ShortDropTransport()
    replacement_transport = FakeSerialTransport()
    factory = SequentialSerialFactory([old_transport, replacement_transport])
    driver, _transport, _clock, gate = _candidate_exact_drop_driver(old_transport)
    driver._serial_factory = factory  # pyright: ignore[reportPrivateUsage]
    arbitration = _exact_drop_arbitration(driver)
    driver.drop_beans(exact_drop=arbitration)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]
    driver.emergency_stop(reason="test")
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]

    assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
    assert driver._drop_settlement.result == "unknown"  # pyright: ignore[reportPrivateUsage]
    assert driver.read_lifecycle_evidence().solenoid_open is False
    _release_candidate_loop(driver, gate)

    with pytest.raises(RuntimeError, match="retains command ownership"):
        driver.connect()

    assert len(factory.calls) == 0
    assert replacement_transport.writes_snapshot() == []


def test_candidate_clean_reconnect_streams_a_new_closed_frame() -> None:
    """An ordinary clean candidate reconnect still owns and streams its new transport."""
    first_transport = FakeSerialTransport()
    replacement_transport = FakeSerialTransport()
    factory = SequentialSerialFactory([first_transport, replacement_transport])
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=factory,
        hottop_reception="candidate/v1",
    )

    driver.connect()
    _wait_for_hottop_write(first_transport, lambda frame: frame[16] == 0)
    driver.disconnect()
    driver.connect()
    replacement_first_frame = _wait_for_hottop_write(
        replacement_transport,
        lambda frame: frame[16] == 0,
    )
    driver.disconnect()

    assert len(factory.calls) == 2
    assert replacement_transport.writes_snapshot()[0][16] == 0
    assert replacement_first_frame[16] == 0


def test_candidate_reconnect_preserves_fan_only_preconnect_compatibility() -> None:
    """A closed, settled candidate may retain an ordinary fan setting on reconnect."""
    first_transport = FakeSerialTransport()
    replacement_transport = FakeSerialTransport()
    factory = SequentialSerialFactory([first_transport, replacement_transport])
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=factory,
        hottop_reception="candidate/v1",
    )

    driver.connect()
    _wait_for_hottop_write(first_transport, lambda frame: frame[16] == 0)
    driver.disconnect()
    driver.set_fan(fan_level_percent=30)
    driver.connect()
    replacement_first_frame = _wait_for_hottop_write(
        replacement_transport,
        lambda frame: frame[12] == 3,
    )
    driver.disconnect()

    assert len(factory.calls) == 2
    assert replacement_transport.writes_snapshot()[0][16] == 0
    assert replacement_first_frame[16] == 0
    assert replacement_first_frame[12] == 3


def test_candidate_reconnect_rechecks_open_chute_before_installing_replacement() -> None:
    """A command change during factory entry cannot publish an open replacement stream."""
    first_transport = FakeSerialTransport()
    replacement_transport = FakeSerialTransport()
    factory = SequentialSerialFactory([first_transport])
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=factory,
        hottop_reception="candidate/v1",
    )
    driver.connect()
    _wait_for_hottop_write(first_transport, lambda frame: frame[16] == 0)
    driver.disconnect()

    def admit_replacement(*args: object, **kwargs: object) -> FakeSerialTransport:
        del args, kwargs
        driver.drop_beans()
        return replacement_transport

    driver._serial_factory = admit_replacement  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match="retains command ownership"):
        driver.connect()

    assert replacement_transport.writes_snapshot() == []
    assert replacement_transport.close_calls == 1
    assert driver._drop_transition_count == 1  # pyright: ignore[reportPrivateUsage]


def test_candidate_public_factory_negative_drop_receipt_remains_unknown_after_stop() -> None:
    """An unrepresentable Drop receipt remains unavailable and blocks a later Drop."""

    class NegativeFirstDropTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_drop_written = Event()
            self.repeat_drop_written = Event()
            self.release_repeat_drop = Event()
            self.next_drop_written = Event()
            self.release_next_drop = Event()
            self._drop_writes = 0

        def write(self, data: bytes) -> int:
            with self._write_lock:
                self.writes.append(data)
            if data[16] != 1:
                return len(data)
            self._drop_writes += 1
            if self._drop_writes == 1:
                self.first_drop_written.set()
                return -1
            if self._drop_writes == 2:
                self.repeat_drop_written.set()
                assert self.release_repeat_drop.wait(timeout=1.0)
            if self._drop_writes == 3:
                self.next_drop_written.set()
                assert self.release_next_drop.wait(timeout=1.0)
            return len(data)

    transport = NegativeFirstDropTransport()
    driver = create_roaster_driver(
        HOTTOP_DRIVER_NAME,
        port="fake-only",
        temperature_unit="celsius",
        command_interval_seconds=0.01,
        hottop_reception="candidate/v1",
        serial_factory=FakeSerialFactory(transport),
    )
    assert isinstance(driver, HottopRoasterDriver)
    driver.drop_beans()
    driver.connect()
    try:
        assert transport.repeat_drop_written.wait(timeout=1.0)
        first_evidence = driver.read_lifecycle_evidence()
        assert first_evidence.last_command_write_size == 0
        assert first_evidence.command_send_attempts == 2
        assert first_evidence.command_write_count == 0
        assert first_evidence.command_loop_error_count == 1

        transport.release_repeat_drop.set()
        assert transport.next_drop_written.wait(timeout=1.0)
        repeat_evidence = driver.read_lifecycle_evidence()
        assert repeat_evidence.last_command_write_size == HOTTOP_PACKET_LENGTH
        assert repeat_evidence.command_write_count == 1
        driver.stop_cooling()
        stopped_evidence = driver.read_lifecycle_evidence()
        assert stopped_evidence.solenoid_open is False
        transition_count = driver._drop_transition_count  # pyright: ignore[reportPrivateUsage]
        revision = driver._command_state_revision  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(HottopDropRefused):
            driver.drop_beans()

        assert driver._drop_transition_count == transition_count  # pyright: ignore[reportPrivateUsage]
        assert driver._command_state_revision == revision  # pyright: ignore[reportPrivateUsage]
        assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
        assert driver._drop_settlement.result == "unknown"  # pyright: ignore[reportPrivateUsage]
        assert [attempt.role for attempt in driver._attempt_history[:2]] == [  # pyright: ignore[reportPrivateUsage]
            "drop",
            "repeat",
        ]
        assert driver._attempt_history[1].result == "accepted_by_host"  # pyright: ignore[reportPrivateUsage]
    finally:
        transport.release_repeat_drop.set()
        transport.release_next_drop.set()
        driver.disconnect()


def test_candidate_write_gate_rebuilds_emergency_frame_after_drop_is_closed() -> None:
    """Stop can close an unentered Drop while the candidate gate waits for its lock."""

    class SignallingLock:
        def __init__(self) -> None:
            self._lock = Lock()
            self.command_loop_waiting = Event()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if get_ident() != main_thread:
                self.command_loop_waiting.set()
            return self._lock.acquire(blocking, timeout)

        def release(self) -> None:
            self._lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self.release()

    class WrittenTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.written = Event()

        def write(self, data: bytes) -> int:
            result = super().write(data)
            self.written.set()
            return result

    transport = WrittenTransport()
    driver = HottopRoasterDriver(
        port="simulated",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    gate = SignallingLock()
    main_thread = get_ident()
    driver._command_write_lock = gate  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    driver.drop_beans()
    assert gate.acquire()
    driver.connect()
    try:
        assert gate.command_loop_waiting.wait(timeout=1.0)
        driver.emergency_stop(reason="test")
        assert transport.writes_snapshot() == []
        gate.release()
        assert transport.written.wait(timeout=1.0)

        assert transport.writes_snapshot()[0][16] == 0
        assert driver._drop_settlement is not None  # pyright: ignore[reportPrivateUsage]
        assert driver._drop_settlement.result == "not_attempted"  # pyright: ignore[reportPrivateUsage]
        assert (driver._drop_transition_count, driver._emergency_count) == (1, 1)  # pyright: ignore[reportPrivateUsage]
    finally:
        if gate._lock.locked():  # pyright: ignore[reportPrivateUsage]
            gate.release()
        driver.disconnect()


def test_candidate_receipt_uses_prior_floor_and_leaves_age_bound_unset() -> None:
    """A simulated clean read publishes host receipt only, never qualification."""
    transport = FakeSerialTransport()
    clock_values = iter((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
        reception_clock=lambda: next(clock_values),
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)
    )
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]

    receipt = driver._reception_record  # pyright: ignore[reportPrivateUsage]
    assert receipt is not None
    result = classify_hottop_received_packet(
        receipt,
        candidate_configured=True,
        current_epoch=driver._reception_epoch,  # pyright: ignore[reportPrivateUsage]
        current_handle_identity=driver._reception_handle_token,  # pyright: ignore[reportPrivateUsage]
        current_sequence=receipt.sequence,
        now=6.0,
        receipt_age_bound_seconds=None,
    )
    assert receipt.latest_pair_c == (197.0, 202.0)
    assert result.reasons == ("receipt_age_bound_unset",)


def test_candidate_uses_falsy_fake_factory_and_refuses_default_transport() -> None:
    """Candidate composition retains an explicit fake even when it is falsy."""

    class FalsyFactory(FakeSerialFactory):
        def __bool__(self) -> bool:
            return False

    factory = FalsyFactory()
    driver = create_roaster_driver(
        HOTTOP_DRIVER_NAME,
        port="fake-only",
        temperature_unit="celsius",
        hottop_reception="candidate/v1",
        serial_factory=factory,
    )

    driver.connect()
    try:
        assert factory.calls
    finally:
        driver.disconnect()

    with pytest.raises(ValueError, match="injected simulated transport"):
        HottopRoasterDriver(
            hottop_reception="candidate/v1",
            serial_factory=drivers_module._create_pyserial_transport,  # pyright: ignore[reportPrivateUsage]
        )


def test_candidate_emergency_receipt_stays_with_exact_transition() -> None:
    """A later heat frame cannot settle a previously admitted Stop."""

    class StopBlockingTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.stop_write_started = Event()
            self.release_stop_write = Event()

        def write(self, data: bytes) -> int:
            with self._write_lock:
                self.writes.append(data)
            if data[10] == 0 and data[12] == 10 and data[17] == 0 and data[18] == 1:
                self.stop_write_started.set()
                self.release_stop_write.wait(timeout=1.0)
            return len(data)

    transport = StopBlockingTransport()
    driver = create_roaster_driver(
        HOTTOP_DRIVER_NAME,
        port="fake-only",
        temperature_unit="celsius",
        command_interval_seconds=0.01,
        hottop_reception="candidate/v1",
        serial_factory=FakeSerialFactory(transport),
    )
    assert isinstance(driver, HottopRoasterDriver)
    driver.connect()
    try:
        driver.emergency_stop(reason="test")
        assert transport.stop_write_started.wait(timeout=1.0)
        driver.set_heat(heat_level_percent=50)
        transport.release_stop_write.set()
        assert _wait_for_hottop_write(transport, lambda frame: frame[10] == 50, start_index=1)

        (first,) = [
            settlement
            for settlement in driver._emergency_settlements  # pyright: ignore[reportPrivateUsage]
            if settlement.transition_seq == 1
        ]
        assert first.result == "accepted_by_host"
        assert first.transition_revision == 1
        first_attempt = next(
            attempt
            for attempt in driver._attempt_history  # pyright: ignore[reportPrivateUsage]
            if attempt.attempt_seq == first.attempt_seq
        )
        assert first_attempt.frame[10] == 0
        assert driver._attempt_history[-1].role == "ordinary"  # pyright: ignore[reportPrivateUsage]
    finally:
        transport.release_stop_write.set()
        driver.disconnect()


def test_candidate_superseded_stop_before_first_tick_is_not_credited() -> None:
    """A heat revision before a tick cannot turn an unentered Stop into a receipt."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    driver.emergency_stop(reason="test")
    driver.set_heat(heat_level_percent=50)
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]

    assert transport.writes_snapshot()[0][10] == 50
    assert driver._emergency_settlements[0].result == "pending"  # pyright: ignore[reportPrivateUsage]
    assert driver._attempt_history[-1].role == "ordinary"  # pyright: ignore[reportPrivateUsage]


def test_candidate_distinct_inflight_stops_settle_their_own_write_results() -> None:
    """A raising later Stop write cannot relabel an earlier in-flight Stop."""

    class TwoStopTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_write_entered = Event()
            self.release_first_write = Event()

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            if len(self.writes) == 1:
                self.first_write_entered.set()
                assert self.release_first_write.wait(timeout=1.0)
                return len(data)
            raise OSError("second stop write failed")

    transport = TwoStopTransport()
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    driver.emergency_stop(reason="first")
    first_tick = Thread(target=driver._send_command_frame_candidate)  # pyright: ignore[reportPrivateUsage]
    first_tick.start()
    assert transport.first_write_entered.wait(timeout=1.0)
    driver.emergency_stop(reason="second")
    transport.release_first_write.set()
    first_tick.join(timeout=1.0)
    assert not first_tick.is_alive()
    driver._send_command_frame_candidate()  # pyright: ignore[reportPrivateUsage]

    assert [settlement.result for settlement in driver._emergency_settlements] == [  # pyright: ignore[reportPrivateUsage]
        "accepted_by_host",
        "unknown",
    ]


def test_candidate_empty_poll_with_unreadable_repeat_probe_revokes_continuity() -> None:
    """An unknown second empty-poll observation cannot retain an old receipt or floor."""

    class UnreadableSecondEmptyProbeTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.empty_probes = 0
            self.raise_on_second_empty_probe = False

        @property
        def in_waiting(self) -> int:
            value = super().in_waiting
            if value == 0:
                self.empty_probes += 1
                if self.raise_on_second_empty_probe and self.empty_probes == 2:
                    raise OSError("second empty probe unavailable")
            return value

    transport = UnreadableSecondEmptyProbeTransport()
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)
    )
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]
    assert driver._reception_record is not None  # pyright: ignore[reportPrivateUsage]
    transport.empty_probes = 0
    transport.raise_on_second_empty_probe = True
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]

    assert driver._reception_record is None  # pyright: ignore[reportPrivateUsage]
    assert driver._reception_floor is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("read_outcome", ["unreadable", "empty", "error"])
def test_candidate_unknown_read_outcomes_revoke_a_previously_good_record(
    read_outcome: str,
) -> None:
    """Unreadable, empty, and failed reads all discard prior candidate continuity."""

    class UnknownReadTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.outcome: str | None = None

        @property
        def in_waiting(self) -> int:
            if self.outcome == "unreadable":
                raise OSError("queue unavailable")
            if self.outcome in {"empty", "error"}:
                return 1
            return super().in_waiting

        def read(self, size: int) -> bytes:
            if self.outcome == "empty":
                return b""
            if self.outcome == "error":
                raise OSError("read failed")
            return super().read(size)

    transport = UnknownReadTransport()
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)
    )
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]
    assert driver._reception_record is not None  # pyright: ignore[reportPrivateUsage]
    transport.outcome = read_outcome
    driver._read_status_packet_candidate(transport)  # pyright: ignore[reportPrivateUsage]

    assert driver._reception_record is None  # pyright: ignore[reportPrivateUsage]


def test_candidate_disconnect_during_real_loop_read_cannot_publish_receipt() -> None:
    """Candidate disconnect revokes read ownership before the loop can publish a receipt."""

    class BlockingReadTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.read_started = Event()
            self.release_read = Event()

        def read(self, size: int) -> bytes:
            self.read_started.set()
            assert self.release_read.wait(timeout=1.0)
            return super().read(size)

    transport = BlockingReadTransport()
    transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=202, raw_bean_temperature=197)
    )
    driver = HottopRoasterDriver(
        port="fake-only",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    driver.connect()
    try:
        assert transport.read_started.wait(timeout=1.0)
        disconnect_thread = Thread(target=driver.disconnect)
        disconnect_thread.start()
        assert driver._disconnect_requested.wait(timeout=1.0)  # pyright: ignore[reportPrivateUsage]
        transport.release_read.set()
        disconnect_thread.join(timeout=1.0)
        assert not disconnect_thread.is_alive()
        assert driver._reception_record is None  # pyright: ignore[reportPrivateUsage]
        assert driver._status_packet_count == 0  # pyright: ignore[reportPrivateUsage]
    finally:
        transport.release_read.set()
        if driver.read_state().connected:
            driver.disconnect()


@pytest.mark.parametrize(
    ("reception", "connected_while_waiting"),
    [("off", True), ("candidate/v1", False)],
)
def test_disconnect_preserves_off_mode_stop_timing_but_revokes_candidate_early(
    reception: str, connected_while_waiting: bool
) -> None:
    """Only candidate mode revokes lifecycle visibility before the write lock opens."""

    class SignallingLock:
        def __init__(self) -> None:
            self._lock = Lock()
            self.waiting = Event()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if get_ident() != main_thread:
                self.waiting.set()
            return self._lock.acquire(blocking, timeout)

        def release(self) -> None:
            self._lock.release()

    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        serial_factory=FakeSerialFactory(transport),
        hottop_reception=cast(Any, reception),
    )
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        driver._serial = transport  # pyright: ignore[reportPrivateUsage]
        driver._connected = True  # pyright: ignore[reportPrivateUsage]
        if reception == "candidate/v1":
            driver._begin_reception_epoch(transport)  # pyright: ignore[reportPrivateUsage]
    gate = SignallingLock()
    main_thread = get_ident()
    driver._command_write_lock = gate  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    assert gate.acquire()
    worker = Thread(target=driver._request_command_loop_stop)  # pyright: ignore[reportPrivateUsage]
    worker.start()
    assert gate.waiting.wait(timeout=1.0)
    assert driver.read_state().connected is connected_while_waiting
    assert driver._disconnect_requested.is_set() is (reception == "candidate/v1")  # pyright: ignore[reportPrivateUsage]
    gate.release()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert driver.read_state().connected is False


def test_create_roaster_safety_driver_alias_returns_mock_driver() -> None:
    driver = create_roaster_safety_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_create_roaster_driver_rejects_unsupported_driver() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        create_roaster_driver("hottop")


def test_mock_driver_satisfies_roaster_driver_contract() -> None:
    _assert_roaster_driver_contract(MockRoasterDriver())


def test_manual_first_crack_capability_is_declared_only_by_mock() -> None:
    """The static manual adapter declaration stays closed to the mock boundary."""
    mock_capabilities = MockRoasterDriver().capabilities
    hottop_capabilities = HottopRoasterDriver(serial_factory=FakeSerialFactory()).capabilities

    assert isinstance(mock_capabilities, RoasterCapabilities)
    assert mock_capabilities.manual_first_crack_version == 1
    assert hottop_capabilities.manual_first_crack_version is None


def test_hottop_driver_capabilities_require_command_streaming() -> None:
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.05,
        serial_factory=FakeSerialFactory(),
    )

    capabilities = driver.capabilities

    assert capabilities.driver == "hottop_kn8828b_2k_plus"
    assert capabilities.command_streaming.required is True
    assert capabilities.command_streaming.interval_seconds == 0.05
    assert capabilities.actions.heat_control is True
    assert capabilities.actions.fan_control is True
    assert capabilities.actions.bean_drop is True
    assert capabilities.actions.cooling_control is True
    assert capabilities.actions.emergency_stop is True


def test_build_hottop_command_packet_uses_expected_36_byte_layout() -> None:
    packet = build_hottop_command_packet(
        heat_level_percent=75,
        fan_level_percent=55,
        main_fan_level_percent=30,
        solenoid_open=True,
        drum_motor_on=True,
        cooling_motor_on=False,
    )

    assert len(packet) == HOTTOP_PACKET_LENGTH
    assert packet[:7] == bytes((0xA5, 0x96, 0xB0, 0xA0, 0x01, 0x01, 0x24))
    assert packet[10] == 75
    assert packet[11] == 6
    assert packet[12] == 3
    assert packet[16] == 1
    assert packet[17] == 1
    assert packet[18] == 0
    assert validate_hottop_packet_checksum(packet) is True
    assert packet[35] == sum(packet[:35]) & 0xFF


def test_build_hottop_command_packet_defaults_to_safe_zero_state() -> None:
    packet = build_hottop_command_packet()

    assert len(packet) == HOTTOP_PACKET_LENGTH
    assert packet[10] == 0
    assert packet[11] == 0
    assert packet[12] == 0
    assert packet[16] == 0
    assert packet[17] == 0
    assert packet[18] == 0
    assert validate_hottop_packet_checksum(packet) is True


@pytest.mark.parametrize(
    ("fan_level_percent", "expected_hottop_scale"),
    [
        (0, 0),
        (5, 1),
        (15, 2),
        (25, 3),
        (35, 4),
        (45, 5),
        (55, 6),
        (65, 7),
        (75, 8),
        (85, 9),
        (95, 10),
        (100, 10),
    ],
)
def test_build_hottop_command_packet_uses_half_up_fan_scale(
    fan_level_percent: int,
    expected_hottop_scale: int,
) -> None:
    packet = build_hottop_command_packet(
        fan_level_percent=fan_level_percent,
        main_fan_level_percent=fan_level_percent,
    )

    assert packet[11] == expected_hottop_scale
    assert packet[12] == expected_hottop_scale


@pytest.mark.parametrize(
    ("kwargs", "error_type", "match"),
    [
        ({"heat_level_percent": -1}, ValueError, "heat_level_percent"),
        ({"fan_level_percent": 101}, ValueError, "fan_level_percent"),
        ({"main_fan_level_percent": True}, TypeError, "main_fan_level_percent"),
        ({"solenoid_open": 1}, TypeError, "solenoid_open"),
        ({"drum_motor_on": 0}, TypeError, "drum_motor_on"),
        ({"cooling_motor_on": "yes"}, TypeError, "cooling_motor_on"),
    ],
)
def test_build_hottop_command_packet_rejects_invalid_fields(
    kwargs: dict[str, object],
    error_type: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error_type, match=match):
        build_hottop_command_packet(**kwargs)  # pyright: ignore[reportArgumentType]


def test_parse_hottop_status_packet_extracts_temperatures_and_preserves_raw_packet() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)

    status = parse_hottop_status_packet(packet)

    assert status.env_temp_c == 205
    assert status.bean_temp_c == 187
    assert status.raw_env_temperature == 205
    assert status.raw_bean_temperature == 187
    assert status.temperature_unit == "celsius"
    assert status.resolved_temperature_unit == "celsius"
    assert status.raw_packet == packet


def test_parse_hottop_status_packet_converts_fahrenheit_to_celsius() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)

    status = parse_hottop_status_packet(packet, temperature_unit="fahrenheit")

    assert status.env_temp_c == 205.0
    assert status.bean_temp_c == 196.7
    assert status.raw_env_temperature == 401
    assert status.raw_bean_temperature == 386
    assert status.resolved_temperature_unit == "fahrenheit"


def test_parse_hottop_status_packet_auto_uses_plausible_fahrenheit_readings() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)

    status = parse_hottop_status_packet(packet, temperature_unit="auto")

    assert status.env_temp_c == 205.0
    assert status.bean_temp_c == 196.7
    assert status.resolved_temperature_unit == "fahrenheit"


def test_parse_hottop_status_packet_ignores_startup_zero_temperatures() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0)

    status = parse_hottop_status_packet(packet, temperature_unit="auto")

    assert status.env_temp_c is None
    assert status.bean_temp_c is None
    assert status.raw_env_temperature == 0
    assert status.raw_bean_temperature == 0
    assert status.resolved_temperature_unit is None


def test_parse_hottop_status_packet_rejects_invalid_temperature_unit() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)

    with pytest.raises(ValueError, match="temperature_unit"):
        parse_hottop_status_packet(
            packet,
            temperature_unit="kelvin",  # pyright: ignore[reportArgumentType]
        )


def test_parse_hottop_status_packet_rejects_non_string_temperature_unit() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)

    with pytest.raises(TypeError, match="temperature_unit"):
        parse_hottop_status_packet(
            packet,
            temperature_unit=[],  # pyright: ignore[reportArgumentType]
        )


def test_parse_hottop_status_packet_normalizes_temperature_unit_token() -> None:
    packet = _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)

    status = parse_hottop_status_packet(packet, temperature_unit=" Fahrenheit ")  # pyright: ignore[reportArgumentType]

    assert status.temperature_unit == "fahrenheit"
    assert status.env_temp_c == 205.0
    assert status.bean_temp_c == 196.7


def test_hottop_driver_normalizes_temperature_unit_token() -> None:
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        temperature_unit=" Auto ",  # pyright: ignore[reportArgumentType]
        serial_factory=FakeSerialFactory(),
    )

    assert driver.read_state().raw_vendor_data["temperature_unit"] == "auto"


def test_parse_hottop_status_packet_rejects_command_packet_echo() -> None:
    packet = build_hottop_command_packet(
        heat_level_percent=75,
        fan_level_percent=55,
        main_fan_level_percent=30,
        solenoid_open=True,
        drum_motor_on=True,
        cooling_motor_on=False,
    )

    assert is_hottop_command_packet(packet) is True
    assert validate_hottop_packet_checksum(packet) is True
    with pytest.raises(ValueError, match="command header"):
        parse_hottop_status_packet(packet)


def test_find_hottop_status_packet_skips_noise_and_invalid_candidates() -> None:
    invalid_packet = bytearray(
        _build_hottop_status_packet(raw_env_temperature=99, raw_bean_temperature=88)
    )
    invalid_packet[35] ^= 0x01
    valid_packet = _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)
    buffer = b"noise" + bytes(invalid_packet) + b"more-noise" + valid_packet + b"tail"

    status = find_hottop_status_packet(buffer)

    assert status is not None
    assert status.env_temp_c == 205
    assert status.bean_temp_c == 187
    assert status.raw_packet == valid_packet


def test_find_hottop_status_packet_skips_command_packet_echoes() -> None:
    command_packet = build_hottop_command_packet(
        heat_level_percent=75,
        fan_level_percent=55,
        main_fan_level_percent=30,
        solenoid_open=True,
        drum_motor_on=True,
        cooling_motor_on=False,
    )
    valid_packet = _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)
    buffer = b"noise" + command_packet + valid_packet

    status = find_hottop_status_packet(buffer)

    assert status is not None
    assert status.env_temp_c == 205
    assert status.bean_temp_c == 187
    assert status.raw_packet == valid_packet


def test_find_hottop_status_packet_returns_none_when_buffer_has_no_valid_packet() -> None:
    invalid_packet = bytearray(
        _build_hottop_status_packet(raw_env_temperature=99, raw_bean_temperature=88)
    )
    invalid_packet[35] ^= 0x01

    assert find_hottop_status_packet(b"noise" + bytes(invalid_packet)) is None


@pytest.mark.parametrize(
    ("packet", "match"),
    [
        (b"", "36 bytes"),
        (bytes([0x00] * HOTTOP_PACKET_LENGTH), "A5 96"),
    ],
)
def test_parse_hottop_status_packet_rejects_invalid_packet_shape(
    packet: bytes,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_hottop_status_packet(packet)


def test_parse_hottop_status_packet_rejects_invalid_checksum() -> None:
    packet = bytearray(
        _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)
    )
    packet[35] ^= 0x01

    assert validate_hottop_packet_checksum(bytes(packet)) is False
    with pytest.raises(ValueError, match="checksum"):
        parse_hottop_status_packet(bytes(packet))


def test_hottop_driver_connect_requires_explicit_port() -> None:
    serial_factory = FakeSerialFactory()
    driver = HottopRoasterDriver(serial_factory=serial_factory)

    with pytest.raises(ValueError, match="serial port"):
        driver.connect()

    assert serial_factory.calls == []


def test_hottop_driver_connect_opens_serial_and_starts_command_loop() -> None:
    serial_factory = FakeSerialFactory()
    command_loop_iterated = Event()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        baudrate=115_200,
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_iterated.set,
    )

    driver.connect()
    try:
        state = driver.read_state()

        assert state.connected is True
        assert state.raw_vendor_data["port"] == "/dev/test-hottop"
        assert state.raw_vendor_data["baudrate"] == 115_200
        assert state.raw_vendor_data["command_loop_running"] is True
        assert serial_factory.calls == [
            (
                ("/dev/test-hottop",),
                {
                    "baudrate": 115_200,
                    "bytesize": 8,
                    "parity": "N",
                    "stopbits": 1,
                    "timeout": 0.5,
                    "write_timeout": 0.01,
                },
            )
        ]
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_streams_injected_frames() -> None:
    serial_factory = FakeSerialFactory()
    command_loop_probe = LoopIterationProbe()
    frame = b"safe-test-frame"
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_probe,
        command_frame_provider=lambda: frame,
    )

    driver.connect()
    try:
        assert command_loop_probe.wait_for_calls(3)
    finally:
        driver.disconnect()

    streaming_state = driver.read_state()
    assert len(serial_factory.transport.writes) >= 3
    assert all(write == frame for write in serial_factory.transport.writes)
    loop_iterations = streaming_state.raw_vendor_data["command_loop_iterations"]
    frame_polls = streaming_state.raw_vendor_data["command_frame_poll_count"]
    send_attempts = streaming_state.raw_vendor_data["command_send_attempts"]
    write_count = streaming_state.raw_vendor_data["command_write_count"]
    assert isinstance(loop_iterations, int)
    assert isinstance(frame_polls, int)
    assert isinstance(send_attempts, int)
    assert isinstance(write_count, int)
    assert loop_iterations >= 3
    assert frame_polls >= 3
    assert send_attempts >= 3
    assert write_count == len(serial_factory.transport.writes)
    assert streaming_state.raw_vendor_data["last_command_write_size"] == len(frame)
    assert streaming_state.raw_vendor_data["command_loop_error_count"] == 0


def test_hottop_driver_command_loop_default_frame_provider_sends_safe_zero_packet() -> None:
    serial_factory = FakeSerialFactory()
    command_loop_iterated = Event()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_iterated.set,
    )

    driver.connect()
    try:
        assert command_loop_iterated.wait(timeout=1.0)
        state = driver.read_state()

        safe_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[11] == 0
                and write[12] == 0
                and write[16] == 0
                and write[17] == 0
                and write[18] == 0
            ),
        )
        assert len(safe_packet) == HOTTOP_PACKET_LENGTH
        frame_polls = state.raw_vendor_data["command_frame_poll_count"]
        send_attempts = state.raw_vendor_data["command_send_attempts"]
        write_count = state.raw_vendor_data["command_write_count"]
        assert isinstance(frame_polls, int)
        assert isinstance(send_attempts, int)
        assert isinstance(write_count, int)
        assert frame_polls >= 1
        assert send_attempts >= 1
        assert write_count >= 1
        assert state.raw_vendor_data["last_command_write_size"] == HOTTOP_PACKET_LENGTH
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_updates_latest_celsius_status_temperatures() -> None:
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        state = _wait_for_hottop_status_count(driver, 1)

        assert state.env_temp_c == 205.0
        assert state.bean_temp_c == 187.0
        assert state.raw_vendor_data["temperature_unit"] == "celsius"
        assert state.raw_vendor_data["resolved_temperature_unit"] == "celsius"
        assert state.raw_vendor_data["raw_env_temperature"] == 205
        assert state.raw_vendor_data["raw_bean_temperature"] == 187
        assert state.raw_vendor_data["ignored_temperature_packet_count"] == 0
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_ignores_unready_temperatures_until_plausible() -> None:
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        temperature_unit="auto",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        unready_state = _wait_for_hottop_status_count(driver, 1)
        serial_factory.transport.queue_read_data(
            _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)
        )
        plausible_state = _wait_for_hottop_status_count(driver, 2)

        assert unready_state.env_temp_c is None
        assert unready_state.bean_temp_c is None
        assert unready_state.raw_vendor_data["ignored_temperature_packet_count"] == 1
        assert plausible_state.env_temp_c == 205.0
        assert plausible_state.bean_temp_c == 196.7
        assert plausible_state.raw_vendor_data["temperature_unit"] == "auto"
        assert plausible_state.raw_vendor_data["resolved_temperature_unit"] == "fahrenheit"
        assert plausible_state.raw_vendor_data["ignored_temperature_packet_count"] == 1
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_clears_resolved_unit_for_ignored_packet() -> None:
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        temperature_unit="auto",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        plausible_state = _wait_for_hottop_status_count(driver, 1)
        serial_factory.transport.queue_read_data(
            _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0)
        )
        ignored_state = _wait_for_hottop_status_count(driver, 2)

        assert plausible_state.env_temp_c == 205.0
        assert plausible_state.bean_temp_c == 196.7
        assert plausible_state.raw_vendor_data["resolved_temperature_unit"] == "fahrenheit"
        assert ignored_state.env_temp_c == 205.0
        assert ignored_state.bean_temp_c == 196.7
        assert ignored_state.raw_vendor_data["raw_env_temperature"] == 0
        assert ignored_state.raw_vendor_data["raw_bean_temperature"] == 0
        assert ignored_state.raw_vendor_data["resolved_temperature_unit"] is None
        assert ignored_state.raw_vendor_data["ignored_temperature_packet_count"] == 1
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_processes_burst_status_packets() -> None:
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=205, raw_bean_temperature=187)
        + _build_hottop_status_packet(raw_env_temperature=206, raw_bean_temperature=188)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        state = _wait_for_hottop_status_count(driver, 2)

        assert state.env_temp_c == 206.0
        assert state.bean_temp_c == 188.0
        assert state.raw_vendor_data["raw_env_temperature"] == 206
        assert state.raw_vendor_data["raw_bean_temperature"] == 188
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_caps_status_read_size() -> None:
    serial_factory = FakeSerialFactory()
    queued_packets = b"".join(
        _build_hottop_status_packet(
            raw_env_temperature=205 + index,
            raw_bean_temperature=187 + index,
        )
        for index in range(6)
    )
    serial_factory.transport.queue_read_data(queued_packets)
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        state = _wait_for_hottop_status_count(driver, 6)

        assert state.env_temp_c == 210.0
        assert state.bean_temp_c == 192.0
        assert max(serial_factory.transport.read_sizes) <= HOTTOP_PACKET_LENGTH * 4
        assert serial_factory.transport.read_sizes[0] == HOTTOP_PACKET_LENGTH * 4
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_preserves_partial_status_packet_tail() -> None:
    serial_factory = FakeSerialFactory()
    first_packet = _build_hottop_status_packet(
        raw_env_temperature=205,
        raw_bean_temperature=187,
    )
    second_packet = _build_hottop_status_packet(
        raw_env_temperature=207,
        raw_bean_temperature=189,
    )
    serial_factory.transport.queue_read_data(first_packet + second_packet[:12])
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        first_state = _wait_for_hottop_status_count(driver, 1)
        serial_factory.transport.queue_read_data(second_packet[12:])
        second_state = _wait_for_hottop_status_count(driver, 2)

        assert first_state.env_temp_c == 205.0
        assert first_state.bean_temp_c == 187.0
        assert second_state.env_temp_c == 207.0
        assert second_state.bean_temp_c == 189.0
    finally:
        driver.disconnect()


def test_hottop_driver_heat_and_fan_commands_stream_current_packet_state() -> None:
    serial_factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()
    try:
        heat_state = driver.set_heat(heat_level_percent=70)
        heat_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 70
                and write[12] == 0
                and write[17] == 1
            ),
        )

        fan_state = driver.set_fan(fan_level_percent=35)
        fan_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 70
                and write[11] == 0
                and write[12] == 4
                and write[17] == 1
            ),
        )

        assert heat_state.heat_level_percent == 70
        assert heat_state.raw_vendor_data["drum_motor_on"] is True
        assert fan_state.fan_level_percent == 35
        assert fan_state.raw_vendor_data["main_fan_level_percent"] == 35
        assert heat_packet[35] == sum(heat_packet[:35]) & 0xFF
        assert fan_packet[35] == sum(fan_packet[:35]) & 0xFF
    finally:
        driver.disconnect()


def test_hottop_driver_drop_and_cooling_commands_stream_safe_compound_states() -> None:
    serial_factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()
    try:
        driver.set_heat(heat_level_percent=80)
        drop_state = driver.drop_beans()
        # write[17] is the drum-motor command byte (_HOTTOP_COMMAND_DRUM_MOTOR_INDEX):
        # the drop must keep the drum RUNNING (==1) so beans eject (#163).
        drop_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[12] == 10
                and write[16] == 1
                and write[17] == 1
                and write[18] == 1
            ),
        )

        # start_cooling leaves the drum bit untouched (#163), so the drum keeps
        # running after the drop while the operator runs the cooling cycle.
        cooling_state = driver.start_cooling()

        stop_cooling_start_index = len(serial_factory.transport.writes_snapshot())
        stopped_state = driver.stop_cooling()
        # stop_cooling is the end-of-roast action: it must stop the drum
        # (write[17] == 0) now that drop_beans leaves it running (#163).
        stopped_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[12] == 0
                and write[16] == 0
                and write[17] == 0
                and write[18] == 0
            ),
            start_index=stop_cooling_start_index,
        )

        assert drop_state.heat_level_percent == 0
        assert drop_state.fan_level_percent == 100
        assert drop_state.cooling_on is True
        assert drop_state.raw_vendor_data["solenoid_open"] is True
        assert drop_state.raw_vendor_data["drum_motor_on"] is True
        assert cooling_state.cooling_on is True
        assert cooling_state.fan_level_percent == 100
        assert cooling_state.raw_vendor_data["drum_motor_on"] is True
        assert stopped_state.cooling_on is False
        assert stopped_state.fan_level_percent == 0
        assert stopped_state.raw_vendor_data["solenoid_open"] is False
        assert stopped_state.raw_vendor_data["drum_motor_on"] is False
        assert drop_packet[35] == sum(drop_packet[:35]) & 0xFF
        assert stopped_packet[35] == sum(stopped_packet[:35]) & 0xFF
    finally:
        driver.disconnect()


def test_hottop_driver_emergency_stop_streams_safe_stop_state() -> None:
    serial_factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()
    try:
        driver.set_heat(heat_level_percent=80)
        driver.set_fan(fan_level_percent=20)

        result = driver.emergency_stop(reason="unit-test")
        stop_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[12] == 10
                and write[16] == 0
                and write[17] == 0
                and write[18] == 1
            ),
        )
        state = driver.read_state()

        assert result.safety_method == "emergency_stop"
        assert result.heat_level_percent == 0
        assert result.fan_level_percent == 100
        assert result.cooling_on is True
        assert state.raw_vendor_data["drum_motor_on"] is False
        assert state.raw_vendor_data["solenoid_open"] is False
        assert stop_packet[35] == sum(stop_packet[:35]) & 0xFF
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_records_write_failures_without_blocking_disconnect() -> None:
    serial_factory = FakeSerialFactory(transport=FailingSerialTransport())
    command_loop_iterated = Event()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_iterated.set,
        command_frame_provider=lambda: b"safe-test-frame",
    )

    driver.connect()
    try:
        assert command_loop_iterated.wait(timeout=1.0)
        state = driver.read_state()

        send_attempts = state.raw_vendor_data["command_send_attempts"]
        error_count = state.raw_vendor_data["command_loop_error_count"]
        assert isinstance(send_attempts, int)
        assert isinstance(error_count, int)
        assert send_attempts >= 1
        assert state.raw_vendor_data["command_write_count"] == 0
        assert state.raw_vendor_data["last_command_write_size"] == 0
        assert error_count >= 1
    finally:
        driver.disconnect()


def test_hottop_driver_command_loop_resets_last_write_size_after_write_error() -> None:
    serial_factory = FakeSerialFactory(transport=OneSuccessThenFailSerialTransport())
    command_loop_probe = LoopIterationProbe()
    frame = b"safe-test-frame"
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_probe,
        command_frame_provider=lambda: frame,
    )

    driver.connect()
    try:
        assert command_loop_probe.wait_for_calls(2)
    finally:
        driver.disconnect()

    state = driver.read_state()
    send_attempts = state.raw_vendor_data["command_send_attempts"]
    error_count = state.raw_vendor_data["command_loop_error_count"]
    assert isinstance(send_attempts, int)
    assert isinstance(error_count, int)
    assert send_attempts >= 2
    assert state.raw_vendor_data["command_write_count"] == 1
    assert state.raw_vendor_data["last_command_write_size"] == 0
    assert error_count >= 1


def test_hottop_driver_command_loop_records_partial_writes_as_errors() -> None:
    serial_factory = FakeSerialFactory(transport=PartialWriteSerialTransport())
    command_loop_iterated = Event()
    frame = b"safe-test-frame"
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_iterated.set,
        command_frame_provider=lambda: frame,
    )

    driver.connect()
    try:
        assert command_loop_iterated.wait(timeout=1.0)
    finally:
        driver.disconnect()

    state = driver.read_state()
    send_attempts = state.raw_vendor_data["command_send_attempts"]
    error_count = state.raw_vendor_data["command_loop_error_count"]
    assert isinstance(send_attempts, int)
    assert isinstance(error_count, int)
    assert send_attempts >= 1
    assert state.raw_vendor_data["command_write_count"] == 0
    assert state.raw_vendor_data["last_command_write_size"] == len(frame) - 1
    assert error_count >= 1


def test_hottop_driver_disconnect_closes_serial_when_write_is_blocked() -> None:
    transport = BlockingWriteSerialTransport()
    serial_factory = FakeSerialFactory(transport=transport)
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        join_timeout_seconds=0.01,
        serial_factory=serial_factory,
        command_frame_provider=lambda: b"safe-test-frame",
    )
    driver.connect()
    assert transport.started.wait(timeout=1.0)

    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            driver.disconnect()

        assert transport.close_calls == 1
        assert transport.is_open is False
    finally:
        transport.release.set()
        driver.disconnect()


def test_hottop_driver_disconnect_publishes_stop_when_blocked_close_fails() -> None:
    transport = BlockingWriteFailingCloseTransport()
    serial_factory = FakeSerialFactory(transport=transport)
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        join_timeout_seconds=0.01,
        serial_factory=serial_factory,
        command_frame_provider=lambda: b"safe-test-frame",
    )
    driver.connect()
    assert transport.started.wait(timeout=1.0)

    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            driver.disconnect()

        state = driver.read_state()
        error_count = state.raw_vendor_data["command_loop_error_count"]
        assert state.connected is False
        assert isinstance(error_count, int)
        assert error_count >= 1
        assert state.raw_vendor_data["last_command_write_size"] == 0
        assert transport.close_calls == 2
        assert transport.is_open is False
    finally:
        transport.release.set()
        driver.disconnect()


def test_hottop_driver_command_loop_hook_error_fails_closed() -> None:
    serial_factory = FakeSerialFactory()
    raising_hook = RaisingHook()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=raising_hook,
    )
    driver.connect()
    assert raising_hook.called.wait(timeout=1.0)
    assert serial_factory.transport.closed.wait(timeout=1.0)

    state = driver.read_state()
    assert state.connected is False
    assert state.raw_vendor_data["command_loop_error_count"] == 1
    assert serial_factory.transport.close_calls == 1
    assert serial_factory.transport.is_open is False

    driver.disconnect()


def test_hottop_driver_disconnect_prevents_write_after_stop_is_requested() -> None:
    serial_factory = FakeSerialFactory()
    frame_provider = BlockingFrameProvider(b"safe-test-frame")
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_frame_provider=frame_provider,
    )
    driver.connect()
    assert frame_provider.started.wait(timeout=1.0)

    disconnect_thread = Thread(target=driver.disconnect)
    disconnect_thread.start()
    frame_provider.release.set()
    disconnect_thread.join(timeout=1.0)

    assert not disconnect_thread.is_alive()
    assert serial_factory.transport.writes == []
    assert serial_factory.transport.close_calls == 1
    assert driver.read_state().raw_vendor_data["command_write_count"] == 0


def test_hottop_driver_disconnect_serializes_close_with_pending_write() -> None:
    serial_factory = FakeSerialFactory()
    frame_provider = StopAwareFrameProvider(b"safe-test-frame")
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_frame_provider=frame_provider,
    )
    driver.connect()
    assert frame_provider.started.wait(timeout=1.0)

    disconnect_thread = Thread(target=driver.disconnect)
    disconnect_thread.start()
    frame_provider.release.set()
    disconnect_thread.join(timeout=1.0)

    assert not disconnect_thread.is_alive()
    assert serial_factory.transport.close_calls == 1
    assert serial_factory.transport.writes == []
    state = driver.read_state()
    assert state.raw_vendor_data["command_send_attempts"] == 0
    assert state.raw_vendor_data["command_write_count"] == 0


def test_hottop_driver_connect_does_not_block_state_reads_during_serial_open() -> None:
    serial_factory = BlockingSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )
    connect_thread = Thread(target=driver.connect)

    connect_thread.start()
    try:
        assert serial_factory.started.wait(timeout=1.0)
        state = driver.read_state()

        assert state.connected is False
        assert state.raw_vendor_data["command_loop_running"] is False
    finally:
        serial_factory.release.set()
        connect_thread.join(timeout=1.0)
        driver.disconnect()


def test_hottop_driver_disconnect_stops_command_loop_and_closes_serial() -> None:
    serial_factory = FakeSerialFactory()
    command_loop_iterated = Event()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_loop_iteration_hook=command_loop_iterated.set,
    )
    driver.connect()
    assert command_loop_iterated.wait(timeout=1.0)

    driver.disconnect()
    write_count_after_disconnect = len(serial_factory.transport.writes)
    disconnected_state = driver.read_state()

    assert disconnected_state.connected is False
    assert disconnected_state.raw_vendor_data["command_loop_running"] is False
    assert serial_factory.transport.is_open is False
    assert serial_factory.transport.close_calls == 1
    assert len(serial_factory.transport.writes) == write_count_after_disconnect


def test_hottop_driver_disconnect_is_idempotent() -> None:
    serial_factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()

    driver.disconnect()
    driver.disconnect()

    assert serial_factory.transport.close_calls == 1


def test_hottop_driver_disconnect_closes_serial_when_command_loop_times_out() -> None:
    serial_factory = FakeSerialFactory()
    driver = StuckHottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        join_timeout_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()

    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            driver.disconnect()

        assert serial_factory.transport.is_open is False
        assert serial_factory.transport.close_calls == 1
    finally:
        driver.release_command_loop()
        driver.disconnect()


def test_hottop_driver_blocks_reconnect_while_previous_loop_is_running() -> None:
    serial_factory = FakeSerialFactory()
    driver = StuckHottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        join_timeout_seconds=0.01,
        serial_factory=serial_factory,
    )
    driver.connect()
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            driver.disconnect()

        with pytest.raises(RuntimeError, match="still stopping"):
            driver.connect()

        assert len(serial_factory.calls) == 1
    finally:
        driver.release_command_loop()
        driver.disconnect()


def test_roaster_state_normalizes_integer_temperatures_and_copies_raw_vendor_data() -> None:
    raw_vendor_data: dict[str, str | int | float | bool | None] = {"vendor_code": 7}

    state = RoasterState(
        driver="mock",
        connected=True,
        bean_temp_c=20,
        env_temp_c=21,
        heat_level_percent=10,
        fan_level_percent=20,
        cooling_on=False,
        raw_vendor_data=raw_vendor_data,
    )
    raw_vendor_data["vendor_code"] = 8

    assert state.bean_temp_c == 20.0
    assert state.env_temp_c == 21.0
    assert state.raw_vendor_data == {"vendor_code": 7}


@pytest.mark.parametrize("driver", ["", "   "])
def test_roaster_state_rejects_empty_driver(driver: str) -> None:
    with pytest.raises(ValueError, match="driver"):
        RoasterState(
            driver=driver,
            connected=True,
            bean_temp_c=20.0,
            env_temp_c=21.0,
            heat_level_percent=10,
            fan_level_percent=20,
            cooling_on=False,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_type", "match"),
    [
        ("connected", 1, TypeError, "connected"),
        ("cooling_on", 0, TypeError, "cooling_on"),
        ("bean_temp_c", True, TypeError, "bean_temp_c"),
        ("env_temp_c", "21.0", TypeError, "env_temp_c"),
        ("bean_temp_c", float("nan"), ValueError, "bean_temp_c"),
        ("env_temp_c", float("inf"), ValueError, "env_temp_c"),
        ("heat_level_percent", -1, ValueError, "heat_level_percent"),
        ("fan_level_percent", 101, ValueError, "fan_level_percent"),
        ("heat_level_percent", True, TypeError, "heat_level_percent"),
        ("fan_level_percent", False, TypeError, "fan_level_percent"),
    ],
)
def test_roaster_state_rejects_invalid_normalized_fields(
    field_name: str,
    field_value: object,
    error_type: type[Exception],
    match: str,
) -> None:
    kwargs: dict[str, object] = {
        "driver": "mock",
        "connected": True,
        "bean_temp_c": 20.0,
        "env_temp_c": 21.0,
        "heat_level_percent": 10,
        "fan_level_percent": 20,
        "cooling_on": False,
    }
    kwargs[field_name] = field_value

    with pytest.raises(error_type, match=match):
        RoasterState(**kwargs)  # pyright: ignore[reportArgumentType]


def test_roaster_state_rejects_non_dict_raw_vendor_data() -> None:
    with pytest.raises(TypeError, match="raw_vendor_data"):
        RoasterState(
            driver="mock",
            connected=True,
            bean_temp_c=20.0,
            env_temp_c=21.0,
            heat_level_percent=10,
            fan_level_percent=20,
            cooling_on=False,
            raw_vendor_data=cast(dict[str, str | int | float | bool | None], []),
        )


def test_roaster_state_rejects_invalid_raw_vendor_data() -> None:
    with pytest.raises(TypeError, match="keys"):
        RoasterState(
            driver="mock",
            connected=True,
            bean_temp_c=20.0,
            env_temp_c=21.0,
            heat_level_percent=10,
            fan_level_percent=20,
            cooling_on=False,
            raw_vendor_data=cast(dict[str, str | int | float | bool | None], {1: "value"}),
        )

    with pytest.raises(TypeError, match="values"):
        RoasterState(
            driver="mock",
            connected=True,
            bean_temp_c=20.0,
            env_temp_c=21.0,
            heat_level_percent=10,
            fan_level_percent=20,
            cooling_on=False,
            raw_vendor_data=cast(dict[str, str | int | float | bool | None], {"nested": {}}),
        )


def test_mock_driver_returns_reproducible_telemetry_sequence() -> None:
    first_driver = MockRoasterDriver()
    second_driver = MockRoasterDriver()

    for driver in (first_driver, second_driver):
        driver.connect()
        driver.set_heat(heat_level_percent=100)
        driver.set_fan(fan_level_percent=0)

    first_sequence = _read_temperature_sequence(first_driver, sample_count=3)
    second_sequence = _read_temperature_sequence(second_driver, sample_count=3)

    assert first_sequence == second_sequence


def test_mock_driver_telemetry_responds_to_heat_and_cooling() -> None:
    driver = MockRoasterDriver()
    driver.connect()

    initial_state = driver.read_state()
    assert initial_state.bean_temp_c == 20.0
    assert initial_state.env_temp_c == 20.0

    driver.set_heat(heat_level_percent=100)
    heating_state = driver.read_state()
    assert heating_state.env_temp_c == 22.0
    assert heating_state.bean_temp_c == 20.2

    hotter_state = driver.read_state()
    assert hotter_state.env_temp_c == 24.0
    assert hotter_state.bean_temp_c == 20.6

    driver.start_cooling()
    cooling_state = driver.read_state()
    assert cooling_state.cooling_on is True
    assert cooling_state.env_temp_c == 20.5
    assert cooling_state.bean_temp_c == 20.6


def test_mock_driver_control_commands_do_not_advance_telemetry() -> None:
    driver = MockRoasterDriver()
    driver.connect()
    sampled_state = driver.read_state()

    heat_state = driver.set_heat(heat_level_percent=70)
    fan_state = driver.set_fan(fan_level_percent=30)
    drop_state = driver.drop_beans()

    assert sampled_state.raw_vendor_data["sample_index"] == 1
    assert heat_state.raw_vendor_data["sample_index"] == 1
    assert fan_state.raw_vendor_data["sample_index"] == 1
    assert drop_state.raw_vendor_data["sample_index"] == 1
    assert drop_state.bean_temp_c == sampled_state.bean_temp_c
    assert drop_state.env_temp_c == sampled_state.env_temp_c


def test_mock_driver_initial_sample_is_private_and_instance_local() -> None:
    """New mock instances expose the unchanged index-zero telemetry grammar."""
    first = MockRoasterDriver()
    second = MockRoasterDriver()

    first_state = first.set_heat(heat_level_percent=0)
    second_state = second.set_fan(fan_level_percent=0)

    for state in (first_state, second_state):
        assert type(state) is RoasterState
        assert state.bean_temp_c == state.env_temp_c == 20.0
        assert state.raw_vendor_data == {
            "beans_dropped": False,
            "sample_index": 0,
            "telemetry_model": "fixed_step_thermal_v1",
        }


def test_mock_driver_serializes_completed_samples_and_keeps_commands_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read owns one completed sample while commands only copy the prior publication.

    This is a causal concurrency witness: removing writer serialization gives
    both readers index one, reloading during assembly gives the first reader
    the second pair, and taking the writer lock from a command blocks its copy.
    """
    driver = MockRoasterDriver()
    driver.set_heat(heat_level_percent=100)
    first_computation_entered = Event()
    release_first_computation = Event()
    first_assembly_entered = Event()
    release_first_assembly = Event()
    command_completed = Event()
    second_completed = Event()
    first_result: list[RoasterState] = []
    second_result: list[RoasterState] = []
    command_result: list[RoasterState] = []
    errors: list[BaseException] = []
    first_reader_ident: list[int] = []
    original_delta = driver._environment_delta_c  # pyright: ignore[reportPrivateUsage]
    original_snapshot = driver._state_snapshot  # pyright: ignore[reportPrivateUsage]

    def hold_first_computation() -> float:
        if not first_computation_entered.is_set():
            first_computation_entered.set()
            assert release_first_computation.wait(timeout=1.0)
        return original_delta()

    def hold_first_assembly(thermal_sample: object = None) -> RoasterState:
        if first_reader_ident == [get_ident()]:
            first_assembly_entered.set()
            assert release_first_assembly.wait(timeout=1.0)
        return original_snapshot(thermal_sample)  # type: ignore[arg-type]

    def read_into(destination: list[RoasterState], completed: Event | None = None) -> None:
        try:
            if destination is first_result:
                first_reader_ident.append(get_ident())
            destination.append(driver.read_state())
        except BaseException as exc:  # noqa: BLE001 - surface worker failures in the test thread.
            errors.append(exc)
        finally:
            if completed is not None:
                completed.set()

    def copy_command_snapshot() -> None:
        try:
            command_result.append(driver.set_heat(heat_level_percent=100))
        except BaseException as exc:  # noqa: BLE001 - surface worker failures in the test thread.
            errors.append(exc)
        finally:
            command_completed.set()

    monkeypatch.setattr(driver, "_environment_delta_c", hold_first_computation)
    monkeypatch.setattr(driver, "_state_snapshot", hold_first_assembly)
    first_reader = Thread(target=read_into, args=(first_result,))
    second_reader = Thread(target=read_into, args=(second_result, second_completed))
    command = Thread(target=copy_command_snapshot)
    second_reader_started = False
    command_started = False
    first_reader.start()
    try:
        assert first_computation_entered.wait(timeout=1.0)
        command.start()
        command_started = True
        assert command_completed.wait(timeout=1.0)
        second_reader.start()
        second_reader_started = True
        release_first_computation.set()
        assert first_assembly_entered.wait(timeout=1.0)
        assert second_completed.wait(timeout=1.0)
    finally:
        release_first_computation.set()
        release_first_assembly.set()
        first_reader.join(timeout=1.0)
        if second_reader_started:
            second_reader.join(timeout=1.0)
        if command_started:
            command.join(timeout=1.0)

    assert not errors
    assert not first_reader.is_alive()
    assert second_reader_started and not second_reader.is_alive()
    assert command_started and not command.is_alive()
    assert command_result[0].raw_vendor_data["sample_index"] == 0
    assert command_result[0].bean_temp_c == command_result[0].env_temp_c == 20.0
    assert first_result[0].raw_vendor_data["sample_index"] == 1
    assert (first_result[0].bean_temp_c, first_result[0].env_temp_c) == (20.2, 22.0)
    assert second_result[0].raw_vendor_data["sample_index"] == 2
    assert (second_result[0].bean_temp_c, second_result[0].env_temp_c) == (20.6, 24.0)


def test_command_streaming_allows_required_positive_interval() -> None:
    streaming = CommandStreaming(required=True, interval_seconds=0.3)

    assert streaming.required is True
    assert streaming.interval_seconds == 0.3


@pytest.mark.parametrize(
    ("required", "interval_seconds", "match"),
    [
        (True, None, "required when command streaming is required"),
        (True, 0.0, "greater than 0"),
        (True, -0.1, "greater than 0"),
        (False, 0.3, "must be None"),
    ],
)
def test_command_streaming_rejects_inconsistent_values(
    required: bool,
    interval_seconds: float | None,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        CommandStreaming(required=required, interval_seconds=interval_seconds)


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("set_heat", {"heat_level_percent": -1}),
        ("set_heat", {"heat_level_percent": 101}),
        ("set_fan", {"fan_level_percent": -1}),
        ("set_fan", {"fan_level_percent": 101}),
    ],
)
def test_mock_driver_rejects_out_of_range_controls(
    method_name: str,
    kwargs: dict[str, int],
) -> None:
    driver = MockRoasterDriver()

    with pytest.raises(ValueError, match="between 0 and 100"):
        getattr(driver, method_name)(**kwargs)


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("set_heat", {"heat_level_percent": True}),
        ("set_fan", {"fan_level_percent": True}),
    ],
)
def test_mock_driver_rejects_bool_controls(
    method_name: str,
    kwargs: dict[str, bool],
) -> None:
    driver = MockRoasterDriver()

    with pytest.raises(TypeError, match="integer between 0 and 100"):
        getattr(driver, method_name)(**kwargs)


def test_mock_driver_emergency_stop_returns_safe_session_state() -> None:
    driver = MockRoasterDriver()
    driver.set_heat(heat_level_percent=75)
    driver.set_fan(fan_level_percent=20)

    result = driver.emergency_stop(reason="unit-test")

    assert result.driver == "mock"
    assert result.safety_method == "emergency_stop"
    assert result.heat_level_percent == 0
    assert result.fan_level_percent == 100
    assert result.cooling_on is True
    assert result.as_event_payload()["driver_safety_method_called"] is True
    assert driver.read_state().heat_level_percent == 0
    assert driver.read_state().fan_level_percent == 100
    assert driver.read_state().cooling_on is True


def test_mock_driver_start_cooling_restores_main_fan_after_stop() -> None:
    """Mock cooling restart matches the Hottop main-fan safety state."""
    driver = MockRoasterDriver()
    driver.stop_cooling()

    state = driver.start_cooling()

    assert state.cooling_on is True
    assert state.fan_level_percent == 100


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("heat_level_percent", True, "integer between 0 and 100"),
        ("main_fan_level_percent", 101, "between 0 and 100"),
        ("command_write_count", -1, "non-negative integer"),
    ),
)
def test_lifecycle_evidence_rejects_invalid_command_values(
    field: str, value: int | bool, message: str
) -> None:
    """Lifecycle evidence rejects bool controls, bounds violations, and negative counters."""
    values: dict[str, object] = {
        "driver": "mock",
        "connected": True,
        "command_streaming_required": False,
        "command_loop_running": None,
        "serial_open": None,
        "heat_level_percent": 0,
        "roast_fan_level_percent": 0,
        "main_fan_level_percent": 0,
        "drum_motor_on": False,
        "cooling_motor_on": False,
        "solenoid_open": False,
        "command_send_attempts": None,
        "command_write_count": None,
        "last_command_write_size": None,
        "command_loop_error_count": None,
        "status_packet_count": None,
        "status_read_error_count": None,
    }
    values[field] = value
    if field == "command_write_count":
        values.update(
            command_streaming_required=True,
            command_loop_running=True,
            serial_open=True,
            command_send_attempts=0,
            last_command_write_size=0,
            command_loop_error_count=0,
            status_packet_count=0,
            status_read_error_count=0,
        )

    with pytest.raises((TypeError, ValueError), match=message):
        DriverLifecycleEvidence(**cast(dict[str, object], values))  # type: ignore[arg-type]


def test_lifecycle_evidence_requires_complete_streaming_fields() -> None:
    """Streaming evidence fails closed when any lifecycle field is absent."""
    with pytest.raises(ValueError, match="must be complete"):
        DriverLifecycleEvidence(
            driver="mock",
            connected=True,
            command_streaming_required=True,
            command_loop_running=True,
            serial_open=True,
            heat_level_percent=0,
            roast_fan_level_percent=0,
            main_fan_level_percent=0,
            drum_motor_on=False,
            cooling_motor_on=False,
            solenoid_open=False,
            command_send_attempts=0,
            command_write_count=None,
            last_command_write_size=0,
            command_loop_error_count=0,
            status_packet_count=0,
            status_read_error_count=0,
        )


def test_non_streaming_lifecycle_evidence_rejects_lifecycle_fields() -> None:
    """Non-streaming drivers must not claim command-loop lifecycle evidence."""
    with pytest.raises(ValueError, match="must omit lifecycle fields"):
        DriverLifecycleEvidence(
            driver="mock",
            connected=True,
            command_streaming_required=False,
            command_loop_running=False,
            serial_open=None,
            heat_level_percent=0,
            roast_fan_level_percent=0,
            main_fan_level_percent=0,
            drum_motor_on=False,
            cooling_motor_on=False,
            solenoid_open=False,
            command_send_attempts=None,
            command_write_count=None,
            last_command_write_size=None,
            command_loop_error_count=None,
            status_packet_count=None,
            status_read_error_count=None,
        )


def test_mock_lifecycle_evidence_is_non_advancing_and_tracks_connection() -> None:
    """Mock lifecycle reads retain safe-zero state and do not advance telemetry."""
    driver = MockRoasterDriver()
    before = driver.read_lifecycle_evidence()
    first = driver.read_state()
    driver.connect()
    connected = driver.read_lifecycle_evidence()
    second = driver.read_state()
    driver.disconnect()
    disconnected = driver.read_lifecycle_evidence()

    assert before.connected is False and before.heat_level_percent == 0
    assert connected.connected is True and connected.command_streaming_required is False
    assert disconnected.connected is False
    assert first.raw_vendor_data["sample_index"] != second.raw_vendor_data["sample_index"]


def test_mock_lifecycle_evidence_tracks_heat_drop_cooling_and_emergency_stop() -> None:
    """Mock evidence reflects the same safe-zero dimensions finalisation admits."""
    driver = MockRoasterDriver()
    driver.connect()
    driver.set_heat(heat_level_percent=40)
    heated = driver.read_lifecycle_evidence()
    driver.drop_beans()
    driver.stop_cooling()
    assert driver.read_lifecycle_evidence().main_fan_level_percent == 0
    driver.set_fan(fan_level_percent=0)
    stopped = driver.read_lifecycle_evidence()
    driver.emergency_stop(reason="test")
    emergency = driver.read_lifecycle_evidence()

    assert heated.heat_level_percent == 40
    assert stopped.heat_level_percent == 0 and stopped.main_fan_level_percent == 0
    assert emergency.heat_level_percent == 0 and emergency.main_fan_level_percent == 100


def test_hottop_lifecycle_evidence_tracks_connect_disconnect_without_actuation() -> None:
    """Hottop evidence exposes retained counters and safe-zero state around its lifecycle."""
    factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop", command_interval_seconds=0.05, serial_factory=factory
    )

    before = driver.read_lifecycle_evidence()
    driver.connect()
    connected = driver.read_lifecycle_evidence()
    driver.disconnect()
    after = driver.read_lifecycle_evidence()

    assert before.connected is False
    assert before.command_loop_running is False
    assert before.serial_open is False
    assert connected.connected is True
    assert connected.command_loop_running is True
    assert connected.serial_open is True
    assert all(
        value == 0
        for value in (
            connected.heat_level_percent,
            connected.roast_fan_level_percent,
            connected.main_fan_level_percent,
        )
    )
    assert after.connected is False
    assert after.command_loop_running is False
    assert after.serial_open is False
    assert after.command_write_count == connected.command_write_count
    assert factory.transport.close_calls == 1
    assert all(
        write == build_hottop_command_packet()
        for write in factory.transport.writes_snapshot()
        if is_hottop_command_packet(write)
    )


def test_hottop_lifecycle_evidence_surfaces_transport_is_open_failure() -> None:
    """Evidence never pretends a serial-open read succeeded when it raised."""

    class IsOpenFailingTransport(FakeSerialTransport):
        def __init__(self) -> None:
            self.raise_on_is_open = False
            super().__init__()

        @property
        def is_open(self) -> bool:
            if self.raise_on_is_open:
                raise RuntimeError("serial state unreadable")
            return self._is_open

        @is_open.setter
        def is_open(self, value: bool) -> None:
            self._is_open = value

    transport = IsOpenFailingTransport()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.05,
        serial_factory=FakeSerialFactory(transport=transport),
    )
    driver.connect()
    transport.raise_on_is_open = True
    with pytest.raises(RuntimeError, match="serial state unreadable"):
        driver.read_lifecycle_evidence()
    transport.raise_on_is_open = False
    driver.disconnect()
    assert transport.close_calls == 1


@pytest.mark.parametrize(
    ("invalid_is_open", "exception_type", "message"),
    ((None, ValueError, "complete"), (1, TypeError, "boolean")),
)
def test_hottop_off_lifecycle_evidence_rejects_invalid_installed_transport_status(
    invalid_is_open: None | int,
    exception_type: type[Exception],
    message: str,
) -> None:
    """Off-mode evidence rejects a non-boolean installed transport status."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.05,
        serial_factory=FakeSerialFactory(transport=transport),
    )
    driver.connect()
    try:
        assert driver.read_lifecycle_evidence().serial_open is True
        transport.is_open = cast(Any, invalid_is_open)

        with pytest.raises(exception_type, match=message):
            driver.read_lifecycle_evidence()
    finally:
        transport.is_open = True
        driver.disconnect()

    assert transport.close_calls == 1
    assert driver.read_lifecycle_evidence().serial_open is False


@pytest.mark.parametrize("invalid_is_open", (None, 1))
def test_candidate_lifecycle_evidence_rejects_inconclusive_retained_handle_status(
    invalid_is_open: None | int,
) -> None:
    """Candidate retained handles cannot convert invalid status into closure evidence."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        serial_factory=FakeSerialFactory(transport=transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)

    transport.is_open = cast(Any, invalid_is_open)

    with pytest.raises((TypeError, ValueError), match="complete|boolean"):
        driver.read_lifecycle_evidence()


def test_hottop_lifecycle_evidence_reports_stuck_loop_after_disconnect_timeout() -> None:
    """A timed-out Hottop loop remains visible as failed disconnect confirmation evidence."""
    factory = FakeSerialFactory()
    driver = StuckHottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        join_timeout_seconds=0.01,
        serial_factory=factory,
    )
    driver.connect()
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            driver.disconnect()
        evidence = driver.read_lifecycle_evidence()
        assert evidence.connected is False
        assert evidence.command_loop_running is True
        assert evidence.serial_open is False
    finally:
        driver.release_command_loop()
        driver.disconnect()


def _cold_temperature_counters(state: RoasterState) -> tuple[object, ...]:
    """Return the four counters the projection must copy from the same snapshot."""
    return tuple(
        state.raw_vendor_data[name]
        for name in (
            "status_packet_count",
            "ignored_temperature_packet_count",
            "status_read_error_count",
            "command_loop_error_count",
        )
    )


def test_hottop_cold_temperature_projection_awaits_first_packet_before_status() -> None:
    """A fresh Hottop snapshot with no counted packet projects ordinary startup absence."""
    driver = HottopRoasterDriver(port="/dev/test-hottop", serial_factory=FakeSerialFactory())

    projection = project_cold_temperature(driver.read_state())

    assert projection == ColdTemperatureProjection(
        projection_version=1,
        outcome="awaiting_first_packet",
        configured_temperature_unit="celsius",
        last_packet_valid=False,
        value_agreement="indeterminate",
        status_packet_count=0,
        ignored_temperature_packet_count=0,
        status_read_error_count=0,
        command_loop_error_count=0,
    )


def test_hottop_cold_temperature_projection_reports_genuine_celsius_packet() -> None:
    """Real checksummed Celsius packets feed an observed, agreeing projection."""
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=22, raw_bean_temperature=21)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        state = _wait_for_hottop_status_count(driver, 1)
    finally:
        driver.disconnect()
    projection = project_cold_temperature(state)

    assert projection.outcome == "observed"
    assert projection.configured_temperature_unit == "celsius"
    assert projection.reported_temperature_unit == "celsius"
    assert projection.last_packet_valid is True
    assert projection.last_packet_bean_temp_c == 21.0
    assert projection.last_packet_env_temp_c == 22.0
    assert projection.retained_bean_temp_c == state.bean_temp_c == 21.0
    assert projection.retained_env_temp_c == state.env_temp_c == 22.0
    assert projection.value_agreement == "agree"
    assert (
        projection.status_packet_count,
        projection.ignored_temperature_packet_count,
        projection.status_read_error_count,
        projection.command_loop_error_count,
    ) == _cold_temperature_counters(state)
    assert projection.status_packet_count == 1


@pytest.mark.parametrize("temperature_unit", ["fahrenheit", "auto"])
def test_hottop_cold_temperature_projection_never_exposes_fahrenheit_numerics(
    temperature_unit: HottopTemperatureUnit,
) -> None:
    """A genuine Fahrenheit packet projects retained Celsius only, never raw numerics."""
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=72, raw_bean_temperature=70)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        temperature_unit=temperature_unit,
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        # 70 and 72 are plausible Celsius, so `auto` would resolve Celsius; use
        # values only plausible as Fahrenheit for the `auto` case.
        if temperature_unit == "auto":
            serial_factory.transport.queue_read_data(
                _build_hottop_status_packet(raw_env_temperature=401, raw_bean_temperature=386)
            )
            state = _wait_for_hottop_status_count(driver, 2)
        else:
            state = _wait_for_hottop_status_count(driver, 1)
    finally:
        driver.disconnect()
    projection = project_cold_temperature(state)

    assert state.raw_vendor_data["resolved_temperature_unit"] == "fahrenheit"
    assert projection.outcome == "observed"
    assert projection.configured_temperature_unit == temperature_unit
    assert projection.reported_temperature_unit == "fahrenheit"
    assert projection.last_packet_valid is True
    assert projection.last_packet_bean_temp_c is None
    assert projection.last_packet_env_temp_c is None
    assert projection.retained_bean_temp_c == state.bean_temp_c
    assert projection.retained_env_temp_c == state.env_temp_c
    assert projection.value_agreement == "indeterminate"


def test_hottop_cold_temperature_projection_reports_ignored_latest_packet() -> None:
    """An ignored latest packet after an accepted one keeps retained Celsius history."""
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=23, raw_bean_temperature=22)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        temperature_unit="auto",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        _wait_for_hottop_status_count(driver, 1)
        serial_factory.transport.queue_read_data(
            _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0)
        )
        state = _wait_for_hottop_status_count(driver, 2)
    finally:
        driver.disconnect()
    projection = project_cold_temperature(state)

    assert projection.outcome == "observed"
    assert projection.configured_temperature_unit == "auto"
    assert projection.reported_temperature_unit == "unknown"
    assert projection.last_packet_valid is False
    assert projection.last_packet_bean_temp_c is None
    assert projection.last_packet_env_temp_c is None
    assert projection.retained_bean_temp_c == 22.0
    assert projection.retained_env_temp_c == 23.0
    assert projection.value_agreement == "indeterminate"
    assert (projection.status_packet_count, projection.ignored_temperature_packet_count) == (2, 1)


def test_hottop_cold_temperature_projection_reports_all_ignored_packets() -> None:
    """Only ignored packets leave the projection observed with no retained history."""
    serial_factory = FakeSerialFactory()
    serial_factory.transport.queue_read_data(
        _build_hottop_status_packet(raw_env_temperature=0, raw_bean_temperature=0)
    )
    driver = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
    )

    driver.connect()
    try:
        state = _wait_for_hottop_status_count(driver, 1)
    finally:
        driver.disconnect()
    projection = project_cold_temperature(state)

    assert projection.outcome == "observed"
    assert projection.reported_temperature_unit == "unknown"
    assert projection.last_packet_valid is False
    assert projection.retained_bean_temp_c is None
    assert projection.retained_env_temp_c is None
    assert (projection.status_packet_count, projection.ignored_temperature_packet_count) == (1, 1)


def test_fixed_zero_containment_preserves_nonheat_mock_and_hottop_command_state() -> None:
    """Containment changes only heat; a released fake loop streams that command state."""
    mock = MockRoasterDriver()
    mock.set_heat(heat_level_percent=47)
    mock.set_fan(fan_level_percent=31)
    object.__setattr__(mock, "_cooling_on", True)
    object.__setattr__(mock, "_drum_motor_on", True)
    object.__setattr__(mock, "_solenoid_open", True)
    mock_evidence = mock.contain_heat_fixed_zero()

    serial_factory = FakeSerialFactory()
    command_frame_started = Event()
    release_command_frame = Event()
    release_later_frames = Event()
    frame_lock = Lock()
    frame_call_count = 0
    hottop: HottopRoasterDriver

    def blocked_current_frame() -> bytes:
        nonlocal frame_call_count
        with frame_lock:
            frame_call_count += 1
            is_first_frame = frame_call_count == 1
        command_frame_started.set()
        if is_first_frame:
            assert release_command_frame.wait(timeout=1.0)
        else:
            assert release_later_frames.wait(timeout=1.0)
        return hottop._current_command_frame()  # pyright: ignore[reportPrivateUsage]

    hottop = HottopRoasterDriver(
        port="/dev/test-hottop",
        command_interval_seconds=0.01,
        serial_factory=serial_factory,
        command_frame_provider=blocked_current_frame,
    )
    object.__setattr__(hottop, "_heat_level_percent", 47)
    object.__setattr__(hottop, "_roast_fan_level_percent", 23)
    object.__setattr__(hottop, "_main_fan_level_percent", 31)
    object.__setattr__(hottop, "_drum_motor_on", True)
    object.__setattr__(hottop, "_cooling_motor_on", True)
    object.__setattr__(hottop, "_solenoid_open", True)
    before_packet = build_hottop_command_packet(
        heat_level_percent=47,
        fan_level_percent=23,
        main_fan_level_percent=31,
        solenoid_open=True,
        drum_motor_on=True,
        cooling_motor_on=True,
    )
    hottop.connect()
    try:
        assert command_frame_started.wait(timeout=1.0)
        writes_before = len(serial_factory.transport.writes_snapshot())
        hottop_evidence = hottop.contain_heat_fixed_zero()
        # The synchronous command-state call itself never writes; the separate
        # command loop is deliberately released below to make one fake frame.
        assert len(serial_factory.transport.writes_snapshot()) == writes_before
        release_command_frame.set()
        streamed_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[11] == before_packet[11]
                and write[12] == before_packet[12]
                and write[16] == before_packet[16]
                and write[17] == before_packet[17]
                and write[18] == before_packet[18]
            ),
            start_index=writes_before,
        )
        writes_after_release = len(serial_factory.transport.writes_snapshot())
        assert writes_after_release == writes_before + 1
    finally:
        release_command_frame.set()
        release_later_frames.set()
        hottop.disconnect()

    for evidence, driver_name, roast_fan in (
        (mock_evidence, "mock", 0),
        (hottop_evidence, "hottop_kn8828b_2k_plus", 23),
    ):
        assert isinstance(evidence, FixedZeroCommandedState)
        assert evidence.as_dict() == {
            "driver": driver_name,
            "capability_version": 1,
            "heat_level_percent": 0,
            "roast_fan_level_percent": roast_fan,
            "main_fan_level_percent": 31,
            "drum_motor_on": True,
            "cooling_motor_on": True,
            "solenoid_open": True,
        }
    assert streamed_packet[35] == sum(streamed_packet[:35]) & 0xFF
    assert streamed_packet[:10] == before_packet[:10]
    assert streamed_packet[10] == 0
    assert streamed_packet[11:35] == before_packet[11:35]


@pytest.mark.parametrize("invalid_version", [1.0, "1", True])
def test_fixed_zero_commanded_state_rejects_nonexact_capability_version(
    invalid_version: object,
) -> None:
    """Fixed-zero evidence does not coerce boolean, float, or string versions."""
    with pytest.raises((TypeError, ValueError), match="capability_version"):
        FixedZeroCommandedState(
            driver="mock",
            capability_version=cast(int, invalid_version),
            heat_level_percent=0,
            roast_fan_level_percent=0,
            main_fan_level_percent=0,
            drum_motor_on=False,
            cooling_motor_on=False,
            solenoid_open=False,
        )


@pytest.mark.parametrize(
    ("field", "invalid_value", "exception"),
    (
        ("driver", "!mock!", ValueError),
        ("driver", "x" * 65, ValueError),
        ("heat_level_percent", True, TypeError),
        ("heat_level_percent", 1, ValueError),
        ("roast_fan_level_percent", 1.0, TypeError),
        ("roast_fan_level_percent", 101, ValueError),
        ("main_fan_level_percent", False, TypeError),
        ("main_fan_level_percent", -1, ValueError),
        ("drum_motor_on", 0, TypeError),
        ("cooling_motor_on", "false", TypeError),
        ("solenoid_open", None, TypeError),
    ),
)
def test_fixed_zero_commanded_state_rejects_each_closed_field(
    field: str, invalid_value: object, exception: type[Exception]
) -> None:
    """The driver-boundary eight-field value rejects every malformed dimension."""
    values: dict[str, object] = {
        "driver": "mock",
        "capability_version": 1,
        "heat_level_percent": 0,
        "roast_fan_level_percent": 0,
        "main_fan_level_percent": 0,
        "drum_motor_on": False,
        "cooling_motor_on": False,
        "solenoid_open": False,
    }
    values[field] = invalid_value
    with pytest.raises(exception):
        FixedZeroCommandedState(**cast(dict[str, Any], values))


@pytest.mark.parametrize("outcome", ["clean", "raise", "open", "unverifiable"])
def test_inhibited_start_exact_disposal_and_legacy_isolation(outcome: str) -> None:
    """Only exact verified closure releases authority, without frames or projections."""

    class Transport(FakeSerialTransport):
        def close(self) -> None:
            self.close_calls += 1
            if outcome == "raise":
                raise OSError("uncertain close")
            if outcome == "clean":
                self.is_open = False
            if outcome == "unverifiable":
                del self.is_open

    transport = Transport()
    calls: list[str] = []

    def factory(port: str, **_kwargs: Any) -> Transport:
        calls.append(port)
        return transport

    driver = HottopRoasterDriver(port="fake", serial_factory=factory)
    baseline = driver.read_state(), driver.read_lifecycle_evidence()
    owner = driver.reserve_inhibited_start("server")
    assert owner.generation == 1
    with pytest.raises(RuntimeError, match="staged"):
        driver.connect()
    driver.open_inhibited_start(owner)
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.OPEN_INHIBITED)
    assert (driver.read_state(), driver.read_lifecycle_evidence()) == baseline
    impostor = HottopStartOwner(owner.server_token, owner.generation)
    assert driver.abort_inhibited_start(impostor) is HottopStartSettlement.UNRESOLVED
    assert transport.close_calls == 0
    driver.disconnect()
    assert transport.close_calls == 0
    settlement = driver.abort_inhibited_start(owner)
    assert transport.close_calls == 1
    assert transport.writes == []
    assert (driver.read_state(), driver.read_lifecycle_evidence()) == baseline
    if outcome == "clean":
        assert settlement is HottopStartSettlement.CLEAN_CLOSED
        newer = driver.reserve_inhibited_start("newer")
        assert newer.generation == 2
        assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
        assert driver.inspect_inhibited_start()[0] is newer
        assert driver.abort_inhibited_start(newer) is HottopStartSettlement.NOT_ENTERED
    else:
        assert settlement is HottopStartSettlement.UNRESOLVED
        assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
        with pytest.raises(RuntimeError, match="staged"):
            driver.connect()
        with pytest.raises(RuntimeError, match="staged"):
            driver.reserve_inhibited_start("replacement")
        assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
        assert transport.close_calls == 1
    assert calls == ["fake"]


def test_inhibited_close_retains_replaced_handle_after_injected_corruption() -> None:
    """An injected, normally unreachable handle swap cannot falsely settle cleanup."""
    replacement = FakeSerialTransport()

    class ReplacingTransport(FakeSerialTransport):
        def close(self) -> None:
            super().close()
            # Deliberately violate lifecycle serialization to exercise the defensive guard.
            with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
                driver._start_handle = replacement  # pyright: ignore[reportPrivateUsage]

    transport = ReplacingTransport()
    factory = FakeSerialFactory(transport)
    driver = HottopRoasterDriver(port="fake-only", serial_factory=factory)
    owner = driver.reserve_inhibited_start("server")
    driver.open_inhibited_start(owner)

    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        assert driver._start_handle is replacement  # pyright: ignore[reportPrivateUsage]
    assert transport.is_open is False
    assert replacement.is_open is True
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
    with pytest.raises(RuntimeError, match="staged"):
        driver.connect()
    with pytest.raises(RuntimeError, match="staged"):
        driver.reserve_inhibited_start("newer")
    assert len(factory.calls) == 1
    assert transport.close_calls == 1
    assert replacement.close_calls == 0
    assert transport.writes == replacement.writes == []


def test_inhibited_factory_exception_retains_owner_without_handle() -> None:
    """Factory entry is irreversible uncertainty even when no handle is returned."""
    calls: list[str] = []

    def factory(port: str, **_kwargs: Any) -> Never:
        calls.append(port)
        raise OSError("entered factory")

    driver = HottopRoasterDriver(port="fake", serial_factory=factory)
    owner = driver.reserve_inhibited_start("server")
    with pytest.raises(OSError, match="entered"):
        driver.open_inhibited_start(owner)
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
    with pytest.raises(RuntimeError, match="staged"):
        driver.connect()
    assert calls == ["fake"]


def test_inhibited_missing_port_is_proven_nonentry() -> None:
    """Known refusal before OPENING can clear only its exact reservation."""
    driver = HottopRoasterDriver()
    owner = driver.reserve_inhibited_start("server")
    with pytest.raises(ValueError, match="port"):
        driver.open_inhibited_start(owner)
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.NOT_ENTERED
    assert driver.inspect_inhibited_start() == (None, None)


def test_inhibited_reservation_refuses_legacy_lifecycle() -> None:
    """Staging cannot borrow a legacy handle or command thread."""
    driver = HottopRoasterDriver(port="fake", serial_factory=FakeSerialFactory())
    driver.connect()
    try:
        with pytest.raises(RuntimeError, match="Legacy"):
            driver.reserve_inhibited_start("server")
    finally:
        driver.disconnect()


@pytest.mark.parametrize(
    ("receipt", "expected_disposition", "expected_phase"),
    [
        (HOTTOP_PACKET_LENGTH, HottopFirstWriteDisposition.WRITTEN, HottopStartPhase.WRITTEN),
        (True, HottopFirstWriteDisposition.UNKNOWN, HottopStartPhase.UNRESOLVED),
    ],
)
def test_claimed_first_write_consumes_entry_before_native_write(
    receipt: object,
    expected_disposition: HottopFirstWriteDisposition,
    expected_phase: HottopStartPhase,
) -> None:
    """One exact claim admits only one native entry, including an unknown receipt."""

    class Transport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            self.entered.set()
            assert self.release.wait(timeout=1.0)
            return cast(int, receipt)

    transport = Transport()
    driver = HottopRoasterDriver(port="fake", serial_factory=FakeSerialFactory(transport))
    owner = driver.reserve_inhibited_start("server")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    outcomes: list[HottopFirstWriteDisposition] = []

    worker = Thread(target=lambda: outcomes.append(driver.dispatch_claimed_first_write(claim)))
    worker.start()
    assert transport.entered.wait(timeout=1.0)
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.ENTERING)
    with pytest.raises(RuntimeError, match="claim"):
        driver.dispatch_claimed_first_write(claim)
    assert transport.writes == [claim.frame]
    transport.release.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert outcomes == [expected_disposition]
    assert driver.inspect_inhibited_start() == (owner, expected_phase)
    assert driver._start_write_disposition is expected_disposition  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="claim"):
        driver.dispatch_claimed_first_write(claim)
    assert driver._start_write_disposition is expected_disposition  # pyright: ignore[reportPrivateUsage]
    assert transport.writes == [claim.frame]


def test_candidate_claimed_first_write_hands_off_same_handle_and_epoch() -> None:
    """The simulated first frame becomes the sole serial stream owner once verified."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))

    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    assert transport.writes_snapshot() == [claim.frame]
    assert driver.read_lifecycle_evidence().serial_open is True
    assert claim.epoch is not None
    first_attempt = driver._attempt_history[0]  # pyright: ignore[reportPrivateUsage]
    assert (first_attempt.role, first_attempt.result, first_attempt.epoch) == (
        "ordinary",
        "accepted_by_host",
        claim.epoch,
    )
    driver.hand_off_written_start_to_stream(owner, session)
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.STREAMING)
    assert _wait_for_hottop_write(transport, lambda _frame: True, start_index=1) == claim.frame
    assert driver._attempt_history[-1].epoch == claim.epoch  # pyright: ignore[reportPrivateUsage]

    driver.disconnect()

    assert driver.inspect_inhibited_start() == (None, None)
    replacement = driver.reserve_inhibited_start("replacement", purpose="roast")
    assert driver.abort_inhibited_start(replacement) is HottopStartSettlement.NOT_ENTERED


def test_candidate_handoff_start_exception_before_native_launch_retains_closed_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-launch start exception retains the exact attempt without streaming."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN

    def fail_before_native_launch(_worker: Thread) -> Never:
        raise RuntimeError("synthetic pre-launch failure")

    monkeypatch.setattr(Thread, "start", fail_before_native_launch)
    with pytest.raises(RuntimeError, match="pre-launch"):
        driver.hand_off_written_start_to_stream(owner, session)

    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver._command_thread is not None  # pyright: ignore[reportPrivateUsage]
    assert transport.writes_snapshot() == [claim.frame]
    driver.disconnect()
    assert driver.inspect_inhibited_start() == (None, None)


def test_candidate_handoff_start_exception_after_native_launch_retains_closed_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-launch start exception cannot admit a second command frame."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    original_start = Thread.start
    native_launch_returned = Event()

    def start_then_raise(worker: Thread) -> None:
        original_start(worker)
        native_launch_returned.set()
        raise RuntimeError("synthetic post-launch failure")

    monkeypatch.setattr(Thread, "start", start_then_raise)
    with pytest.raises(RuntimeError, match="post-launch"):
        driver.hand_off_written_start_to_stream(owner, session)

    assert native_launch_returned.is_set()
    worker = driver._command_thread  # pyright: ignore[reportPrivateUsage]
    assert worker is not None
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert transport.writes_snapshot() == [claim.frame]
    driver.disconnect()
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert driver.inspect_inhibited_start() == (None, None)


def test_candidate_retained_handoff_close_failure_does_not_report_closure() -> None:
    """A retained failed-handoff handle remains open until a later exact close succeeds."""

    class FailOnceCloseTransport(FakeSerialTransport):
        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("synthetic close failure")
            self.is_open = False
            self.closed.set()

    transport = FailOnceCloseTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    original_start = Thread.start

    def start_then_raise(worker: Thread) -> None:
        original_start(worker)
        raise RuntimeError("synthetic post-launch failure")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Thread, "start", start_then_raise)
        with pytest.raises(RuntimeError, match="post-launch"):
            driver.hand_off_written_start_to_stream(owner, session)

    with pytest.raises(OSError, match="close failure"):
        driver.disconnect()

    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver.read_lifecycle_evidence().serial_open is True
    driver.disconnect()
    assert transport.close_calls == 2
    assert driver.inspect_inhibited_start() == (None, None)
    assert driver.read_lifecycle_evidence().serial_open is False


def test_candidate_disconnect_before_open_preserves_known_nonentry() -> None:
    """A reserved candidate with no handle cannot be mistaken for a stream owner."""
    factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="fake", serial_factory=factory, hottop_reception="candidate/v1"
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")

    driver.disconnect()

    assert factory.calls == []
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.RESERVED)
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.NOT_ENTERED


def test_candidate_stop_after_full_first_write_refuses_handoff_without_second_frame() -> None:
    """A Stop after candidate entry cannot stream a second command frame."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))

    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    driver.emergency_stop(reason="test")

    with pytest.raises(RuntimeError, match="cannot hand off"):
        driver.hand_off_written_start_to_stream(owner, session)

    assert transport.writes_snapshot() == [claim.frame]


def test_candidate_deferred_ambiguous_worker_cannot_write_to_replacement_transport() -> None:
    """An old ambiguous worker retains its handle gate across a clean replacement connect."""

    class OrderedTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_write = Event()
            self.streaming_write = Event()
            self.stale_replacement_write = Event()
            self._stale_writer_identity: int | None = None
            self._writer_identities: list[int] = []
            self._writer_lock = Lock()

        def track_stale_writer(self, identity: int) -> None:
            """Record the real deferred worker identity for replacement-write detection."""
            with self._writer_lock:
                self._stale_writer_identity = identity

        def write(self, data: bytes) -> int:
            receipt = super().write(data)
            with self._writer_lock:
                writer_identity = get_ident()
                self._writer_identities.append(writer_identity)
                if writer_identity == self._stale_writer_identity:
                    self.stale_replacement_write.set()
            if len(self.writes_snapshot()) == 1:
                self.first_write.set()
            else:
                self.streaming_write.set()
            return receipt

    old_transport = OrderedTransport()
    replacement_transport = OrderedTransport()
    transports = iter((old_transport, replacement_transport))

    def factory(*_args: object, **_kwargs: object) -> OrderedTransport:
        return next(transports)

    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=0.01,
        serial_factory=factory,
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    assert old_transport.first_write.wait(timeout=1.0)
    deferred_workers: list[Thread] = []

    def defer_then_raise(worker: Thread) -> None:
        deferred_workers.append(worker)
        raise RuntimeError("synthetic ambiguous launch")

    with pytest.MonkeyPatch.context() as launch_patch:
        launch_patch.setattr(Thread, "start", defer_then_raise)
        with pytest.raises(RuntimeError, match="ambiguous launch"):
            driver.hand_off_written_start_to_stream(owner, session)

    deferred_worker = deferred_workers.pop()
    with driver._state_lock:  # pyright: ignore[reportPrivateUsage]
        deferred_admission = driver._start_stream_admission  # pyright: ignore[reportPrivateUsage]
    assert isinstance(deferred_admission, Event)
    deferred_admission.set()
    driver.disconnect()

    driver.connect()
    assert replacement_transport.streaming_write.wait(timeout=1.0)
    stale_worker_started = Event()

    def run_deferred_worker() -> None:
        replacement_transport.track_stale_writer(get_ident())
        stale_worker_started.set()
        deferred_worker.run()

    stale_worker = Thread(target=run_deferred_worker)
    stale_worker.start()
    try:
        assert stale_worker_started.wait(timeout=1.0)
        stale_worker.join(timeout=1.0)
        assert not stale_worker.is_alive()
        assert replacement_transport.streaming_write.is_set()
        assert driver.read_lifecycle_evidence().command_loop_running is True
        assert not replacement_transport.stale_replacement_write.is_set()
        assert old_transport.writes_snapshot() == [claim.frame]
    finally:
        driver.disconnect()
        stale_worker.join(timeout=1.0)

    assert not stale_worker.is_alive()


def test_candidate_stop_before_admission_records_no_first_write_attempt() -> None:
    """A candidate Stop after claim supersedes first-byte admission without native I/O."""
    transport = FakeSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    driver.emergency_stop(reason="test")

    with pytest.raises(RuntimeError, match="superseded before admission"):
        driver.dispatch_claimed_first_write(claim)

    assert transport.writes_snapshot() == []
    assert driver._attempt_history == []  # pyright: ignore[reportPrivateUsage]
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.CLAIMED)


def test_candidate_pending_stop_refuses_reservation_before_factory_open() -> None:
    """A pending candidate Stop fences a later Start before it can open a transport."""
    factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="fake", serial_factory=factory, hottop_reception="candidate/v1"
    )
    driver.emergency_stop(reason="test")

    with pytest.raises(RuntimeError, match="known-clear"):
        driver.reserve_inhibited_start("server", purpose="roast")

    assert factory.calls == []


@pytest.mark.parametrize("purpose", [None, "cold_characterisation"])
def test_candidate_reservation_requires_explicit_roast_purpose_before_factory_open(
    purpose: str | None,
) -> None:
    """Candidate staging cannot open a simulated handle for a non-roast purpose."""
    factory = FakeSerialFactory()
    driver = HottopRoasterDriver(
        port="fake", serial_factory=factory, hottop_reception="candidate/v1"
    )

    with pytest.raises(ValueError, match="explicit roast"):
        driver.reserve_inhibited_start("server", purpose=cast(Any, purpose))

    assert factory.calls == []
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    assert owner.purpose == "roast"
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.NOT_ENTERED


@pytest.mark.parametrize("receipt", [True, HOTTOP_PACKET_LENGTH - 1])
def test_candidate_first_write_requires_an_exact_integer_receipt(receipt: object) -> None:
    """A bool or short candidate receipt retains unknown first-write ownership."""

    class Transport(FakeSerialTransport):
        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return cast(int, receipt)

    transport = Transport()
    driver = HottopRoasterDriver(
        port="fake",
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))

    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.UNKNOWN
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver._attempt_history[-1].result == "unknown"  # pyright: ignore[reportPrivateUsage]


def test_candidate_first_write_exception_retains_unknown_ownership() -> None:
    """A raising candidate first write records unknown receipt evidence and does not stream."""
    transport = FailingSerialTransport()
    driver = HottopRoasterDriver(
        port="fake",
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))

    with pytest.raises(OSError, match="serial write failed"):
        driver.dispatch_claimed_first_write(claim)

    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver.read_lifecycle_evidence().serial_open is True
    assert driver._attempt_history[-1].result == "unknown"  # pyright: ignore[reportPrivateUsage]


def test_candidate_stop_during_blocked_first_write_is_not_delayed() -> None:
    """Candidate Stop remains state-lock-only while the first native write is blocked."""

    class HeldWriteTransport(FakeSerialTransport):
        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        def write(self, data: bytes) -> int:
            with self._write_lock:
                self.writes.append(data)
            self.started.set()
            assert self.release.wait(timeout=4.0)
            return len(data)

    transport = HeldWriteTransport()
    driver = HottopRoasterDriver(
        port="fake",
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    outcomes: list[HottopFirstWriteDisposition] = []
    worker = Thread(target=lambda: outcomes.append(driver.dispatch_claimed_first_write(claim)))
    worker.start()
    assert transport.started.wait(timeout=1.0)

    stop_completed = Event()

    def stop() -> None:
        driver.emergency_stop(reason="test")
        stop_completed.set()

    stopper = Thread(target=stop)
    try:
        stopper.start()
        assert stop_completed.wait(timeout=1.0)
        assert not transport.release.is_set()
        assert transport.writes_snapshot() == [claim.frame]
    finally:
        transport.release.set()
        stopper.join(timeout=1.0)
        worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert not stopper.is_alive()
    assert outcomes == [HottopFirstWriteDisposition.WRITTEN]
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.WRITTEN)


def test_candidate_disconnect_close_failure_retains_stream_owner() -> None:
    """A failed candidate stream close fences reconnect behind unresolved ownership."""

    class FailingCloseTransport(FakeSerialTransport):
        def close(self) -> None:
            self.close_calls += 1
            raise OSError("close failed")

    transport = FailingCloseTransport()
    driver = HottopRoasterDriver(
        port="fake",
        command_interval_seconds=1.0,
        serial_factory=FakeSerialFactory(transport),
        hottop_reception="candidate/v1",
    )
    owner = driver.reserve_inhibited_start("server", purpose="roast")
    driver.open_inhibited_start(owner)
    driver.begin_inhibited_start_promotion(owner)
    session = object()
    driver.bind_promoted_inhibited_start(owner, session)
    claim = driver.claim_prepared_first_write(driver.prepare_promoted_first_write(owner, session))
    assert driver.dispatch_claimed_first_write(claim) is HottopFirstWriteDisposition.WRITTEN
    driver.hand_off_written_start_to_stream(owner, session)

    with pytest.raises(OSError, match="close failed"):
        driver.disconnect()

    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    with pytest.raises(RuntimeError, match="staged"):
        driver.connect()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_inhibited_close_interruption_propagates_and_retains_exact_handle(
    interruption: type[BaseException],
) -> None:
    """Process interruption must not suppress cancellation or discard uncertain identity."""

    class Transport(FakeSerialTransport):
        def close(self) -> None:
            self.close_calls += 1
            raise interruption()

    transport = Transport()
    driver = HottopRoasterDriver(port="fake", serial_factory=FakeSerialFactory(transport=transport))
    owner = driver.reserve_inhibited_start("server")
    driver.open_inhibited_start(owner)
    with pytest.raises(interruption):
        driver.abort_inhibited_start(owner)
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.UNRESOLVED)
    assert driver._start_handle is transport  # pyright: ignore[reportPrivateUsage]
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.UNRESOLVED
    assert transport.close_calls == 1
    assert transport.writes == []


def test_inhibited_foreign_open_cannot_enter_factory() -> None:
    """Caller text and a copied generation never reconstruct the minted capability."""
    factory = FakeSerialFactory()
    driver = HottopRoasterDriver(port="fake", serial_factory=factory)
    owner = driver.reserve_inhibited_start("server")
    with pytest.raises(RuntimeError, match="not reserved"):
        driver.open_inhibited_start(HottopStartOwner(owner.server_token, owner.generation))
    assert driver.inspect_inhibited_start() == (owner, HottopStartPhase.RESERVED)
    assert driver.abort_inhibited_start(owner) is HottopStartSettlement.NOT_ENTERED
