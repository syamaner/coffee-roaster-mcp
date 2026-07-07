"""Unit coverage for the session-owned ambient sensor runtime (#185)."""

from __future__ import annotations

from coffee_roaster_mcp.ambient import AmbientReaderError, AmbientReading
from coffee_roaster_mcp.ambient_runtime import (
    AmbientSessionRuntime,
    build_ambient_session_runtime,
)
from coffee_roaster_mcp.config import AmbientConfig, AppConfig
from coffee_roaster_mcp.session import RoastSessionStore


class ClockHarness:
    def __init__(self, *, start: float = 100.0) -> None:
        self.value = start

    def monotonic_now(self) -> float:
        return self.value


class FakeAmbientReader:
    def __init__(self, readings: list[AmbientReading | Exception]) -> None:
        self._readings = list(readings)
        self.read_count = 0
        self.closed = False

    def read(self) -> AmbientReading:
        self.read_count += 1
        outcome = self._readings.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def _reading(*, temperature_c: float = 20.0, monotonic_seconds: float = 100.0) -> AmbientReading:
    return AmbientReading(
        temperature_c=temperature_c,
        humidity_percent=45.0,
        pressure_hpa=1013.0,
        monotonic_seconds=monotonic_seconds,
    )


def test_disabled_mode_never_builds_a_reader() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    calls: list[AmbientConfig] = []

    def factory(config: AmbientConfig) -> FakeAmbientReader:
        calls.append(config)
        raise AssertionError("reader factory should not be called when disabled")

    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="disabled")),
        reader_factory=factory,
    )

    snapshot = runtime.start_for_session(session)

    assert snapshot.status == "disabled"
    assert snapshot.ambient_running is False
    assert calls == []

    polled = runtime.poll()
    assert polled.status == "disabled"


def test_start_for_session_fails_soft_when_reader_construction_raises() -> None:
    store = RoastSessionStore()
    session = store.start_session()

    def factory(config: AmbientConfig) -> FakeAmbientReader:
        raise AmbientReaderError("no Yocto-Meteo device found on the USB bus")

    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=factory,
    )

    snapshot = runtime.start_for_session(session)

    assert snapshot.status == "unavailable"
    assert snapshot.reason is not None
    assert "no Yocto-Meteo device found" in snapshot.reason
    assert snapshot.ambient_running is False


def test_start_for_session_fails_soft_on_unexpected_exception() -> None:
    store = RoastSessionStore()
    session = store.start_session()

    def factory(config: AmbientConfig) -> FakeAmbientReader:
        raise RuntimeError("unexpected native failure")

    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=factory,
    )

    snapshot = runtime.start_for_session(session)

    assert snapshot.status == "unavailable"
    assert snapshot.reason is not None
    assert "RuntimeError" in snapshot.reason


def test_poll_caches_reading_and_refreshes_only_after_interval() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    clock = ClockHarness(start=100.0)
    reader = FakeAmbientReader([_reading(temperature_c=20.0), _reading(temperature_c=21.0)])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce", poll_interval_seconds=10.0)),
        reader_factory=lambda _: reader,
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    first_poll = runtime.poll()
    assert first_poll.status == "ok"
    assert first_poll.temperature_c == 20.0
    assert reader.read_count == 1

    # Within the poll interval: cached reading is returned, no second read.
    clock.value = 105.0
    cached_poll = runtime.poll()
    assert cached_poll.temperature_c == 20.0
    assert reader.read_count == 1

    # Past the poll interval: a fresh read happens.
    clock.value = 111.0
    refreshed_poll = runtime.poll()
    assert refreshed_poll.temperature_c == 21.0
    assert reader.read_count == 2


def test_poll_fails_soft_on_read_error_and_preserves_last_reading() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    clock = ClockHarness(start=100.0)
    reader = FakeAmbientReader(
        [
            _reading(temperature_c=20.0),
            AmbientReaderError("device disconnected"),
        ]
    )
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce", poll_interval_seconds=1.0)),
        reader_factory=lambda _: reader,
        monotonic_now=clock.monotonic_now,
    )

    runtime.start_for_session(session)
    first_poll = runtime.poll()
    assert first_poll.status == "ok"
    assert first_poll.temperature_c == 20.0

    clock.value = 102.0
    failed_poll = runtime.poll()

    assert failed_poll.status == "unavailable"
    assert failed_poll.reason is not None
    assert "device disconnected" in failed_poll.reason
    # The last-known-good reading is preserved rather than blanked.
    assert failed_poll.temperature_c == 20.0


def test_poll_fails_soft_on_unexpected_read_exception() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    reader = FakeAmbientReader([RuntimeError("native crash")])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: reader,
    )

    runtime.start_for_session(session)
    snapshot = runtime.poll()

    assert snapshot.status == "unavailable"
    assert snapshot.reason is not None
    assert "RuntimeError" in snapshot.reason


def test_poll_is_a_no_op_when_runtime_never_started() -> None:
    runtime = AmbientSessionRuntime(config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")))

    snapshot = runtime.poll()

    assert snapshot.status == "unavailable"
    assert snapshot.ambient_running is False


def test_stop_for_session_closes_reader_and_ignores_other_sessions() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    reader = FakeAmbientReader([_reading()])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: reader,
    )
    runtime.start_for_session(session)
    runtime.poll()

    ignored = runtime.stop_for_session("some-other-session-id", reason="ignored")
    assert ignored.ambient_running is True
    assert reader.closed is False

    stopped = runtime.stop_for_session(session.id, reason="beans dropped")
    assert stopped.ambient_running is False
    assert reader.closed is True


def test_shutdown_closes_active_reader() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    reader = FakeAmbientReader([_reading()])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: reader,
    )
    runtime.start_for_session(session)
    runtime.poll()

    snapshot = runtime.shutdown()

    assert snapshot.ambient_running is False
    assert reader.closed is True


def test_shutdown_tolerates_reader_close_failure() -> None:
    class ExplodingCloseReader(FakeAmbientReader):
        def close(self) -> None:
            raise RuntimeError("close failed")

    store = RoastSessionStore()
    session = store.start_session()
    reader = ExplodingCloseReader([_reading()])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: reader,
    )
    runtime.start_for_session(session)
    runtime.poll()

    snapshot = runtime.shutdown()  # must not raise

    assert snapshot.ambient_running is False


def test_start_for_session_resets_stale_state_from_a_previous_session() -> None:
    store = RoastSessionStore()
    first_session = store.start_session()
    reader = FakeAmbientReader([_reading(temperature_c=19.0)])
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: reader,
    )
    runtime.start_for_session(first_session)
    runtime.poll()
    runtime.stop_for_session(first_session.id, reason="roast complete")

    store.stop_session()
    second_session = store.start_session()
    second_reader = FakeAmbientReader([])
    runtime_with_new_reader = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: second_reader,
    )
    snapshot = runtime_with_new_reader.start_for_session(second_session)

    assert snapshot.temperature_c is None
    assert snapshot.last_reading_monotonic_seconds is None


def test_stop_for_session_on_disabled_mode_is_a_no_op() -> None:
    store = RoastSessionStore()
    session = store.start_session()
    runtime = AmbientSessionRuntime(config=AppConfig(ambient=AmbientConfig(mode="disabled")))
    runtime.start_for_session(session)

    snapshot = runtime.stop_for_session(session.id, reason="beans dropped")

    assert snapshot.status == "disabled"
    assert snapshot.ambient_running is False


def test_stop_for_session_tolerates_a_reader_without_close() -> None:
    class NoCloseReader:
        def read(self) -> AmbientReading:
            return _reading()

    store = RoastSessionStore()
    session = store.start_session()
    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=lambda _: NoCloseReader(),
    )
    runtime.start_for_session(session)

    snapshot = runtime.stop_for_session(session.id, reason="beans dropped")  # must not raise

    assert snapshot.ambient_running is False


def test_stop_for_session_when_status_never_reached_ok_keeps_unavailable_reason() -> None:
    store = RoastSessionStore()
    session = store.start_session()

    def factory(config: AmbientConfig) -> FakeAmbientReader:
        raise AmbientReaderError("no device present")

    runtime = AmbientSessionRuntime(
        config=AppConfig(ambient=AmbientConfig(mode="yoctopuce")),
        reader_factory=factory,
    )
    runtime.start_for_session(session)

    snapshot = runtime.stop_for_session(session.id, reason="beans dropped")

    assert snapshot.status == "unavailable"
    assert snapshot.reason is not None
    assert "no device present" in snapshot.reason


def test_build_ambient_session_runtime_returns_configured_runtime() -> None:
    runtime = build_ambient_session_runtime(AppConfig(ambient=AmbientConfig(mode="disabled")))

    assert isinstance(runtime, AmbientSessionRuntime)
    snapshot = runtime.snapshot()
    assert snapshot.status == "disabled"
