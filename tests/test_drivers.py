"""Roaster driver contract, capability, and safety behavior coverage."""

from collections.abc import Callable
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import cast

import pytest

from coffee_roaster_mcp.drivers import (
    HOTTOP_PACKET_LENGTH,
    HOTTOP_PACKET_PREFIX,
    CommandStreaming,
    HottopRoasterDriver,
    MockRoasterDriver,
    RoasterDriver,
    RoasterState,
    SerialTransportFactory,
    build_hottop_command_packet,
    calculate_hottop_packet_checksum,
    create_roaster_driver,
    create_roaster_safety_driver,
    find_hottop_status_packet,
    is_hottop_command_packet,
    parse_hottop_status_packet,
    validate_hottop_packet_checksum,
)


class FakeSerialTransport:
    """Mock serial transport for Hottop lifecycle tests."""

    def __init__(self) -> None:
        self.is_open = True
        self.close_calls = 0
        self.closed = Event()
        self.writes: list[bytes] = []
        self._write_lock = Lock()

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False
        self.closed.set()

    def write(self, data: bytes) -> int:
        with self._write_lock:
            self.writes.append(data)
        return len(data)

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

    def _command_loop(self) -> None:
        """Keep running so disconnect timeout handling can be tested."""
        self._release_command_loop.wait()


def _build_hottop_status_packet(*, env_temp_c: int, bean_temp_c: int) -> bytes:
    """Build a minimal valid Hottop status packet for parser tests."""
    packet = bytearray(HOTTOP_PACKET_LENGTH)
    packet[: len(HOTTOP_PACKET_PREFIX)] = HOTTOP_PACKET_PREFIX
    packet[23] = env_temp_c // 256
    packet[24] = env_temp_c % 256
    packet[25] = bean_temp_c // 256
    packet[26] = bean_temp_c % 256
    packet[35] = calculate_hottop_packet_checksum(packet)
    return bytes(packet)


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
    driver = create_roaster_driver("hottop_kn8828b_2k_plus")

    assert isinstance(driver, HottopRoasterDriver)


def test_create_roaster_safety_driver_alias_returns_mock_driver() -> None:
    driver = create_roaster_safety_driver("mock")

    assert isinstance(driver, MockRoasterDriver)


def test_create_roaster_driver_rejects_unsupported_driver() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        create_roaster_driver("hottop")


def test_mock_driver_satisfies_roaster_driver_contract() -> None:
    _assert_roaster_driver_contract(MockRoasterDriver())


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
    packet = _build_hottop_status_packet(env_temp_c=205, bean_temp_c=187)

    status = parse_hottop_status_packet(packet)

    assert status.env_temp_c == 205
    assert status.bean_temp_c == 187
    assert status.raw_packet == packet


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
    invalid_packet = bytearray(_build_hottop_status_packet(env_temp_c=99, bean_temp_c=88))
    invalid_packet[35] ^= 0x01
    valid_packet = _build_hottop_status_packet(env_temp_c=205, bean_temp_c=187)
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
    valid_packet = _build_hottop_status_packet(env_temp_c=205, bean_temp_c=187)
    buffer = b"noise" + command_packet + valid_packet

    status = find_hottop_status_packet(buffer)

    assert status is not None
    assert status.env_temp_c == 205
    assert status.bean_temp_c == 187
    assert status.raw_packet == valid_packet


def test_find_hottop_status_packet_returns_none_when_buffer_has_no_valid_packet() -> None:
    invalid_packet = bytearray(_build_hottop_status_packet(env_temp_c=99, bean_temp_c=88))
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
    packet = bytearray(_build_hottop_status_packet(env_temp_c=205, bean_temp_c=187))
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
        drop_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[12] == 10
                and write[16] == 1
                and write[17] == 0
                and write[18] == 1
            ),
        )

        stop_cooling_start_index = len(serial_factory.transport.writes_snapshot())
        stopped_state = driver.stop_cooling()
        stopped_packet = _wait_for_hottop_write(
            serial_factory.transport,
            lambda write: (
                is_hottop_command_packet(write)
                and validate_hottop_packet_checksum(write)
                and write[10] == 0
                and write[12] == 0
                and write[16] == 0
                and write[18] == 0
            ),
            start_index=stop_cooling_start_index,
        )

        assert drop_state.heat_level_percent == 0
        assert drop_state.fan_level_percent == 100
        assert drop_state.cooling_on is True
        assert drop_state.raw_vendor_data["solenoid_open"] is True
        assert drop_state.raw_vendor_data["drum_motor_on"] is False
        assert stopped_state.cooling_on is False
        assert stopped_state.fan_level_percent == 0
        assert stopped_state.raw_vendor_data["solenoid_open"] is False
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
    assert cooling_state.env_temp_c == 21.0
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
