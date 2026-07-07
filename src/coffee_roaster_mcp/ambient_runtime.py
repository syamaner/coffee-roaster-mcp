"""Session-owned ambient sensor runtime orchestration (#185).

Mirrors the shape of `first_crack_runtime.FirstCrackSessionRuntime`, simplified
for a periodically-polled sensor rather than a streaming detector: there is no
audio pipeline, no windowing, and no detector adapter. The runtime owns a
lazy-refresh-with-staleness cache so `get_roast_state` (polled at ~1 Hz by the
agent) never hits the USB bus more than once per `poll_interval_seconds`, and
every read is fail-soft: an absent, unplugged, or erroring probe moves the
runtime to `unavailable` and is never allowed to raise out of the session-read
hot path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Literal

from coffee_roaster_mcp.ambient import (
    AmbientReader,
    AmbientReaderError,
    AmbientReading,
    build_configured_ambient_reader,
)
from coffee_roaster_mcp.config import AmbientConfig, AppConfig
from coffee_roaster_mcp.session import RoastSession

_LOGGER = logging.getLogger(__name__)

AmbientRuntimeState = Literal["disabled", "ok", "unavailable"]

AmbientReaderFactory = Callable[[AmbientConfig], AmbientReader]


@dataclass(frozen=True)
class AmbientRuntimeSnapshot:
    """MCP-visible ambient runtime status.

    Attributes:
        status: Current runtime state.
        active_session_id: Session id that owns the runtime, if any.
        reason: Human-readable status detail.
        ambient_running: Whether the runtime is prepared to poll readings for
            the owning session (mirrors `audio_running` in the FC snapshot).
        temperature_c: Last-known ambient temperature in Celsius, or `None`.
        humidity_percent: Last-known relative humidity percentage, or `None`.
        pressure_hpa: Last-known barometric pressure in hectopascals, or `None`.
        last_reading_monotonic_seconds: Monotonic timestamp of the last
            successful reading, or `None` if none has ever succeeded.
    """

    status: AmbientRuntimeState
    active_session_id: str | None
    reason: str | None = None
    ambient_running: bool = False
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    last_reading_monotonic_seconds: float | None = None


class AmbientSessionRuntime:
    """Own ambient sensor polling for roast sessions.

    Read-only and fail-soft by design: no method here ever raises out to a
    caller on the state-read path. A configuration or hardware problem is
    always surfaced as an `unavailable` snapshot instead.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        reader_factory: AmbientReaderFactory | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        """Initialize a session-owned ambient runtime.

        Args:
            config: Application configuration.
            reader_factory: Optional test double for ambient reader construction.
            monotonic_now: Optional monotonic clock supplier for tests.
        """
        self._config = config
        self._reader_factory = reader_factory or build_configured_ambient_reader
        self._monotonic_now = monotonic_now or time.monotonic
        self._lock = RLock()
        self._active_session_id: str | None = None
        self._reader: AmbientReader | None = None
        self._status: AmbientRuntimeState = _initial_status(config.ambient)
        self._reason: str | None = _initial_reason(config.ambient)
        self._last_reading: AmbientReading | None = None
        self._last_refresh_monotonic_seconds: float | None = None

    def start_for_session(self, session: RoastSession) -> AmbientRuntimeSnapshot:
        """Start or prepare ambient sensing for a roast session.

        Fail-soft: any reader-construction error (missing dependency, no
        device present) is caught here and reported as `unavailable` rather
        than raised, so a roast session always starts regardless of probe
        health.
        """
        with self._lock:
            self._active_session_id = session.id
            self._last_reading = None
            self._last_refresh_monotonic_seconds = None
            self._reader = None

            if self._config.ambient.mode == "disabled":
                self._status = "disabled"
                self._reason = "Ambient sensing is disabled by configuration."
                return self.snapshot()

            try:
                self._reader = self._reader_factory(self._config.ambient)
            except AmbientReaderError as exc:
                self._status = "unavailable"
                self._reason = f"Ambient sensor is unavailable: {exc}"
                self._reader = None
                return self.snapshot()
            except Exception as exc:  # noqa: BLE001 - dependency backends vary.
                self._status = "unavailable"
                self._reason = f"Ambient sensor could not be prepared: {type(exc).__name__}: {exc}"
                self._reader = None
                return self.snapshot()

            self._status = "ok"
            self._reason = None
            return self.snapshot()

    def poll(self) -> AmbientRuntimeSnapshot:
        """Refresh the cached reading if the poll interval has elapsed.

        Bounded, fail-soft refresh intended to be called from the
        `get_roast_state` hot path: a read is only attempted once per
        `poll_interval_seconds`, and any read failure demotes the runtime to
        `unavailable` while preserving the last-known-good reading in the
        snapshot rather than raising.
        """
        with self._lock:
            if self._config.ambient.mode == "disabled" or self._reader is None:
                return self.snapshot()

            now = self._monotonic_now()
            if (
                self._last_refresh_monotonic_seconds is not None
                and now - self._last_refresh_monotonic_seconds
                < self._config.ambient.poll_interval_seconds
            ):
                return self.snapshot()

            try:
                reading = self._reader.read()
            except AmbientReaderError as exc:
                self._last_refresh_monotonic_seconds = now
                self._status = "unavailable"
                self._reason = f"Ambient sensor read failed: {exc}"
                _LOGGER.warning("Ambient sensor read failed: %s", exc)
                return self.snapshot()
            except Exception as exc:  # noqa: BLE001 - dependency backends vary.
                self._last_refresh_monotonic_seconds = now
                self._status = "unavailable"
                self._reason = f"Ambient sensor read failed: {type(exc).__name__}: {exc}"
                _LOGGER.warning("Ambient sensor read failed: %s", exc)
                return self.snapshot()

            self._last_refresh_monotonic_seconds = now
            self._last_reading = reading
            self._status = "ok"
            self._reason = None
            return self.snapshot()

    def stop_for_session(self, session_id: str, *, reason: str) -> AmbientRuntimeSnapshot:
        """Stop ambient sensing if it belongs to the supplied session."""
        with self._lock:
            if self._active_session_id != session_id:
                return self.snapshot()
            self._stop_locked(reason=reason)
            return self.snapshot()

    def shutdown(self) -> AmbientRuntimeSnapshot:
        """Stop any active ambient runtime for process shutdown."""
        with self._lock:
            self._stop_locked(reason="process shutdown")
            return self.snapshot()

    def snapshot(self) -> AmbientRuntimeSnapshot:
        """Return an MCP-visible runtime snapshot."""
        with self._lock:
            reading = self._last_reading
            return AmbientRuntimeSnapshot(
                status=self._status,
                active_session_id=self._active_session_id,
                reason=self._reason,
                ambient_running=self._reader is not None,
                temperature_c=reading.temperature_c if reading is not None else None,
                humidity_percent=reading.humidity_percent if reading is not None else None,
                pressure_hpa=reading.pressure_hpa if reading is not None else None,
                last_reading_monotonic_seconds=(
                    reading.monotonic_seconds if reading is not None else None
                ),
            )

    def _stop_locked(self, *, reason: str) -> None:
        reader = self._reader
        if reader is not None:
            close = getattr(reader, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - shutdown should be best effort.
                    _LOGGER.warning("Ambient sensor close failed: %s", exc)
        self._reader = None
        if self._status == "ok":
            self._reason = f"Ambient sensing stopped: {reason}."


def build_ambient_session_runtime(
    config: AppConfig,
    *,
    reader_factory: AmbientReaderFactory | None = None,
) -> AmbientSessionRuntime:
    """Build the configured session-owned ambient runtime."""
    return AmbientSessionRuntime(config=config, reader_factory=reader_factory)


def _initial_status(config: AmbientConfig) -> AmbientRuntimeState:
    if config.mode == "disabled":
        return "disabled"
    return "unavailable"


def _initial_reason(config: AmbientConfig) -> str | None:
    if config.mode == "disabled":
        return "Ambient sensing is disabled by configuration."
    return "Ambient sensing has not started."
