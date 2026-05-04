"""FastMCP stdio server entrypoint for RoastPilot."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.config import AppConfig, load_config
from coffee_roaster_mcp.session import RoastEvent, RoastPhase, RoastSession, RoastSessionStore


@dataclass(frozen=True)
class ServerContext:
    """Server bootstrap context.

    Attributes:
        config: Loaded RoastPilot configuration.
        transport: Actual MCP transport used by this server process.
        session_store: Authoritative in-process roast session owner.
        started_at_utc: UTC time when the MCP process initialized.
    """

    config: AppConfig
    transport: str
    session_store: RoastSessionStore
    started_at_utc: datetime


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
        available_bootstrap_tools: Minimal tool surface exposed in `E2-S1`.
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


@dataclass(frozen=True)
class EventSnapshot:
    """Serializable roast event snapshot for MCP tool responses."""

    kind: str
    recorded_at_utc: str
    monotonic_seconds: float


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
    """Stub export manifest for the current pre-log-writer runtime."""

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
    return ServerContext(
        config=config,
        transport=transport,
        session_store=RoastSessionStore(default_log_dir=config.logging.log_dir / "roasts"),
        started_at_utc=datetime.now(UTC),
    )


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
        yield build_server_context(config_path=config_path, transport=transport)

    mcp = FastMCP(
        name="RoastPilot",
        instructions=(
            "RoastPilot MCP server with a mock-safe session runtime. "
            "This tool surface supports one in-process roast session and "
            "basic mock-path controls before hardware drivers and log writers land."
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
        )

    @mcp.tool()
    def start_roast_session(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> StartRoastSessionResult:
        """Start one new authoritative roast session."""
        server_context = ctx.request_context.lifespan_context
        session = server_context.session_store.start_session()
        return StartRoastSessionResult(session=_serialize_session_state(session, server_context))

    @mcp.tool()
    def get_roast_state(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> RoastSessionState:
        """Return the current authoritative roast session state."""
        server_context = ctx.request_context.lifespan_context
        session = _resolve_session(server_context, session_id=session_id)
        return _serialize_session_state(session, server_context)

    @mcp.tool()
    def set_heat(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        heat_level_percent: int,
    ) -> ControlCommandResult:
        """Set in-memory heat for the active mock session."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        server_context.session_store.set_heat(session, heat_level_percent=heat_level_percent)
        return _serialize_control_result(session)

    @mcp.tool()
    def set_fan(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        fan_level_percent: int,
    ) -> ControlCommandResult:
        """Set in-memory fan for the active mock session."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        server_context.session_store.set_fan(session, fan_level_percent=fan_level_percent)
        return _serialize_control_result(session)

    @mcp.tool()
    def mark_beans_added(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative beans-added event."""
        return _record_session_event(ctx, "beans_added")

    @mcp.tool()
    def mark_first_crack(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record the authoritative first-crack event."""
        server_context = ctx.request_context.lifespan_context
        if not server_context.config.first_crack.allow_manual_override:
            raise ValueError("Manual first-crack override is disabled by configuration.")
        return _record_session_event(ctx, "first_crack_detected")

    @mcp.tool()
    def drop_beans(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Record bean drop and force heat off in the mock session state."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        server_context.session_store.set_heat(session, heat_level_percent=0)
        event = server_context.session_store.record_event(session, "beans_dropped")
        return _serialize_event_result(session, event)

    @mcp.tool()
    def start_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Start cooling in the mock session state and record the event."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        event = server_context.session_store.start_cooling(session)
        return _serialize_event_result(session, event)

    @mcp.tool()
    def stop_cooling(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
    ) -> EventCommandResult:
        """Stop cooling in the mock session state and record the event."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        event = server_context.session_store.stop_cooling(session)
        return _serialize_event_result(session, event)

    @mcp.tool()
    def export_roast_log(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        session_id: str | None = None,
    ) -> ExportRoastLogResult:
        """Return the planned export targets for one roast session.

        Log writing itself lands in Epic 5. This tool only exposes the expected
        output paths so the MCP surface can stabilize before the writers exist.
        """
        server_context = ctx.request_context.lifespan_context
        session = _resolve_session(server_context, session_id=session_id)
        log_dir = _require_log_dir(session)
        log_dir.mkdir(parents=True, exist_ok=True)
        return ExportRoastLogResult(
            session_id=session.id,
            log_dir=str(log_dir),
            jsonl_path=str(log_dir / "roast.jsonl"),
            csv_path=str(log_dir / "roast.csv"),
            summary_path=str(log_dir / "summary.json"),
            ready=False,
            note=(
                "Export writers land in Epic 5. "
                "This tool currently returns the planned manifest only."
            ),
        )

    @mcp.tool()
    def emergency_stop(  # pyright: ignore[reportUntypedFunctionDecorator, reportUnusedFunction]
        ctx: Context[ServerSession, ServerContext],
        reason: str = "manual emergency stop",
    ) -> EventCommandResult:
        """Apply mock-safe emergency-stop state and record a fault event."""
        server_context = ctx.request_context.lifespan_context
        session = _require_active_session(server_context)
        server_context.session_store.set_heat(session, heat_level_percent=0)
        server_context.session_store.set_fan(session, fan_level_percent=100)
        session.cooling_on = True
        event = server_context.session_store.record_event(
            session,
            "fault",
            payload={"reason": reason},
        )
        return _serialize_event_result(session, event)

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
    session = server_context.session_store.get_latest_session()
    if session is None:
        raise ValueError("No roast session exists.")
    if session_id is not None and session.id != session_id:
        raise ValueError(f"Unknown session_id: {session_id}")
    return session


def _record_session_event(
    ctx: Context[ServerSession, ServerContext],
    kind: Literal["beans_added", "first_crack_detected"],
) -> EventCommandResult:
    """Record one core event against the active session."""
    server_context = ctx.request_context.lifespan_context
    session = _require_active_session(server_context)
    event = server_context.session_store.record_event(session, kind)
    return _serialize_event_result(session, event)


def _serialize_session_state(
    session: RoastSession,
    server_context: ServerContext,
) -> RoastSessionState:
    """Convert one in-memory session into an MCP-safe snapshot."""
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
        events=tuple(_serialize_event(event) for event in session.event_timeline),
        log_dir=str(session.log_writer.log_dir) if session.log_writer is not None else None,
    )


def _serialize_control_result(session: RoastSession) -> ControlCommandResult:
    """Convert one control mutation into a stable tool response."""
    return ControlCommandResult(
        session_id=session.id,
        phase=session.phase,
        heat_level_percent=session.heat_level_percent,
        fan_level_percent=session.fan_level_percent,
        cooling_on=session.cooling_on,
    )


def _serialize_event_result(session: RoastSession, event: RoastEvent) -> EventCommandResult:
    """Convert one event mutation into a stable tool response."""
    return EventCommandResult(
        session_id=session.id,
        phase=session.phase,
        event=_serialize_event(event),
        event_count=len(session.event_timeline),
    )


def _serialize_event(event: RoastEvent) -> EventSnapshot:
    """Convert one roast event into a serializable snapshot."""
    return EventSnapshot(
        kind=event.kind,
        recorded_at_utc=event.recorded_at_utc.isoformat(),
        monotonic_seconds=event.monotonic_seconds,
    )


def _iso_or_none(value: datetime | None) -> str | None:
    """Return one ISO8601 string when a datetime exists."""
    return value.isoformat() if value is not None else None


def _require_log_dir(session: RoastSession) -> Path:
    """Return the session log directory when configured."""
    if session.log_writer is None:
        raise ValueError("Session log target is unavailable.")
    return session.log_writer.log_dir
