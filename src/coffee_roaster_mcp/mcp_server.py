"""FastMCP stdio server entrypoint for RoastPilot.

This module implements the first local MCP server runtime for RoastPilot.
The `E2-S1` scope is intentionally narrow: start cleanly over stdio, load the
existing typed configuration, and expose a minimal bootstrap-safe tool list.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.config import AppConfig, load_config


@dataclass(frozen=True)
class ServerContext:
    """Server bootstrap context.

    Attributes:
        config: Loaded RoastPilot configuration.
        started_at_utc: UTC time when the MCP process initialized.
    """

    config: AppConfig
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


def create_mcp_server(config_path: str | Path | None = None) -> FastMCP:
    """Create the RoastPilot FastMCP server.

    Args:
        config_path: Optional explicit config path. When omitted, the standard
            config loading rules apply.

    Returns:
        A configured FastMCP server instance.
    """

    @asynccontextmanager
    async def server_lifespan(_: FastMCP) -> AsyncGenerator[ServerContext, None]:
        """Load typed config once for the MCP process lifetime."""
        yield ServerContext(
            config=load_config(path=config_path),
            started_at_utc=datetime.now(UTC),
        )

    mcp = FastMCP(
        name="RoastPilot",
        instructions=(
            "Bootstrap-safe RoastPilot MCP server. "
            "This E2-S1 tool surface exposes runtime introspection only."
        ),
        lifespan=server_lifespan,
    )

    @mcp.tool()
    def get_server_info(ctx: Context[ServerSession, ServerContext]) -> ServerInfo:
        """Return structured metadata about the running MCP server."""
        server_context = ctx.request_context.lifespan_context
        config = server_context.config
        return ServerInfo(
            product_name="RoastPilot",
            package_name="coffee-roaster-mcp",
            version=__version__,
            transport=config.transport.type,
            current_phase="bootstrap",
            roaster_driver=config.roaster.driver,
            first_crack_mode=config.first_crack.mode,
            bootstrap_safe=_is_bootstrap_safe(config),
            available_bootstrap_tools=("get_server_info", "get_runtime_config"),
            started_at_utc=server_context.started_at_utc.isoformat(),
        )

    @mcp.tool()
    def get_runtime_config(
        ctx: Context[ServerSession, ServerContext],
    ) -> RuntimeConfigSnapshot:
        """Return a bootstrap-safe summary of the active RoastPilot config."""
        config = ctx.request_context.lifespan_context.config
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

    return mcp


def run_stdio_server(config_path: str | Path | None = None) -> None:
    """Run RoastPilot over stdio transport.

    Args:
        config_path: Optional explicit config path.
    """
    create_mcp_server(config_path=config_path).run(transport="stdio")


def _is_bootstrap_safe(config: AppConfig) -> bool:
    """Return whether the active config matches bootstrap-safe defaults.

    Args:
        config: Loaded RoastPilot configuration.

    Returns:
        True when the config stays on the mock driver and disabled detector mode.
    """
    return config.roaster.driver == "mock" and config.first_crack.mode == "disabled"
