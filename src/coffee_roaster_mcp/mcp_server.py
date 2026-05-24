"""FastMCP stdio server entrypoint for RoastPilot."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.config import AppConfig, ConfigError, FirstCrackMode, load_config
from coffee_roaster_mcp.drivers import RoasterDriver, RoasterState, create_roaster_driver
from coffee_roaster_mcp.exports import export_roast_snapshot
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackSessionRuntime,
    build_first_crack_session_runtime,
)
from coffee_roaster_mcp.session import (
    DriverCommandReservation,
    EventPayloadValue,
    RoastEvent,
    RoastPhase,
    RoastSession,
    RoastSessionStore,
    SessionLifecycleError,
    compute_roast_metrics,
    default_emergency_safety_payload,
)


@dataclass(frozen=True)
class ServerContext:
    """Server bootstrap context.

    Attributes:
        config: Loaded RoastPilot configuration.
        transport: Actual MCP transport used by this server process.
        session_store: Authoritative in-process roast session owner.
        roaster_driver: Configured driver boundary.
        first_crack_runtime: Session-owned first-crack detector runtime.
        telemetry_sampler: Session-owned autonomous telemetry sampler.
        started_at_utc: UTC time when the MCP process initialized.
    """

    config: AppConfig
    transport: str
    session_store: RoastSessionStore
    roaster_driver: RoasterDriver
    first_crack_runtime: FirstCrackSessionRuntime
    telemetry_sampler: _TelemetrySampler
    started_at_utc: datetime


TelemetrySampleCallback = Callable[[str], bool]


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
        self._active_session_id: str | None = None
        self._last_error: str | None = None

    def start_for_session(self, session_id: str) -> None:
        """Start autonomous sampling for one session id."""
        thread: Thread | None
        with self._lock:
            thread = self._stop_locked()
        self._join_thread(thread)

        with self._lock:
            self._stop_event = Event()
            self._active_session_id = session_id
            self._last_error = None
            self._thread = Thread(
                target=self._run,
                args=(session_id, self._stop_event),
                name=f"roast-telemetry-sampler-{session_id}",
                daemon=True,
            )
            self._thread.start()

    def stop_for_session(self, session_id: str, *, reason: str) -> None:
        """Stop sampling if the supplied session currently owns the sampler."""
        _ = reason
        thread: Thread | None
        with self._lock:
            if self._active_session_id != session_id:
                return
            thread = self._stop_locked()
        self._join_thread(thread)

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

    def _run(self, session_id: str, stop_event: Event) -> None:
        while not stop_event.wait(self._interval_seconds):
            try:
                keep_sampling = self._sample_callback(session_id)
            except Exception as exc:  # noqa: BLE001 - background worker must not escape.
                with self._lock:
                    if self._active_session_id == session_id:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                keep_sampling = False
            if not keep_sampling:
                with self._lock:
                    if self._active_session_id == session_id:
                        self._active_session_id = None
                return

    def _stop_locked(self) -> Thread | None:
        thread = self._thread
        self._active_session_id = None
        self._thread = None
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
    """

    mode: FirstCrackMode
    status: FirstCrackRuntimeStatus
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    allow_manual_override: bool
    reason: str | None = None


T0RuntimeStatus = Literal["disabled", "pending", "detected", "unavailable"]


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
    events: tuple[EventSnapshot, ...]
    log_dir: str | None


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
class ExportRoastLogResult:
    """Result for one snapshot roast-log export."""

    session_id: str
    log_dir: str
    jsonl_path: str
    csv_path: str
    summary_path: str
    ready: bool
    note: str


def build_server_context(
    *,
    config_path: str | Path | None = None,
    transport: str = "stdio",
) -> ServerContext:
    """Build the MCP server lifecycle context from config and runtime transport.

    Args:
        config_path: Optional explicit config path.
        transport: Actual MCP transport used by this server instance.

    Returns:
        The initialized server context used by the MCP lifespan.
    """
    config = load_config(path=config_path)
    try:
        roaster_driver = create_roaster_driver(
            config.roaster.driver,
            port=config.roaster.port,
            baudrate=config.roaster.baudrate,
            temperature_unit=config.roaster.temperature_unit,
            command_interval_seconds=config.roaster.command_interval_seconds,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    server_context: ServerContext | None = None

    def sample_active_session(session_id: str) -> bool:
        if server_context is None:
            return False
        return _sample_active_session_telemetry(server_context, session_id=session_id)

    telemetry_sampler = _TelemetrySampler(
        interval_seconds=config.logging.sample_interval_seconds,
        sample_callback=sample_active_session,
    )
    server_context = ServerContext(
        config=config,
        transport=transport,
        session_store=RoastSessionStore(
            default_log_dir=config.logging.log_dir / "roasts",
            telemetry_log_interval_seconds=config.logging.sample_interval_seconds,
        ),
        roaster_driver=roaster_driver,
        first_crack_runtime=build_first_crack_session_runtime(config),
        telemetry_sampler=telemetry_sampler,
        started_at_utc=datetime.now(UTC),
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

    @asynccontextmanager
    async def server_lifespan(_: FastMCP) -> AsyncGenerator[ServerContext, None]:
        """Load typed config once for the MCP process lifetime."""
        server_context = build_server_context(config_path=config_path, transport=transport)
        try:
            yield server_context
        finally:
            server_context.telemetry_sampler.shutdown()
            server_context.first_crack_runtime.shutdown()

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
                "start_cooling",
                "stop_cooling",
                "export_roast_log",
                "emergency_stop",
            ),
            started_at_utc=server_context.started_at_utc.isoformat(),
        )

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
    ) -> StartRoastSessionResult:
        """Start one new authoritative roast session and prepare the driver."""
        server_context = ctx.request_context.lifespan_context
        reservation = server_context.session_store.reserve_session_start()
        try:
            server_context.roaster_driver.connect()
            session = server_context.session_store.complete_session_start_snapshot(reservation)
        except Exception:
            server_context.session_store.clear_session_start_reservation(reservation)
            raise
        _start_first_crack_runtime(server_context, session=session)
        server_context.telemetry_sampler.start_for_session(session.id)
        return StartRoastSessionResult(
            session=_serialize_session_state(
                session,
                config=server_context.config,
                first_crack_runtime=server_context.first_crack_runtime.snapshot(),
            )
        )

    @mcp.tool()
    def get_roast_state(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> RoastSessionState:
        """Return the current authoritative roast session state."""
        server_context = ctx.request_context.lifespan_context
        session = _resolve_session(server_context, session_id=session_id)
        driver_state = _read_current_driver_state(server_context)
        device_state = _serialize_device_state(driver_state)
        session = _record_polling_telemetry_for_active_session(
            server_context,
            session=session,
            requested_session_id=session_id,
            driver_state=driver_state,
        )
        auto_t0_recorded = _process_auto_t0_for_active_session(
            server_context,
            session_id=session_id,
            device_state=device_state,
        )
        if auto_t0_recorded:
            server_context.first_crack_runtime.discard_queued_windows_for_session(
                session.id,
                reason="Dropped queued pre-T0 detector windows after automatic T0.",
            )
        _process_first_crack_runtime_for_active_session(server_context, session_id=session_id)
        session = _resolve_session(server_context, session_id=session_id)
        return _serialize_session_state(
            session,
            config=server_context.config,
            device_state=device_state,
            first_crack_runtime=server_context.first_crack_runtime.snapshot(),
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
    def mark_beans_added(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative beans-added event."""
        result = _record_session_event(ctx, "beans_added")
        _process_first_crack_runtime_for_active_session(
            ctx.request_context.lifespan_context,
            session_id=result.session_id,
        )
        snapshot = _snapshot_session(
            ctx.request_context.lifespan_context,
            session_id=result.session_id,
        )
        return EventCommandResult(
            session_id=snapshot.id,
            phase=snapshot.phase,
            event=result.event,
            event_count=len(snapshot.event_timeline),
        )

    @mcp.tool()
    def mark_first_crack(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative first-crack event."""
        server_context = ctx.request_context.lifespan_context
        if not server_context.config.first_crack.allow_manual_override:
            raise ValueError("Manual first-crack override is disabled by configuration.")
        result = _record_session_event(ctx, "first_crack_detected")
        server_context.first_crack_runtime.stop_for_session(
            result.session_id,
            reason="manual first-crack override",
        )
        return result

    @mcp.tool()
    def drop_beans(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Drop beans through the driver and record the cooling transition."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        event, snapshot = _run_reserved_driver_drop(server_context, session)
        server_context.first_crack_runtime.stop_for_session(
            snapshot.id,
            reason="beans dropped",
        )
        if not snapshot.active:
            server_context.telemetry_sampler.stop_for_session(snapshot.id, reason="beans dropped")
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    def start_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Start cooling through the driver as an explicit recovery command."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        event, snapshot = _run_reserved_driver_start_cooling(server_context, session)
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    def stop_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Stop cooling through the configured driver boundary."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        event, snapshot = _run_reserved_driver_stop_cooling(server_context, session)
        server_context.first_crack_runtime.stop_for_session(
            snapshot.id,
            reason="cooling stopped",
        )
        server_context.telemetry_sampler.stop_for_session(
            snapshot.id,
            reason="cooling stopped",
        )
        return _serialize_event_result(snapshot=snapshot, event=event)

    @mcp.tool()
    def export_roast_log(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> ExportRoastLogResult:
        """Write a snapshot export for one roast session."""
        server_context = ctx.request_context.lifespan_context
        session = _resolve_session(server_context, session_id=session_id)
        export = export_roast_snapshot(
            session,
            roaster_driver=server_context.config.roaster.driver,
            ror_window_seconds=server_context.config.session.ror_window_seconds,
            ror_min_sample_seconds=server_context.config.session.ror_min_sample_seconds,
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
    def emergency_stop(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        reason: str = "manual emergency stop",
    ) -> EventCommandResult:
        """Call the configured driver safety method and record a fault event."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        server_context.session_store.cancel_pending_driver_command(session)
        safety_payload = run_driver_emergency_stop(server_context, reason=reason)
        event, snapshot = server_context.session_store.emergency_stop_snapshot(
            session,
            reason=reason,
            safety_payload=safety_payload,
            allow_stopped_latest=True,
        )
        server_context.first_crack_runtime.stop_for_session(
            snapshot.id,
            reason="emergency stop",
        )
        server_context.telemetry_sampler.stop_for_session(
            snapshot.id,
            reason="emergency stop",
        )
        return _serialize_event_result(snapshot=snapshot, event=event)

    return mcp


def run_stdio_server(config_path: str | Path | None = None) -> None:
    """Run RoastPilot over stdio transport.

    Args:
        config_path: Optional explicit config path.
    """
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
    event, snapshot = server_context.session_store.record_event_snapshot(session, kind)
    return _serialize_event_result(snapshot=snapshot, event=event)


def _start_first_crack_runtime(
    server_context: ServerContext,
    *,
    session: RoastSession,
) -> None:
    """Start first-crack runtime preparation for a newly created session."""
    server_context.first_crack_runtime.start_for_session(session)


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
    server_context.first_crack_runtime.process_available_windows(
        session_store=server_context.session_store,
        session=active_session,
    )


def _process_auto_t0_for_active_session(
    server_context: ServerContext,
    *,
    session_id: str | None,
    device_state: RoasterDeviceState,
) -> bool:
    """Process automatic T0 after a successful configured-driver state read."""
    if not server_context.config.session.auto_t0_detection_enabled:
        return False
    active_session = server_context.session_store.get_active_session()
    if active_session is None:
        return False
    if session_id is not None and session_id != active_session.id:
        return False
    if active_session.phase != "pre_roast" or active_session.beans_added_at_utc is not None:
        return False
    if not device_state.connected or device_state.bean_temp_c is None:
        return False
    event, _ = server_context.session_store.process_auto_t0_reading_snapshot(
        active_session,
        bean_temp_c=device_state.bean_temp_c,
        drop_threshold_c=server_context.config.session.auto_t0_drop_threshold_c,
    )
    return event is not None


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
    try:
        driver_state = server_context.roaster_driver.drop_beans()
        try:
            return server_context.session_store.complete_reserved_driver_drop_snapshot(
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

        auto_t0_recorded = _process_auto_t0_for_active_session(
            server_context,
            session_id=session_id,
            device_state=device_state,
        )
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
    safety_payload = run_driver_emergency_stop(server_context, reason=reason)
    try:
        _, snapshot = server_context.session_store.emergency_stop_snapshot(
            session,
            reason=reason,
            safety_payload=safety_payload,
            allow_stopped_latest=True,
        )
    except SessionLifecycleError:
        return
    server_context.first_crack_runtime.stop_for_session(
        snapshot.id,
        reason="autonomous telemetry sampler failure",
    )


def _serialize_session_state(
    session: RoastSession,
    *,
    config: AppConfig,
    device_state: RoasterDeviceState | None = None,
    first_crack_runtime: FirstCrackRuntimeSnapshot | None = None,
) -> RoastSessionState:
    """Convert one in-memory session into an MCP-safe snapshot."""
    metrics = compute_roast_metrics(
        session,
        ror_window_seconds=config.session.ror_window_seconds,
        ror_min_sample_seconds=config.session.ror_min_sample_seconds,
    )
    return RoastSessionState(
        session_id=session.id,
        active=session.active,
        phase=session.phase,
        created_at_utc=session.created_at_utc.isoformat(),
        stopped_at_utc=_iso_or_none(session.stopped_at_utc),
        elapsed_monotonic_seconds=round(session.elapsed_monotonic_seconds(time.monotonic), 3),
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
        events=tuple(_serialize_event(event) for event in session.event_timeline),
        log_dir=str(session.log_writer.log_dir.resolve())
        if session.log_writer is not None
        else None,
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
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="detected",
            detected_at_utc=session.first_crack_at_utc.isoformat(),
            detected_monotonic_seconds=session.first_crack_monotonic_seconds,
            allow_manual_override=config.first_crack.allow_manual_override,
        )
    if session.faulted_at_utc is not None:
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status="faulted",
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason="Session faulted before first crack was recorded.",
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
        return FirstCrackStatus(
            mode=config.first_crack.mode,
            status=first_crack_runtime.status,
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=config.first_crack.allow_manual_override,
            reason=first_crack_runtime.reason,
        )
    reason = "Audio first-crack detection has not recorded first crack for this session."
    if (
        config.first_crack.mode == "audio"
        and first_crack_runtime is not None
        and first_crack_runtime.active_session_id == session.id
        and first_crack_runtime.reason is not None
    ):
        reason = first_crack_runtime.reason
    return FirstCrackStatus(
        mode=config.first_crack.mode,
        status="pending",
        detected_at_utc=None,
        detected_monotonic_seconds=None,
        allow_manual_override=config.first_crack.allow_manual_override,
        reason=reason,
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
