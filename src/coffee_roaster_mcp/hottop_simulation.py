"""Trusted in-process adapters for the Hottop software simulation.

These adapters are deliberately not configuration providers. They can only be
selected by the server's in-process simulation composition, so no CLI, MCP
request, or environment value can turn a deployed server into this mode.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from coffee_roaster_mcp.ambient import AmbientReaderError, AmbientReading
from coffee_roaster_mcp.config import AmbientConfig
from coffee_roaster_mcp.drivers import SerialTransport

SYNTHETIC_HOTTOP_PORT = "in-memory://hottop-kn8828b-2k-plus"


class InMemoryHottopByteTransport:
    """Repository-owned byte transport with no native serial opening path."""

    def __init__(self) -> None:
        """Initialize a closed-world in-memory transport."""
        self._is_open = True
        self._read_buffer = bytearray()
        self._writes: list[bytes] = []
        self._lock = Lock()

    @property
    def is_open(self) -> bool:
        """Return whether the synthetic transport remains open."""
        with self._lock:
            return self._is_open

    @property
    def in_waiting(self) -> int:
        """Return the exact number of queued synthetic status bytes."""
        with self._lock:
            return len(self._read_buffer)

    def close(self) -> None:
        """Close this in-memory handle without touching an operating-system port."""
        with self._lock:
            self._is_open = False

    def write(self, data: bytes) -> int:
        """Record one driver command frame and return its complete byte count."""
        with self._lock:
            if not self._is_open:
                raise OSError("Synthetic Hottop transport is closed.")
            self._writes.append(bytes(data))
        return len(data)

    def read(self, size: int) -> bytes:
        """Read at most ``size`` queued synthetic status bytes."""
        if size < 0:
            raise ValueError("size must be non-negative.")
        with self._lock:
            chunk = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            return chunk

    def queue_status_bytes(self, data: bytes) -> None:
        """Queue synthetic driver status bytes for the candidate receiver."""
        with self._lock:
            if not self._is_open:
                raise OSError("Synthetic Hottop transport is closed.")
            self._read_buffer.extend(data)

    def writes_snapshot(self) -> tuple[bytes, ...]:
        """Return an immutable snapshot of command frames written by the driver."""
        with self._lock:
            return tuple(self._writes)


@dataclass(frozen=True)
class SyntheticAmbientSample:
    """One scripted room-metadata value for software-only simulation.

    This is synthetic input, not a claim that a physical room was observed.
    """

    temperature_c: float
    humidity_percent: float
    pressure_hpa: float


class _InMemoryAmbientReader:
    """Read scripted synthetic ambient values through the normal reader port."""

    def __init__(
        self,
        *,
        samples: deque[SyntheticAmbientSample],
        clock: Callable[[], float],
    ) -> None:
        """Bind scripted samples and the composition-owned clock."""
        self._samples = samples
        self._clock = clock
        self._closed = False

    def read(self) -> AmbientReading:
        """Return the next scripted sample with a composition-clock timestamp."""
        if self._closed:
            raise AmbientReaderError("Synthetic ambient reader is closed.")
        if not self._samples:
            raise AmbientReaderError("No synthetic ambient sample is available.")
        sample = self._samples.popleft()
        return AmbientReading(
            temperature_c=sample.temperature_c,
            humidity_percent=sample.humidity_percent,
            pressure_hpa=sample.pressure_hpa,
            monotonic_seconds=self._clock(),
        )

    def close(self) -> None:
        """Close the synthetic reader without opening or closing USB resources."""
        self._closed = True


class HottopSimulationComposition:
    """Closed trusted composition for the software-only candidate Hottop path."""

    synthetic_port: Literal["in-memory://hottop-kn8828b-2k-plus"] = SYNTHETIC_HOTTOP_PORT

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        receipt_age_bound_seconds: float,
        ambient_samples: tuple[SyntheticAmbientSample, ...] = (),
    ) -> None:
        """Create one non-reusable simulation composition.

        Args:
            clock: Trusted deterministic clock shared with the candidate driver
                and synthetic ambient timestamps.
            receipt_age_bound_seconds: Positive age bound passed unchanged to
                the candidate driver.
            ambient_samples: Optional scripted values exposed only through the
                ambient runtime's normal reader/polling ownership path.
        """
        if receipt_age_bound_seconds <= 0:
            raise ValueError("receipt_age_bound_seconds must be greater than 0.")
        self.clock = clock
        self.receipt_age_bound_seconds = receipt_age_bound_seconds
        self._transport = InMemoryHottopByteTransport()
        self._ambient_samples = deque(ambient_samples)
        self._serial_factory_calls = 0

    @property
    def transport(self) -> InMemoryHottopByteTransport:
        """Expose synthetic transport state for scripted-byte test input only."""
        return self._transport

    @property
    def serial_factory_calls(self) -> int:
        """Return repository-owned synthetic handle acquisitions."""
        return self._serial_factory_calls

    def queue_status_bytes(self, data: bytes) -> None:
        """Feed scripted synthetic status bytes without exposing a serial factory."""
        self._transport.queue_status_bytes(data)

    def queue_ambient_sample(self, sample: SyntheticAmbientSample) -> None:
        """Feed one scripted room sample without exposing an I/O reader."""
        self._ambient_samples.append(sample)

    def acquire_serial_transport(self, port: str) -> SerialTransport:
        """Acquire the sole repository-owned synthetic transport."""
        if port != SYNTHETIC_HOTTOP_PORT:
            raise ValueError("Simulation composition rejected an unexpected Hottop port.")
        if self._serial_factory_calls != 0:
            raise RuntimeError("Simulation composition permits only one Hottop handle.")
        self._serial_factory_calls += 1
        return self._transport

    def build_ambient_reader(self, config: AmbientConfig) -> _InMemoryAmbientReader:
        """Build the repository-owned synthetic ambient reader."""
        if config.mode != "yoctopuce" or config.device is not None:
            raise AmbientReaderError("Simulation composition rejected ambient device selection.")
        return _InMemoryAmbientReader(samples=self._ambient_samples, clock=self.clock)


def build_trusted_simulation_serial_factory(
    composition: HottopSimulationComposition,
) -> Callable[..., SerialTransport]:
    """Bind the repository-owned in-memory serial adapter at assembly."""
    if type(composition) is not HottopSimulationComposition:
        raise ValueError("Trusted Hottop simulation requires its repository-owned composition.")
    acquire_transport = HottopSimulationComposition.acquire_serial_transport
    if not callable(acquire_transport):
        raise RuntimeError("Trusted Hottop simulation serial implementation is unavailable.")

    def serial_factory(port: str, **_kwargs: object) -> SerialTransport:
        """Return the sole repository-owned synthetic transport for this composition."""
        transport = acquire_transport(composition, port)
        if transport is not composition.transport:
            raise RuntimeError("Simulation composition returned an unexpected Hottop transport.")
        return transport

    return serial_factory


def build_trusted_simulation_ambient_reader_factory(
    composition: HottopSimulationComposition,
) -> Callable[[AmbientConfig], _InMemoryAmbientReader]:
    """Bind the repository-owned in-memory ambient adapter at assembly."""
    if type(composition) is not HottopSimulationComposition:
        raise ValueError("Trusted Hottop simulation requires its repository-owned composition.")
    build_reader = HottopSimulationComposition.build_ambient_reader
    if not callable(build_reader):
        raise RuntimeError("Trusted Hottop simulation ambient implementation is unavailable.")

    def ambient_reader_factory(config: AmbientConfig) -> _InMemoryAmbientReader:
        """Build only the repository-owned synthetic ambient reader."""
        return build_reader(composition, config)

    return ambient_reader_factory
