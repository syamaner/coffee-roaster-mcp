"""Trusted in-process Hottop simulation composition coverage."""

from __future__ import annotations

import asyncio
import csv
import inspect
import json
import os
import struct
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.message import SessionMessage
from mcp.types import TextContent
from pydantic import ValidationError

import coffee_roaster_mcp.ambient_runtime as ambient_runtime_module
import coffee_roaster_mcp.artifacts as artifacts_module
import coffee_roaster_mcp.audio as audio_module
import coffee_roaster_mcp.mcp_server as mcp_server
import coffee_roaster_mcp.session as session_module
from coffee_roaster_mcp.config import AmbientConfig, AppConfig, ConfigError, FirstCrackConfig
from coffee_roaster_mcp.detector import FirstCrackDetectorAdapter
from coffee_roaster_mcp.drivers import (
    HOTTOP_DRIVER_NAME,
    HOTTOP_PACKET_LENGTH,
    HOTTOP_PACKET_PREFIX,
    HottopRoasterDriver,
    build_hottop_command_packet,
    calculate_hottop_packet_checksum,
)
from coffee_roaster_mcp.first_crack_observer import ObservationModel
from coffee_roaster_mcp.first_crack_runtime import FirstCrackSessionRuntime
from coffee_roaster_mcp.hottop_simulation import (
    SYNTHETIC_HOTTOP_PORT,
    HottopSimulationComposition,
    SyntheticAmbientSample,
    build_trusted_simulation_ambient_reader_factory,
    build_trusted_simulation_serial_factory,
)
from coffee_roaster_mcp.mcp_server import (
    CaptureEvidenceBootstrapResult,
    EventCommandResult,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerContext,
    StartRoastSessionResult,
    build_server_context,
    create_hottop_simulation_mcp_server,
    create_mcp_server,
)


class _Clock:
    """Deterministic composition clock shared by synthetic adapters."""

    def __init__(self, value: float = 100.0) -> None:
        """Initialize the fixed test clock."""
        self.value = value

    def __call__(self) -> float:
        """Return the current synthetic monotonic time."""
        return self.value


def _write_synthetic_wav(path: Path) -> None:
    """Create a scratch-only 480-frame mono PCM capture input."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8)
        output.writeframes(struct.pack("<480h", *(1000,) * 480))


def _status_packet(*, bean_c: int, environment_c: int) -> bytes:
    """Build one tracked synthetic Celsius Hottop packet for the in-memory stream."""
    packet = bytearray(HOTTOP_PACKET_LENGTH)
    packet[: len(HOTTOP_PACKET_PREFIX)] = HOTTOP_PACKET_PREFIX
    packet[23:25] = environment_c.to_bytes(2, "big")
    packet[25:27] = bean_c.to_bytes(2, "big")
    packet[-1] = calculate_hottop_packet_checksum(packet)
    return bytes(packet)


def _simulation_config(
    tmp_path: Path,
    *,
    port: str = SYNTHETIC_HOTTOP_PORT,
    include_local_model_dir: bool = True,
) -> Path:
    """Write one complete trusted-simulation config in pytest scratch space."""
    source_wav = tmp_path / "synthetic-input.wav"
    _write_synthetic_wav(source_wav)
    model_dir = tmp_path / "synthetic-model"
    (model_dir / "onnx" / "int8").mkdir(parents=True)
    (model_dir / "onnx" / "int8" / "model_quantized.onnx").write_bytes(b"synthetic")
    (model_dir / "onnx" / "int8" / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    local_model_dir = f"  local_model_dir: {model_dir}\n" if include_local_model_dir else ""
    config = tmp_path / "simulation.yaml"
    config_text = (
        (
            "roaster:\n"
            "  driver: hottop_kn8828b_2k_plus\n"
            f"  port: {port}\n"
            "  temperature_unit: celsius\n"
            "  hottop_reception: candidate/v1\n"
            "  command_interval_seconds: 0.01\n"
            "first_crack:\n"
            "  mode: audio\n"
            "  event_authority: manual\n"
            "  allow_manual_override: true\n"
        )
        + local_model_dir
        + (
            "audio:\n"
            "  source: wav\n"
            f"  wav_path: {source_wav}\n"
            "  sample_rate: 8\n"
            "  window_seconds: 1\n"
            "recording:\n"
            "  enabled: true\n"
            "  autocapture: true\n"
            f"  export_location: {tmp_path / 'recordings'}\n"
            "ambient:\n"
            "  mode: yoctopuce\n"
            "  poll_interval_seconds: 1\n"
            "logging:\n"
            f"  log_dir: {tmp_path / 'logs'}\n"
        )
    )
    config.write_text(config_text, encoding="utf-8")
    return config


def _tool_context(context: ServerContext) -> Any:
    """Build the minimal registered-tool context for a real lifespan context."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=context))


async def _call(server: Any, name: str, context: ServerContext, **kwargs: object) -> Any:
    """Call one registered tool with an actual lifecycle context."""
    manager = server._tool_manager  # pyright: ignore[reportPrivateUsage]
    tool = manager.get_tool(name)
    assert tool is not None
    result = tool.fn(_tool_context(context), **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def test_candidate_without_trusted_composition_refuses_before_native_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidate config cannot reach a driver or native transport by itself."""
    config = _simulation_config(tmp_path)
    constructed: list[object] = []

    def forbidden_driver(*_args: object, **_kwargs: object) -> object:
        constructed.append(object())
        raise AssertionError("candidate attempted driver construction")

    monkeypatch.setattr(mcp_server, "create_roaster_driver", forbidden_driver)

    with pytest.raises(ValueError, match="requires trusted simulation composition"):
        build_server_context(config_path=config)

    assert constructed == []


def test_simulation_rejects_wrong_port_before_driver_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted composition rejects accidental physical port selection before setup."""
    config = _simulation_config(tmp_path, port="/dev/not-a-simulation")
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=2.0)
    factory_calls: list[object] = []

    def forbidden_driver(*_args: object, **_kwargs: object) -> object:
        factory_calls.append(object())
        raise AssertionError("invalid simulation configuration reached driver construction")

    monkeypatch.setattr(mcp_server, "create_roaster_driver", forbidden_driver)

    with pytest.raises(ValueError, match="exact candidate configuration"):
        build_server_context(config_path=config, simulation_composition=composition)

    assert factory_calls == []


def test_simulation_config_without_factory_binding_cannot_enable_manual_writer(
    tmp_path: Path,
) -> None:
    """Synthetic config values alone cannot grant the factory-bound writer authority."""
    config = _simulation_config(tmp_path)
    context = build_server_context(
        config_path=config,
        simulation_composition=HottopSimulationComposition(
            clock=_Clock(), receipt_age_bound_seconds=1.0
        ),
        environ={},
    )

    try:
        assert context.capture_evidence_configured
        assert not mcp_server._manual_first_crack_writer_composition(  # pyright: ignore[reportPrivateUsage]
            context
        )
        assert not context.session_store._manual_first_crack_writer_enabled  # pyright: ignore[reportPrivateUsage]
    finally:
        context.first_crack_runtime.shutdown()
        context.ambient_runtime.shutdown()
        context.session_store.shutdown_manual_first_crack_writer()


def test_simulation_requires_local_model_before_driver_or_downloader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulation rejects an omitted local model directory before any adapter can run."""
    config = _simulation_config(tmp_path, include_local_model_dir=False)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=2.0)
    driver_calls: list[object] = []
    downloader_calls: list[object] = []

    def forbidden_driver(*_args: object, **_kwargs: object) -> object:
        driver_calls.append(object())
        raise AssertionError("missing local model reached driver construction")

    def forbidden_downloader(**_kwargs: object) -> str:
        downloader_calls.append(object())
        raise AssertionError("simulation attempted external artifact resolution")

    monkeypatch.setattr(mcp_server, "create_roaster_driver", forbidden_driver)
    monkeypatch.setattr(artifacts_module, "_hf_hub_download", forbidden_downloader)

    with pytest.raises(ConfigError, match="explicit local first-crack model directory"):
        build_server_context(config_path=config, simulation_composition=composition, environ={})

    assert driver_calls == []
    assert downloader_calls == []
    assert composition.serial_factory_calls == 0


def test_simulation_start_schema_requires_explicit_roast_and_ack(tmp_path: Path) -> None:
    """Only the trusted composition gets a strict raw SDK Start schema."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=2.0)
    simulated = create_hottop_simulation_mcp_server(
        config_path=config,
        composition=composition,
    )
    simulated_tool = simulated._tool_manager.get_tool("start_roast_session")  # pyright: ignore[reportPrivateUsage]
    assert simulated_tool is not None
    model: Any = simulated_tool.fn_metadata.arg_model

    with pytest.raises(ValidationError):
        model.model_validate({})
    with pytest.raises(ValidationError):
        model.model_validate(
            {"purpose": "cold_characterisation", "first_crack_observer_ack": "manual/v1"}
        )
    assert (
        model.model_validate({"purpose": "roast", "first_crack_observer_ack": "manual/v1"}).purpose
        == "roast"
    )
    assert composition.serial_factory_calls == 0

    legacy_server = create_mcp_server()
    legacy_tool = legacy_server._tool_manager.get_tool("start_roast_session")  # pyright: ignore[reportPrivateUsage]
    assert legacy_tool is not None
    legacy_model: Any = legacy_tool.fn_metadata.arg_model
    assert legacy_model.model_validate({}).purpose == "roast"
    simulated_tools = {
        tool.name
        for tool in simulated._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
    }
    legacy_tools = {
        tool.name
        for tool in legacy_server._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
    }
    assert simulated_tools == legacy_tools | {"get_hottop_reception_evidence"}


def test_simulation_reception_tool_uses_the_registered_closed_protocol_in_memory(
    tmp_path: Path,
) -> None:
    """An in-memory SDK client sees the closed tool and strict identity refusal wire row."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=2.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Drive the actual registered server tool across in-memory SDK streams."""
        client_send, server_receive = create_memory_object_stream[SessionMessage](0)
        server_send, client_receive = create_memory_object_stream[SessionMessage | Exception](0)
        low_level = server._mcp_server  # pyright: ignore[reportPrivateUsage]
        async with create_task_group() as group:
            group.start_soon(
                low_level.run,
                cast(MemoryObjectReceiveStream[SessionMessage | Exception], server_receive),
                server_send,
                low_level.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as client:
                await client.initialize()
                discovered = await client.list_tools()
                tool = next(
                    item
                    for item in discovered.tools
                    if item.name == "get_hottop_reception_evidence"
                )
                assert tool.inputSchema == {
                    "type": "object",
                    "properties": {
                        "expected_session_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                        "evidence_protocol": {"const": "hottop_reception_evidence/v1"},
                    },
                    "required": ["expected_session_id", "evidence_protocol"],
                    "additionalProperties": False,
                }
                assert tool.outputSchema is not None
                output_rows = cast(list[dict[str, object]], tool.outputSchema["oneOf"])
                outcomes = {
                    cast(dict[str, str], cast(dict[str, object], row["properties"])["outcome"])[
                        "const"
                    ]
                    for row in output_rows
                }
                assert outcomes == {
                    "evidence",
                    "refused_identity",
                    "refused_capability",
                    "refused_busy",
                    "unavailable",
                }
                refused = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": "winter-sim-1",
                        "evidence_protocol": "hottop_reception_evidence/v1",
                    },
                )
                assert refused.isError is False
                assert refused.structuredContent == {
                    "evidence_protocol": "hottop_reception_evidence/v1",
                    "evidence_version": 1,
                    "outcome": "refused_identity",
                    "reason_code": "unknown_session",
                    "driver_read": "not_invoked",
                    "requested_session_id": "winter-sim-1",
                    "record": None,
                }
                text_content = cast(TextContent, refused.content[0])
                assert json.loads(text_content.text) == refused.structuredContent
                malformed = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": "winter-sim-1",
                        "evidence_protocol": "hottop_reception_evidence/v1",
                        "unexpected": True,
                    },
                )
                assert malformed.isError is True
            await client_send.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("hold_schedule", ["before_storage", "after_storage"])
def test_simulation_exact_active_stop_enters_driver_before_held_human_mark_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold_schedule: str
) -> None:
    """The factory-bound exact owner reaches synthetic Stop before a held Mark settles."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)
    first_crack_persisting = Event()
    release_first_crack = Event()
    driver_entered = Event()
    driver_entries: list[HottopRoasterDriver] = []
    writer_timeouts: list[str] = []
    writes_before_driver_entry: list[int] = []
    runtime_cleanup_calls: list[str] = []
    canonical_event_frames: list[bytes] = []
    original_append = session_module._CanonicalGenerationWriter._append_verified  # pyright: ignore[reportPrivateUsage]
    original_emergency_stop = HottopRoasterDriver.emergency_stop
    original_freeze = FirstCrackSessionRuntime.freeze_observation_boundary
    original_stop_sampler = mcp_server._TelemetrySampler.stop_for_session  # pyright: ignore[reportPrivateUsage]
    original_stop_ambient = ambient_runtime_module.AmbientSessionRuntime.stop_for_session

    def hold_first_crack(
        writer: object,
        frame: bytes,
    ) -> None:
        """Hold the human first-crack turn on either side of real storage."""
        if b'"first_crack_detected"' in frame and hold_schedule == "before_storage":
            first_crack_persisting.set()
            if not release_first_crack.wait(timeout=1.0):
                writer_timeouts.append("first_crack")
        original_append(writer, frame)  # type: ignore[arg-type]
        if b'"first_crack_detected"' in frame or b'"fault"' in frame:
            canonical_event_frames.append(frame)
        if b'"first_crack_detected"' in frame and hold_schedule == "after_storage":
            first_crack_persisting.set()
            if not release_first_crack.wait(timeout=1.0):
                writer_timeouts.append("first_crack")

    def record_driver_entry(driver: HottopRoasterDriver, *, reason: str) -> object:
        """Observe candidate/v1 method entry separately from its transport frame."""
        driver_entries.append(driver)
        writes_before_driver_entry.append(len(composition.transport.writes_snapshot()))
        driver_entered.set()
        return original_emergency_stop(driver, reason=reason)

    def record_first_crack_cleanup(
        runtime: FirstCrackSessionRuntime,
        session: object,
        ticket: object | None,
        *,
        reason: str,
    ) -> None:
        """Count emergency cleanup without changing the registered handler path."""
        runtime_cleanup_calls.append("first_crack")
        original_freeze(runtime, cast(Any, session), ticket, reason=reason)

    def record_sampler_cleanup(sampler: object, session_id: str, *, reason: str) -> None:
        """Count telemetry cleanup without altering sampler ownership semantics."""
        runtime_cleanup_calls.append("telemetry")
        original_stop_sampler(sampler, session_id, reason=reason)  # type: ignore[arg-type]

    def record_ambient_cleanup(runtime: object, session_id: str, *, reason: str) -> object:
        """Count ambient cleanup without altering ambient ownership semantics."""
        runtime_cleanup_calls.append("ambient")
        return original_stop_ambient(runtime, session_id, reason=reason)  # type: ignore[arg-type]

    monkeypatch.setattr(
        session_module._CanonicalGenerationWriter,  # pyright: ignore[reportPrivateUsage]
        "_append_verified",
        hold_first_crack,
    )
    monkeypatch.setattr(HottopRoasterDriver, "emergency_stop", record_driver_entry)
    monkeypatch.setattr(
        FirstCrackSessionRuntime, "freeze_observation_boundary", record_first_crack_cleanup
    )
    monkeypatch.setattr(
        mcp_server._TelemetrySampler,  # pyright: ignore[reportPrivateUsage]
        "stop_for_session",
        record_sampler_cleanup,
    )
    monkeypatch.setattr(
        ambient_runtime_module.AmbientSessionRuntime,
        "stop_for_session",
        record_ambient_cleanup,
    )

    async def wait_for_event(event: Event) -> None:
        """Wait for one deterministic test witness without blocking the MCP loop."""
        assert await asyncio.to_thread(event.wait, 1.0)

    async def wait_for_emergency_frame(frame: bytes) -> None:
        """Observe the new emergency write after entry and before storage release."""
        assert writes_before_driver_entry
        while frame not in composition.transport.writes_snapshot()[writes_before_driver_entry[0] :]:
            await asyncio.sleep(0)

    async def exercise() -> None:
        """Use the registered SDK client for the exact active-owner route."""
        client_send, server_receive = create_memory_object_stream[SessionMessage](0)
        server_send, client_receive = create_memory_object_stream[SessionMessage | Exception](0)
        low_level = server._mcp_server  # pyright: ignore[reportPrivateUsage]
        async with create_task_group() as group:
            group.start_soon(
                low_level.run,
                cast(MemoryObjectReceiveStream[SessionMessage | Exception], server_receive),
                server_send,
                low_level.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as client:
                await client.initialize()
                started = await client.call_tool(
                    "start_roast_session",
                    {"purpose": "roast", "first_crack_observer_ack": "manual/v1"},
                )
                assert started.structuredContent is not None
                session_id = cast(str, started.structuredContent["session"]["session_id"])
                assert not (await client.call_tool("mark_beans_added", {})).isError

                wrong_owner = await client.call_tool(
                    "emergency_stop", {"expected_session_id": "not-the-active-session"}
                )
                assert wrong_owner.isError
                assert driver_entries == []
                assert runtime_cleanup_calls == []
                canonical_path = tmp_path / "logs" / "roasts" / session_id / "roast.jsonl"
                assert canonical_path.exists()

                mark = asyncio.create_task(client.call_tool("mark_first_crack", {}))
                await wait_for_event(first_crack_persisting)
                stop = asyncio.create_task(
                    client.call_tool("emergency_stop", {"expected_session_id": session_id})
                )
                await wait_for_event(driver_entered)
                assert not release_first_crack.is_set()
                assert len(driver_entries) == 1
                emergency_frame = build_hottop_command_packet(
                    heat_level_percent=0,
                    fan_level_percent=0,
                    main_fan_level_percent=100,
                    cooling_motor_on=True,
                )
                await asyncio.wait_for(wait_for_emergency_frame(emergency_frame), timeout=1.0)

                # The held mark must neither become a live observation nor permit
                # an active export to create derived output.  The before-storage
                # schedule also proves the historical row is still absent on disk.
                held_bytes = canonical_path.read_bytes()
                unavailable = await client.call_tool("export_roast_log", {"session_id": session_id})
                assert unavailable.structuredContent is not None
                assert unavailable.structuredContent["ready"] is False
                assert canonical_path.read_bytes() == held_bytes
                assert not (canonical_path.parent / "roast.csv").exists()
                assert not (canonical_path.parent / "summary.json").exists()
                if hold_schedule == "before_storage":
                    # The single real writer call is intentionally still blocked.
                    # Its frame cannot be present before storage starts.
                    assert [
                        json.loads(line)["kind"] for line in held_bytes.decode().splitlines()
                    ] == ["beans_added"]

                release_first_crack.set()
                stopped = await asyncio.wait_for(stop, timeout=5.0)
                marked = await asyncio.wait_for(mark, timeout=5.0)
                assert writer_timeouts == []
                assert not stopped.isError
                assert marked.isError
                assert runtime_cleanup_calls == ["first_crack", "telemetry", "ambient"]
                state = await client.call_tool("get_roast_state", {"session_id": session_id})
                assert state.structuredContent is not None
                events = state.structuredContent["events"]
                assert [json.loads(frame)["kind"] for frame in canonical_event_frames] == [
                    "first_crack_detected",
                    "fault",
                ]
                # The historical queued mark was stored before the emergency;
                # its displaced completion must not create a second live mark.
                assert sum(event["kind"] == "first_crack_detected" for event in events) == 0
                assert sum(event["kind"] == "fault" for event in events) == 1
                assert state.structuredContent["first_crack_at_utc"] is None
                assert state.structuredContent["development_time_seconds"] is None
                assert state.structuredContent["development_percent"] is None
                assert [
                    json.loads(line)["kind"]
                    for line in canonical_path.read_text(encoding="utf-8").splitlines()
                ] == ["beans_added", "first_crack_detected", "fault"]
            await client_send.aclose()

    asyncio.run(exercise())


def test_simulation_cancelled_held_manual_mark_settles_live_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a legacy caller leaves its owned synthetic mark to settle live."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)
    first_crack_persisting = Event()
    release_first_crack = Event()
    attempts: list[session_module.ManualFirstCrackAttempt] = []
    observer_notifications: list[object | None] = []
    terminal_dispositions: list[str] = []
    original_append = session_module._CanonicalGenerationWriter._append_verified  # pyright: ignore[reportPrivateUsage]

    def hold_first_crack(writer: object, frame: bytes) -> None:
        """Hold the original mark before its canonical append can settle."""
        if b'"first_crack_detected"' in frame:
            first_crack_persisting.set()
            assert release_first_crack.wait(timeout=1.0)
        original_append(writer, frame)  # type: ignore[arg-type]

    monkeypatch.setattr(
        session_module._CanonicalGenerationWriter,  # pyright: ignore[reportPrivateUsage]
        "_append_verified",
        hold_first_crack,
    )

    async def wait_for_event(event: Event) -> None:
        """Wait for one synthetic writer witness without blocking the event loop."""
        assert await asyncio.to_thread(event.wait, 1.0)

    async def wait_for_terminal_audit() -> None:
        """Wait until the owned writer emits its terminal audit disposition."""
        while terminal_dispositions != ["live"]:
            await asyncio.sleep(0)

    async def exercise() -> None:
        """Use the actual registered handler while its store-owned Future is held."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            original_begin = context.session_store.begin_manual_first_crack_write
            original_notify = context.first_crack_runtime.notify_human_mark
            original_terminal_audit = (
                context.session_store._append_manual_first_crack_terminal_audit
            )  # pyright: ignore[reportPrivateUsage]

            def record_attempt(*args: Any, **kwargs: Any) -> session_module.ManualFirstCrackAttempt:
                """Capture the exact store-owned completion Future for this handler call."""
                attempt = original_begin(*args, **kwargs)
                attempts.append(attempt)
                return attempt

            def record_observer_notification(*args: Any, **kwargs: Any) -> None:
                """Record the publication claim consumed by a live settlement."""
                publication_token = kwargs.get("publication_token")
                if publication_token is not None:
                    observer_notifications.append(publication_token)
                original_notify(*args, **kwargs)

            def record_terminal_audit(*args: Any, **kwargs: Any) -> None:
                """Record the terminal disposition without replacing the real audit write."""
                terminal_dispositions.append(cast(str, kwargs["disposition"]))
                original_terminal_audit(*args, **kwargs)

            monkeypatch.setattr(
                context.session_store, "begin_manual_first_crack_write", record_attempt
            )
            monkeypatch.setattr(
                context.first_crack_runtime, "notify_human_mark", record_observer_notification
            )
            monkeypatch.setattr(
                context.session_store,
                "_append_manual_first_crack_terminal_audit",
                record_terminal_audit,
            )
            await _call(
                server,
                "start_roast_session",
                context,
                purpose="roast",
                first_crack_observer_ack="manual/v1",
            )
            await _call(server, "mark_beans_added", context)

            caller = asyncio.create_task(_call(server, "mark_first_crack", context))
            await wait_for_event(first_crack_persisting)
            assert len(attempts) == 1
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert not attempts[0].completion.cancelled()

            release_first_crack.set()
            settlement = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(attempts[0].completion)), timeout=1.0
            )
            assert settlement.outcome == "live"
            assert settlement.event is not None
            await asyncio.wait_for(wait_for_terminal_audit(), timeout=1.0)
            state = cast(RoastSessionState, await _call(server, "get_roast_state", context))
            assert state.first_crack_at_utc is not None
            assert state.development_time_seconds is not None
            assert len(observer_notifications) == 1
            assert terminal_dispositions == ["live"]
            duplicate = cast(EventCommandResult, await _call(server, "mark_first_crack", context))
            assert duplicate.event.kind == settlement.event.kind
            assert duplicate.event.recorded_at_utc == settlement.event.recorded_at_utc.isoformat()
            assert sum(event.kind == "first_crack_detected" for event in state.events) == 1
            await _call(server, "emergency_stop", context, reason="synthetic test cleanup")

    asyncio.run(exercise())


def test_simulation_cancelled_held_manual_mark_becomes_historical_after_exact_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact synthetic Stop displaces a cancelled held mark without replaying Stop."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)
    first_crack_persisting = Event()
    release_first_crack = Event()
    driver_entered = Event()
    attempts: list[session_module.ManualFirstCrackAttempt] = []
    observer_notifications: list[object | None] = []
    terminal_dispositions: list[str] = []
    driver_calls: list[str] = []
    original_append = session_module._CanonicalGenerationWriter._append_verified  # pyright: ignore[reportPrivateUsage]
    original_emergency_stop = HottopRoasterDriver.emergency_stop

    def hold_first_crack(writer: object, frame: bytes) -> None:
        """Hold the original mark before its canonical append can settle."""
        if b'"first_crack_detected"' in frame:
            first_crack_persisting.set()
            assert release_first_crack.wait(timeout=1.0)
        original_append(writer, frame)  # type: ignore[arg-type]

    def record_driver_entry(driver: HottopRoasterDriver, *, reason: str) -> object:
        """Witness exactly one competing registered Stop before releasing the mark."""
        driver_calls.append(reason)
        driver_entered.set()
        return original_emergency_stop(driver, reason=reason)

    monkeypatch.setattr(
        session_module._CanonicalGenerationWriter,  # pyright: ignore[reportPrivateUsage]
        "_append_verified",
        hold_first_crack,
    )
    monkeypatch.setattr(HottopRoasterDriver, "emergency_stop", record_driver_entry)

    async def wait_for_event(event: Event) -> None:
        """Wait for one synthetic witness without blocking the event loop."""
        assert await asyncio.to_thread(event.wait, 1.0)

    async def wait_for_terminal_audit() -> None:
        """Wait until the displaced mark records its historical disposition."""
        while terminal_dispositions != ["historical"]:
            await asyncio.sleep(0)

    async def exercise() -> None:
        """Cancel the registered Mark caller, then let an exact Stop win admission."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            original_begin = context.session_store.begin_manual_first_crack_write
            original_notify = context.first_crack_runtime.notify_human_mark
            original_terminal_audit = (
                context.session_store._append_manual_first_crack_terminal_audit
            )  # pyright: ignore[reportPrivateUsage]

            def record_attempt(*args: Any, **kwargs: Any) -> session_module.ManualFirstCrackAttempt:
                """Capture the exact store-owned completion Future for this handler call."""
                attempt = original_begin(*args, **kwargs)
                attempts.append(attempt)
                return attempt

            def record_observer_notification(*args: Any, **kwargs: Any) -> None:
                """Record any erroneous live observer publication after Stop wins."""
                publication_token = kwargs.get("publication_token")
                if publication_token is not None:
                    observer_notifications.append(publication_token)
                original_notify(*args, **kwargs)

            def record_terminal_audit(*args: Any, **kwargs: Any) -> None:
                """Record the terminal disposition without replacing the real audit write."""
                terminal_dispositions.append(cast(str, kwargs["disposition"]))
                original_terminal_audit(*args, **kwargs)

            monkeypatch.setattr(
                context.session_store, "begin_manual_first_crack_write", record_attempt
            )
            monkeypatch.setattr(
                context.first_crack_runtime, "notify_human_mark", record_observer_notification
            )
            monkeypatch.setattr(
                context.session_store,
                "_append_manual_first_crack_terminal_audit",
                record_terminal_audit,
            )
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(server, "mark_beans_added", context)

            caller = asyncio.create_task(_call(server, "mark_first_crack", context))
            await wait_for_event(first_crack_persisting)
            assert len(attempts) == 1
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert not attempts[0].completion.cancelled()

            stop = asyncio.create_task(
                _call(
                    server,
                    "emergency_stop",
                    context,
                    expected_session_id=started.session.session_id,
                )
            )
            await wait_for_event(driver_entered)
            assert driver_calls == ["manual emergency stop"]
            release_first_crack.set()
            stopped = cast(EventCommandResult, await asyncio.wait_for(stop, timeout=2.0))
            assert stopped.event.kind == "fault"
            settlement = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(attempts[0].completion)), timeout=1.0
            )
            assert settlement.outcome == "historical"
            await asyncio.wait_for(wait_for_terminal_audit(), timeout=1.0)
            state = cast(RoastSessionState, await _call(server, "get_roast_state", context))
            assert sum(event.kind == "first_crack_detected" for event in state.events) == 0
            assert sum(event.kind == "fault" for event in state.events) == 1
            assert state.first_crack_at_utc is None
            assert state.first_crack_monotonic_seconds is None
            assert state.development_time_seconds is None
            assert state.development_percent is None
            assert observer_notifications == []
            assert terminal_dispositions == ["historical"]
            assert driver_calls == ["manual emergency stop"]

    asyncio.run(exercise())


def test_simulation_replaced_exact_active_owner_refuses_before_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement after exact active selection cannot fall back to reason-only Stop."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Replace only the exact owner between selection and endpoint admission."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(server, "mark_beans_added", context)
            original_session = context.session_store.get_active_session()
            assert original_session is not None
            driver_entries: list[str] = []
            runtime_cleanup_calls: list[str] = []
            original_assert = context.session_store.assert_exact_active_emergency_owner
            original_emergency_stop = context.roaster_driver.emergency_stop
            original_freeze = context.first_crack_runtime.freeze_observation_boundary
            original_first_crack_stop = context.first_crack_runtime.stop_for_session
            original_stop_sampler = context.telemetry_sampler.stop_for_session
            original_stop_ambient = context.ambient_runtime.stop_for_session
            replaced = False

            def replace_after_selection(session: object, expected_session_id: str) -> None:
                """Create a successor only after the store accepted the original owner."""
                nonlocal replaced
                original_assert(cast(Any, session), expected_session_id)
                if not replaced:
                    replaced = True
                    context.session_store.stop_session()
                    context.session_store.start_session()

            def record_driver_entry(*, reason: str) -> object:
                """Record driver entry if an identity refusal were incorrectly bypassed."""
                driver_entries.append(reason)
                return original_emergency_stop(reason=reason)

            def record_first_crack_cleanup(
                session: object, ticket: object | None, *, reason: str
            ) -> None:
                """Record whether a refused owner enters first-crack cleanup."""
                runtime_cleanup_calls.append("first_crack")
                original_freeze(cast(Any, session), ticket, reason=reason)

            def record_observer_cleanup(session_id: str, *, reason: str) -> object:
                """Record whether a refused owner stops its first-crack observer."""
                runtime_cleanup_calls.append("observer")
                return original_first_crack_stop(session_id, reason=reason)

            def record_sampler_cleanup(session_id: str, *, reason: str) -> None:
                """Record whether a refused owner enters telemetry cleanup."""
                runtime_cleanup_calls.append("telemetry")
                original_stop_sampler(session_id, reason=reason)

            def record_ambient_cleanup(session_id: str, *, reason: str) -> object:
                """Record whether a refused owner enters ambient cleanup."""
                runtime_cleanup_calls.append("ambient")
                return original_stop_ambient(session_id, reason=reason)

            monkeypatch.setattr(
                context.session_store,
                "assert_exact_active_emergency_owner",
                replace_after_selection,
            )
            monkeypatch.setattr(context.roaster_driver, "emergency_stop", record_driver_entry)
            monkeypatch.setattr(
                context.first_crack_runtime,
                "freeze_observation_boundary",
                record_first_crack_cleanup,
            )
            monkeypatch.setattr(
                context.first_crack_runtime,
                "stop_for_session",
                record_observer_cleanup,
            )
            monkeypatch.setattr(
                context.telemetry_sampler, "stop_for_session", record_sampler_cleanup
            )
            monkeypatch.setattr(context.ambient_runtime, "stop_for_session", record_ambient_cleanup)

            with pytest.raises(ValueError, match="not the latest roast session"):
                await _call(
                    server,
                    "emergency_stop",
                    context,
                    expected_session_id=started.session.session_id,
                )
            assert driver_entries == []
            assert runtime_cleanup_calls == []

            await _call(server, "emergency_stop", context, reason="synthetic cleanup")
            assert driver_entries == ["synthetic cleanup"]

    asyncio.run(exercise())


def test_simulation_stopped_nonfault_exact_stop_refuses_before_runtime_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped non-fault owner cannot enter driver or runtime cleanup through Stop."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Invoke the registered emergency handler against one stopped non-fault owner."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(server, "mark_beans_added", context)
            runtime_cleanup_calls: list[str] = []
            driver_entries: list[str] = []
            original_driver_stop = context.roaster_driver.emergency_stop
            original_first_crack_freeze = context.first_crack_runtime.freeze_observation_boundary
            original_first_crack_stop = context.first_crack_runtime.stop_for_session
            original_sampler_stop = context.telemetry_sampler.stop_for_session
            original_ambient_stop = context.ambient_runtime.stop_for_session

            def record_driver_entry(*, reason: str) -> object:
                """Record an emergency driver call that must not occur."""
                driver_entries.append(reason)
                return original_driver_stop(reason=reason)

            def record_first_crack_cleanup(session_id: str, *, reason: str) -> object:
                """Record first-crack cleanup attempted by a refused owner."""
                runtime_cleanup_calls.append("first_crack")
                return original_first_crack_stop(session_id, reason=reason)

            def record_first_crack_boundary_cleanup(
                session: object, ticket: object | None, *, reason: str
            ) -> None:
                """Record primary-boundary cleanup attempted by a refused owner."""
                runtime_cleanup_calls.append("first_crack_boundary")
                original_first_crack_freeze(cast(Any, session), ticket, reason=reason)

            def record_sampler_cleanup(session_id: str, *, reason: str) -> None:
                """Record telemetry cleanup attempted by a refused owner."""
                runtime_cleanup_calls.append("telemetry")
                original_sampler_stop(session_id, reason=reason)

            def record_ambient_cleanup(session_id: str, *, reason: str) -> object:
                """Record ambient cleanup attempted by a refused owner."""
                runtime_cleanup_calls.append("ambient")
                return original_ambient_stop(session_id, reason=reason)

            monkeypatch.setattr(context.roaster_driver, "emergency_stop", record_driver_entry)
            monkeypatch.setattr(
                context.first_crack_runtime,
                "stop_for_session",
                record_first_crack_cleanup,
            )
            monkeypatch.setattr(
                context.first_crack_runtime,
                "freeze_observation_boundary",
                record_first_crack_boundary_cleanup,
            )
            monkeypatch.setattr(
                context.telemetry_sampler, "stop_for_session", record_sampler_cleanup
            )
            monkeypatch.setattr(context.ambient_runtime, "stop_for_session", record_ambient_cleanup)
            context.session_store.stop_session()

            with pytest.raises(ValueError, match="not a stopped fault session"):
                await _call(
                    server,
                    "emergency_stop",
                    context,
                    expected_session_id=started.session.session_id,
                )

            assert driver_entries == []
            assert runtime_cleanup_calls == []

    asyncio.run(exercise())


def test_simulation_stopped_fault_exact_stop_cleans_verified_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted exact Stop cleans the selected stopped-fault session once."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Enter a stopped fault through Stop, then recover it through exact Stop."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            session_id = started.session.session_id
            await _call(server, "mark_beans_added", context)
            driver_calls: list[str] = []
            original_driver_stop = context.roaster_driver.emergency_stop

            def record_driver_stop(*, reason: str) -> object:
                """Record each driver dispatch while preserving the simulation behavior."""
                driver_calls.append(reason)
                return original_driver_stop(reason=reason)

            monkeypatch.setattr(context.roaster_driver, "emergency_stop", record_driver_stop)
            await _call(server, "emergency_stop", context, reason="enter stopped fault")

            first_crack_cleanup_ids: list[str] = []
            sampler_cleanup_ids: list[str] = []
            ambient_cleanup_ids: list[str] = []
            original_first_crack_freeze = context.first_crack_runtime.freeze_observation_boundary
            original_sampler_stop = context.telemetry_sampler.stop_for_session
            original_ambient_stop = context.ambient_runtime.stop_for_session

            def record_first_crack_cleanup(
                session: object, ticket: object | None, *, reason: str
            ) -> None:
                """Record the exact session receiving first-crack cleanup."""
                first_crack_cleanup_ids.append(cast(Any, session).id)
                original_first_crack_freeze(cast(Any, session), ticket, reason=reason)

            def record_sampler_cleanup(session_id: str, *, reason: str) -> None:
                """Record the exact session receiving sampler cleanup."""
                sampler_cleanup_ids.append(session_id)
                original_sampler_stop(session_id, reason=reason)

            def record_ambient_cleanup(session_id: str, *, reason: str) -> object:
                """Record the exact session receiving ambient cleanup."""
                ambient_cleanup_ids.append(session_id)
                return original_ambient_stop(session_id, reason=reason)

            monkeypatch.setattr(
                context.first_crack_runtime,
                "freeze_observation_boundary",
                record_first_crack_cleanup,
            )
            monkeypatch.setattr(
                context.telemetry_sampler, "stop_for_session", record_sampler_cleanup
            )
            monkeypatch.setattr(context.ambient_runtime, "stop_for_session", record_ambient_cleanup)

            stopped = cast(
                EventCommandResult,
                await _call(
                    server,
                    "emergency_stop",
                    context,
                    expected_session_id=session_id,
                ),
            )

            assert stopped.event.kind == "fault"
            assert driver_calls == ["enter stopped fault", "manual emergency stop"]
            assert first_crack_cleanup_ids == [session_id]
            assert sampler_cleanup_ids == [session_id]
            assert ambient_cleanup_ids == [session_id]

    asyncio.run(exercise())


def test_simulation_stopped_fault_exact_stop_cancellation_failure_cleans_verified_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected stopped-fault owner is cleaned when start cancellation fails."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Raise from stopped-fault cancellation after selection through registered Stop."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            session_id = started.session.session_id
            await _call(server, "mark_beans_added", context)
            driver_calls: list[str] = []
            original_driver_stop = context.roaster_driver.emergency_stop

            def record_driver_stop(*, reason: str) -> object:
                """Record driver dispatches while preserving the initial fault Stop."""
                driver_calls.append(reason)
                return original_driver_stop(reason=reason)

            monkeypatch.setattr(context.roaster_driver, "emergency_stop", record_driver_stop)
            await _call(server, "emergency_stop", context, reason="enter stopped fault")

            first_crack_cleanup_ids: list[str] = []
            sampler_cleanup_ids: list[str] = []
            ambient_cleanup_ids: list[str] = []
            original_first_crack_stop = context.first_crack_runtime.stop_for_session
            original_sampler_stop = context.telemetry_sampler.stop_for_session
            original_ambient_stop = context.ambient_runtime.stop_for_session

            def reject_start_cancellation() -> None:
                """Model the first cancellable stopped-fault step failing."""
                raise RuntimeError("synthetic start cancellation failure")

            def record_first_crack_cleanup(session_id: str, *, reason: str) -> object:
                """Record the exact session receiving first-crack cleanup."""
                first_crack_cleanup_ids.append(session_id)
                return original_first_crack_stop(session_id, reason=reason)

            def record_sampler_cleanup(session_id: str, *, reason: str) -> None:
                """Record the exact session receiving sampler cleanup."""
                sampler_cleanup_ids.append(session_id)
                original_sampler_stop(session_id, reason=reason)

            def record_ambient_cleanup(session_id: str, *, reason: str) -> object:
                """Record the exact session receiving ambient cleanup."""
                ambient_cleanup_ids.append(session_id)
                return original_ambient_stop(session_id, reason=reason)

            monkeypatch.setattr(
                context.session_store,
                "cancel_session_start_for_emergency_stop",
                reject_start_cancellation,
            )
            monkeypatch.setattr(
                context.first_crack_runtime,
                "stop_for_session",
                record_first_crack_cleanup,
            )
            monkeypatch.setattr(
                context.telemetry_sampler, "stop_for_session", record_sampler_cleanup
            )
            monkeypatch.setattr(context.ambient_runtime, "stop_for_session", record_ambient_cleanup)

            with pytest.raises(RuntimeError, match="synthetic start cancellation failure"):
                await _call(
                    server,
                    "emergency_stop",
                    context,
                    expected_session_id=session_id,
                )

            assert driver_calls == ["enter stopped fault"]
            assert first_crack_cleanup_ids == [session_id]
            assert sampler_cleanup_ids == [session_id]
            assert ambient_cleanup_ids == [session_id]

    asyncio.run(exercise())


def test_simulation_reception_tool_projects_one_registered_eligible_snapshot(
    tmp_path: Path,
) -> None:
    """A real in-memory SDK call projects one immutable, canonical receipt record."""
    config = _simulation_config(tmp_path)
    clock = _Clock()
    composition = HottopSimulationComposition(clock=clock, receipt_age_bound_seconds=1)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Drive Start, canonical manual events, and the read over the registered transport."""
        client_send, server_receive = create_memory_object_stream[SessionMessage](0)
        server_send, client_receive = create_memory_object_stream[SessionMessage | Exception](0)
        low_level = server._mcp_server  # pyright: ignore[reportPrivateUsage]
        async with create_task_group() as group:
            group.start_soon(
                low_level.run,
                cast(MemoryObjectReceiveStream[SessionMessage | Exception], server_receive),
                server_send,
                low_level.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as client:
                await client.initialize()
                discovered = await client.list_tools()
                assert any(
                    tool.name == "get_hottop_reception_evidence" for tool in discovered.tools
                )
                assert composition.serial_factory_calls == 0
                assert composition.transport.writes_snapshot() == ()
                started = await client.call_tool(
                    "start_roast_session",
                    {"purpose": "roast", "first_crack_observer_ack": "manual/v1"},
                )
                assert started.structuredContent is not None
                session_id = cast(str, started.structuredContent["session"]["session_id"])
                clock.value = 110.0
                assert not (await client.call_tool("mark_beans_added", {})).isError
                clock.value = 170.0
                assert not (await client.call_tool("mark_first_crack", {})).isError
                clock.value = 179.0
                # The candidate loop first establishes an empty drained floor,
                # then consumes this one scripted frame without native I/O.
                assert not (await client.call_tool("get_roast_state", {})).isError
                initial_writes = len(composition.transport.writes_snapshot())
                for _ in range(100):
                    if len(composition.transport.writes_snapshot()) > initial_writes:
                        break
                    await asyncio.sleep(0.01)
                assert len(composition.transport.writes_snapshot()) > initial_writes
                no_receipt = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": session_id,
                        "evidence_protocol": "hottop_reception_evidence/v1",
                    },
                )
                assert no_receipt.isError is False
                assert no_receipt.structuredContent is not None
                assert no_receipt.structuredContent["record"]["pair"] == {
                    "status": "ineligible",
                    "reasons": ["no_receipt"],
                }
                clock.value = 179.1
                composition.queue_status_bytes(_status_packet(bean_c=100, environment_c=100))
                for _ in range(100):
                    if composition.transport.in_waiting == 0:
                        break
                    await asyncio.sleep(0.01)
                assert composition.transport.in_waiting == 0
                writes_after_floor = len(composition.transport.writes_snapshot())
                for _ in range(100):
                    if len(composition.transport.writes_snapshot()) > writes_after_floor:
                        break
                    await asyncio.sleep(0.01)
                assert len(composition.transport.writes_snapshot()) > writes_after_floor
                clock.value = 180.0
                composition.queue_status_bytes(_status_packet(bean_c=195, environment_c=204))
                for _ in range(100):
                    if composition.transport.in_waiting == 0:
                        break
                    await asyncio.sleep(0.01)
                assert composition.transport.in_waiting == 0
                state = await client.call_tool("get_roast_state", {})
                assert state.isError is False
                assert state.structuredContent is not None
                assert state.structuredContent["elapsed_monotonic_seconds"] == 80.0
                result = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": session_id,
                        "evidence_protocol": "hottop_reception_evidence/v1",
                    },
                )
                assert result.isError is False
                assert result.structuredContent is not None
                payload = result.structuredContent
                assert json.loads(cast(TextContent, result.content[0]).text) == payload
                assert payload["outcome"] == "evidence"
                record = payload["record"]
                assert record["phase"] == "development"
                assert record["pair"]["status"] == "eligible", payload
                assert record["pair"] == {
                    **record["pair"],
                    "status": "eligible",
                    "bean_c": 195.0,
                    "environment_c": 204.0,
                    "unit": "celsius",
                    "age_bound_seconds": 1.0,
                }
                assert record["decision"]["operational_lo_seconds"] == 80.0
                assert record["decision"]["operational_hi_seconds"] == 80.0
                assert record["charge"]["canonical_seconds"] == 10.0
                assert record["first_crack"]["canonical_seconds"] == 70.0
                assert record["first_crack"]["source"] == "manual_mark"
                assert record["command_settlements"]["emergency"] is None
                clock.value = 181.1
                expired = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": session_id,
                        "evidence_protocol": "hottop_reception_evidence/v1",
                    },
                )
                assert expired.isError is False
                assert expired.structuredContent is not None
                assert expired.structuredContent["record"]["pair"] == {
                    "status": "ineligible",
                    "reasons": ["receipt_age_exceeds_bound"],
                }
                writes_before_drop = len(composition.transport.writes_snapshot())
                dropped = await client.call_tool("drop_beans", {})
                assert dropped.isError is False
                for _ in range(100):
                    if len(composition.transport.writes_snapshot()) > writes_before_drop:
                        break
                    await asyncio.sleep(0.01)
                assert len(composition.transport.writes_snapshot()) > writes_before_drop
                settled = None
                for _ in range(100):
                    settled = await client.call_tool(
                        "get_hottop_reception_evidence",
                        {
                            "expected_session_id": session_id,
                            "evidence_protocol": "hottop_reception_evidence/v1",
                        },
                    )
                    assert settled.structuredContent is not None
                    current_settlement = settled.structuredContent["record"]["command_settlements"][
                        "drop"
                    ]
                    if current_settlement["result"] == "accepted_by_host":
                        break
                    await asyncio.sleep(0.01)
                assert settled is not None
                assert settled.isError is False
                assert settled.structuredContent is not None
                settlement_record = settled.structuredContent["record"]
                assert settlement_record["phase"] == "cooling"
                assert settlement_record["drop"]["kind"] == "beans_dropped"
                assert settlement_record["pair"] == {
                    "status": "ineligible",
                    "reasons": ["superseded"],
                }
                drop_settlement = settlement_record["command_settlements"]["drop"]
                assert drop_settlement["transition"] == "drop"
                assert drop_settlement["result"] == "accepted_by_host"
                assert drop_settlement["attempt_seq"] is not None
                assert drop_settlement["physical_effect"] == "not_established"
                assert settlement_record["command_settlements"]["emergency"] is None
                cleanup = await client.call_tool("emergency_stop", {"reason": "test cleanup"})
                assert cleanup.isError is False
                clock.value = 190.0
                stopped = await client.call_tool("get_roast_state", {})
                assert stopped.isError is False
                assert stopped.structuredContent is not None
                assert stopped.structuredContent["elapsed_monotonic_seconds"] == 81.1
                faulted_read = await client.call_tool(
                    "get_hottop_reception_evidence",
                    {
                        "expected_session_id": session_id,
                        "evidence_protocol": "hottop_reception_evidence/v1",
                    },
                )
                assert faulted_read.isError is False
                assert faulted_read.structuredContent == {
                    "evidence_protocol": "hottop_reception_evidence/v1",
                    "evidence_version": 1,
                    "outcome": "refused_identity",
                    "reason_code": "session_faulted",
                    "driver_read": "not_invoked",
                    "requested_session_id": session_id,
                    "record": None,
                }
            await client_send.aclose()

    asyncio.run(exercise())
    recordings = list((tmp_path / "recordings").rglob("*.wav"))
    assert len(recordings) == 1
    with wave.open(str(recordings[0]), "rb") as recording:
        assert recording.getnframes() == 480
    sidecars = list((tmp_path / "recordings").rglob("roast.recording.json"))
    assert len(sidecars) == 1
    assert json.loads(sidecars[0].read_text(encoding="utf-8"))["milestones"] == {}
    observation_logs = list((tmp_path / "logs").rglob("first_crack_observation.jsonl"))
    assert len(observation_logs) == 1
    frozen = next(
        json.loads(line)
        for line in observation_logs[0].read_text(encoding="utf-8").splitlines()
        if json.loads(line)["record_type"] == "fc_observation_summary"
    )
    assert frozen["payload"]["human_mark"]["value"]["session_relative_seconds"]["value"] == 70.0
    assert frozen["payload"]["human_mark"]["value"]["utc"] == {
        "value": None,
        "null_reason": "clock_unavailable",
    }
    assert frozen["payload"]["frozen_at"]["recording_relative_seconds"] == {
        "value": None,
        "null_reason": "clock_unavailable",
    }
    assert frozen["payload"]["frozen_at"]["utc"] == {
        "value": None,
        "null_reason": "clock_unavailable",
    }


def test_simulation_start_refuses_missing_authority_before_serial_factory(tmp_path: Path) -> None:
    """Connected MCP requests reject incomplete simulation Start authority before I/O."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=2.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Submit each incomplete Start request through the in-memory MCP transport."""
        async with create_connected_server_and_client_session(server) as client:
            missing_purpose = await client.call_tool(
                "start_roast_session", {"first_crack_observer_ack": "manual/v1"}
            )
            assert missing_purpose.isError
            assert composition.serial_factory_calls == 0

            missing_acknowledgement = await client.call_tool(
                "start_roast_session", {"purpose": "roast"}
            )
            assert missing_acknowledgement.isError
            assert composition.serial_factory_calls == 0

    asyncio.run(exercise())


def test_trusted_factories_bind_class_implementations_not_instance_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory closures retain the repository implementation selected at assembly."""
    composition = HottopSimulationComposition(
        clock=_Clock(),
        receipt_age_bound_seconds=2.0,
        ambient_samples=(SyntheticAmbientSample(21.5, 41.0, 1011.2),),
    )
    serial_factory = build_trusted_simulation_serial_factory(composition)
    ambient_factory = build_trusted_simulation_ambient_reader_factory(composition)
    selected_callbacks: list[str] = []

    def substituted_serial_transport(_port: str) -> object:
        """Fail if a closure dynamically resolves the replaced instance method."""
        selected_callbacks.append("acquire_serial_transport")
        return object()

    def substituted_ambient_reader(_config: AmbientConfig) -> object:
        """Fail if a closure dynamically resolves the replaced instance method."""
        selected_callbacks.append("build_ambient_reader")
        return object()

    monkeypatch.setattr(composition, "acquire_serial_transport", substituted_serial_transport)
    monkeypatch.setattr(composition, "build_ambient_reader", substituted_ambient_reader)

    assert serial_factory(SYNTHETIC_HOTTOP_PORT) is composition.transport
    reader = ambient_factory(AmbientConfig(mode="yoctopuce"))
    assert reader.read().temperature_c == 21.5
    assert selected_callbacks == []
    assert composition.serial_factory_calls == 1


def test_simulation_sdk_manual_confirmation_keeps_capture_until_finalised(tmp_path: Path) -> None:
    """Registered tools preserve confirmed observation and active same-session capture."""
    source_wav = tmp_path / "silence.wav"
    with wave.open(str(source_wav), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(bytes(32000))
    config = tmp_path / "fixture.yaml"
    config.write_text(
        "\n".join(
            (
                "roaster:",
                "  driver: hottop_kn8828b_2k_plus",
                f"  port: {SYNTHETIC_HOTTOP_PORT}",
                "  hottop_reception: candidate/v1",
                "  temperature_unit: celsius",
                "first_crack:",
                "  mode: audio",
                "  event_authority: manual",
                f"  local_model_dir: {tmp_path / 'model-fixture'}",
                "audio:",
                "  source: wav",
                f"  wav_path: {source_wav}",
                "recording:",
                "  enabled: true",
                "  autocapture: true",
                f"  export_location: {tmp_path / 'captures'}",
                "logging:",
                f"  log_dir: {tmp_path / 'logs'}",
                "ambient:",
                "  mode: disabled",
                "",
            )
        ),
        encoding="utf-8",
    )
    child = tmp_path / "hottop_simulation_child.py"
    child.write_text(
        "\n".join(
            (
                "import sys, time",
                "from pathlib import Path",
                "from unittest.mock import patch",
                "from coffee_roaster_mcp import mcp_server",
                "from coffee_roaster_mcp.hottop_simulation import HottopSimulationComposition",
                "from observation_mcp_child import serve",
                "simulation = HottopSimulationComposition(",
                "    clock=time.monotonic, receipt_age_bound_seconds=1.0",
                ")",
                "def synthetic_entry(*, config_path):",
                "    return mcp_server.create_hottop_simulation_mcp_server(",
                "        config_path=config_path, composition=simulation",
                "    )",
                "with patch.object(mcp_server, 'create_mcp_server', synthetic_entry):",
                "    serve(Path(sys.argv[1]), Path(sys.argv[2]), 'none')",
                "",
            )
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(child), str(config), str(tmp_path)],
        cwd=tmp_path,
        env={
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests" / "support"))),
            "HF_HUB_OFFLINE": "1",
        },
    )
    session_id: str | None = None

    async def exercise() -> None:
        """Call the registered SDK tools across one genuine synthetic lifecycle."""
        nonlocal session_id
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await asyncio.wait_for(client.initialize(), timeout=5.0)

            async def call(name: str, arguments: dict[str, object]) -> dict[str, Any]:
                """Call one registered tool and return its structured result."""
                response = await asyncio.wait_for(client.call_tool(name, arguments), timeout=5.0)
                assert not response.isError, (name, response.content)
                assert response.structuredContent is not None
                return response.structuredContent

            runtime_config = await call("get_runtime_config", {})
            assert runtime_config["first_crack_event_authority"] == "manual"
            assert runtime_config["first_crack_observer_protocol"] == "manual/v1"
            started = await call(
                "start_roast_session",
                {"purpose": "roast", "first_crack_observer_ack": "manual/v1"},
            )
            session_id = started["session"]["session_id"]
            assert session_id is not None
            before_charge = await call("export_roast_log", {"session_id": session_id})
            assert before_charge["ready"] is False
            assert not (tmp_path / "logs" / "roasts" / session_id / "roast.jsonl").exists()
            await call("mark_beans_added", {})
            (tmp_path / "charge-ready").touch()

            async def confirmed_state() -> dict[str, Any]:
                """Wait until the synthetic model's confirmation is projected."""
                while True:
                    state = await call("get_roast_state", {})
                    observation = cast(dict[str, Any], state["first_crack_observation"] or {})
                    if observation.get("observer_state") == "confirmed":
                        return state
                    await asyncio.sleep(0.02)

            before_mark = await asyncio.wait_for(confirmed_state(), timeout=6.0)
            assert before_mark["phase"] == "roasting"
            assert before_mark["first_crack_at_utc"] is None
            assert before_mark["first_crack_monotonic_seconds"] is None
            assert before_mark["development_time_seconds"] is None
            assert before_mark["development_percent"] is None
            assert (
                sum(event["kind"] == "first_crack_detected" for event in before_mark["events"]) == 0
            )
            capture = (
                await call(
                    "get_capture_evidence",
                    {
                        "capture_protocol": "capture_evidence/v1",
                        "intent": "current_session",
                        "expected_session_id": session_id,
                    },
                )
            )["result"]
            assert capture["session_id"] == session_id
            assert capture["inputs"]
            before_real_mark = await call("get_roast_state", {})
            assert before_real_mark["first_crack_monotonic_seconds"] is None
            assert before_real_mark["development_time_seconds"] is None
            assert before_real_mark["development_percent"] is None
            assert (
                sum(event["kind"] == "first_crack_detected" for event in before_real_mark["events"])
                == 0
            )
            marked = await call("mark_first_crack", {})
            after_mark = await call("get_roast_state", {})
            assert after_mark["phase"] == "development"
            assert after_mark["first_crack_at_utc"] == marked["event"]["recorded_at_utc"]
            assert after_mark["first_crack_monotonic_seconds"] is not None
            assert after_mark["development_time_seconds"] is not None
            assert after_mark["development_percent"] is not None
            assert (
                sum(event["kind"] == "first_crack_detected" for event in after_mark["events"]) == 1
            )
            assert await call("mark_first_crack", {}) == marked
            assert not (tmp_path / "capture-stopped").exists()
            capture_after = (
                await call(
                    "get_capture_evidence",
                    {
                        "capture_protocol": "capture_evidence/v1",
                        "intent": "current_session",
                        "expected_session_id": session_id,
                    },
                )
            )["result"]
            assert capture_after["outcome"] == capture["outcome"] == "observed"
            assert capture_after["session_id"] == session_id
            assert capture_after["recorder_generation"] == capture["recorder_generation"]
            assert capture_after["inputs"] == capture["inputs"]
            stopped = await call("emergency_stop", {"reason": "simulation test stop"})
            assert stopped["event"]["kind"] == "fault"
            assert (tmp_path / "capture-stopped").exists()
            exported = await call("export_roast_log", {"session_id": session_id})
            assert exported["ready"] is False

    asyncio.run(exercise())
    assert session_id is not None
    recordings = list((tmp_path / "captures").rglob("*.wav"))
    assert len(recordings) == 1
    assert recordings[0].parent.name == session_id
    with wave.open(str(recordings[0]), "rb") as recording:
        assert recording.getnframes() == 480
        assert recording.getframerate() == 16000
    sidecars = list((tmp_path / "captures").rglob("roast.recording.json"))
    assert len(sidecars) == 1
    sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert sidecar["frame_count"] == 480


def test_composed_lifespan_starts_staged_driver_capture_telemetry_and_synthetic_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted composition uses normal lifecycle ownership without physical adapters."""
    config = _simulation_config(tmp_path)
    clock = _Clock()
    composition = HottopSimulationComposition(
        clock=clock,
        receipt_age_bound_seconds=2.0,
        ambient_samples=(SyntheticAmbientSample(21.5, 41.0, 1011.2),),
    )

    def runtime_builder(
        config: AppConfig, *, session_is_retained: Callable[[str], bool]
    ) -> FirstCrackSessionRuntime:
        """Use normal WAV capture without consulting external artefacts."""

        def unavailable_loader(_first_crack: FirstCrackConfig) -> ObservationModel:
            """Preserve the diagnostic unavailable-model observer path."""
            raise RuntimeError("synthetic observation model unavailable")

        return FirstCrackSessionRuntime(
            config=config,
            detector_adapter_factory=lambda _config: cast(FirstCrackDetectorAdapter, object()),
            observation_model_loader=unavailable_loader,
            session_is_retained=session_is_retained,
        )

    def forbidden_factory(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("simulation attempted a physical adapter factory")

    monkeypatch.setattr(mcp_server, "build_first_crack_session_runtime", runtime_builder)
    monkeypatch.setattr(
        ambient_runtime_module,
        "build_configured_ambient_reader",
        forbidden_factory,
    )
    monkeypatch.setattr(audio_module, "_load_sounddevice", forbidden_factory)
    monkeypatch.setenv("COFFEE_AUDIO_WAV_PATH", str(tmp_path / "redirected.wav"))
    monkeypatch.setenv("COFFEE_FIRST_CRACK_LOCAL_MODEL_DIR", str(tmp_path / "redirected-model"))
    monkeypatch.setenv("COFFEE_RECORDING_EXPORT_LOCATION", str(tmp_path / "redirected-recordings"))
    monkeypatch.setenv("COFFEE_FIRST_CRACK_EVENT_AUTHORITY", "automatic")
    selected_callables: list[str] = []

    def selected_serial_factory(*_args: object, **_kwargs: object) -> None:
        """Record an accidentally selected caller-controlled serial callback."""
        selected_callables.append("serial")

    def selected_ambient_reader_factory(*_args: object, **_kwargs: object) -> None:
        """Record an accidentally selected caller-controlled ambient callback."""
        selected_callables.append("ambient")

    monkeypatch.setattr(
        composition,
        "acquire_serial_transport",
        selected_serial_factory,
    )
    monkeypatch.setattr(
        composition,
        "build_ambient_reader",
        selected_ambient_reader_factory,
    )
    server = create_hottop_simulation_mcp_server(
        config_path=config,
        composition=composition,
    )

    async def exercise() -> None:
        """Enter the registered lifespan and run normal Start/state/Stop tools."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            assert composition.serial_factory_calls == 1
            assert selected_callables == []
            assert composition.transport.writes_snapshot()
            assert started.session.device_state is None
            readback = cast(
                RuntimeConfigSnapshot, await _call(server, "get_runtime_config", context)
            )
            assert readback.first_crack_event_authority == "manual"
            assert readback.first_crack_observer_protocol == "manual/v1"
            capture = cast(
                CaptureEvidenceBootstrapResult,
                await _call(
                    server,
                    "get_capture_evidence",
                    context,
                    capture_protocol="capture_evidence/v1",
                    intent="bootstrap",
                ),
            )
            assert capture.capability == "manual_hottop_simulation_capture"
            await _call(server, "mark_beans_added", context)
            before_mark = cast(RoastSessionState, await _call(server, "get_roast_state", context))
            assert before_mark.phase == "roasting"
            assert before_mark.first_crack_at_utc is None
            marked = cast(EventCommandResult, await _call(server, "mark_first_crack", context))
            after_mark = cast(RoastSessionState, await _call(server, "get_roast_state", context))
            assert after_mark.phase == "development"
            assert after_mark.first_crack_at_utc == marked.event.recorded_at_utc
            assert [event.kind for event in after_mark.events].count("first_crack_detected") == 1
            canonical_session = context.session_store.get_active_session()
            assert canonical_session is not None
            assert canonical_session.first_crack_source == "manual_mark"
            state = cast(RoastSessionState, await _call(server, "get_roast_state", context))
            assert state.ambient_status.status == "ok"
            assert state.ambient_status.temperature_c == 21.5
            assert state.ambient_status.last_reading_monotonic_seconds == 100.0
            assert state.device_state is not None
            assert state.device_state.connected is True

            await _call(server, "emergency_stop", context, reason="synthetic test cleanup")

    asyncio.run(exercise())
    assert list((tmp_path / "recordings").rglob("*.wav"))


def test_trusted_simulation_export_preserves_canonical_writer_and_closed_snapshot(
    tmp_path: Path,
) -> None:
    """Trusted simulation exports only settled writer-owned canonical JSONL."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Run active, terminal, and interrupted registered-tool export paths."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            session_id = started.session.session_id
            active = context.session_store.get_active_session()
            assert active is not None and active.log_writer is not None
            raw_dir = active.log_writer.log_dir
            entries_before = tuple(raw_dir.iterdir()) if raw_dir.exists() else ()
            marker = context.unavailable_export_marker
            assert marker is not None and not marker.exists()
            for arguments in ({}, {"session_id": session_id}):
                unavailable = await _call(server, "export_roast_log", context, **arguments)
                assert unavailable.ready is False
                assert "while this roast session is active" in unavailable.note
                assert Path(unavailable.log_dir) == marker
            entries_after = tuple(raw_dir.iterdir()) if raw_dir.exists() else ()
            assert entries_after == entries_before
            assert not marker.exists()

            await _call(server, "mark_beans_added", context)
            assert active.mock_log_torn is False
            raw_jsonl = raw_dir / "roast.jsonl"
            first_row = json.loads(raw_jsonl.read_text(encoding="utf-8").splitlines()[0])
            assert first_row["kind"] == "beans_added"
            assert active.canonical_writer is not None
            assert (raw_jsonl.stat().st_dev, raw_jsonl.stat().st_ino) == (
                active.canonical_writer._identity  # pyright: ignore[reportPrivateUsage]
            )

            await _call(server, "mark_first_crack", context)
            await _call(server, "drop_beans", context)
            await _call(server, "start_cooling", context)
            await _call(server, "stop_cooling", context)
            canonical_before = raw_jsonl.read_bytes()
            identity_before = (raw_jsonl.stat().st_dev, raw_jsonl.stat().st_ino)
            for arguments in ({}, {"session_id": session_id}):
                exported = await _call(server, "export_roast_log", context, **arguments)
                assert exported.ready is True
                assert Path(exported.jsonl_path) == raw_jsonl
                summary = json.loads(Path(exported.summary_path).read_text(encoding="utf-8"))
                assert summary["roaster_driver"] == HOTTOP_DRIVER_NAME
            assert raw_jsonl.read_bytes() == canonical_before
            assert (raw_jsonl.stat().st_dev, raw_jsonl.stat().st_ino) == identity_before
            event_kinds = [
                json.loads(line)["kind"] for line in canonical_before.decode().splitlines()
            ]
            with Path(raw_dir / "roast.csv").open(newline="", encoding="utf-8") as output:
                assert [
                    row["event"] for row in csv.DictReader(output) if row["event"]
                ] == event_kinds

    asyncio.run(exercise())


def test_trusted_simulation_interrupted_export_uses_a_fresh_composition(tmp_path: Path) -> None:
    """An interrupted synthetic run exports only from its own fresh transport."""
    interrupted_root = tmp_path / "interrupted"
    interrupted_root.mkdir()
    config = _simulation_config(interrupted_root)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Exercise the interrupted export path without reusing a staged transport."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            interrupted = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(server, "mark_beans_added", context)
            await _call(server, "mark_first_crack", context)
            await _call(server, "emergency_stop", context, reason="synthetic interruption")
            session = context.session_store.get_latest_session()
            assert session is not None and session.log_writer is not None
            for arguments in ({}, {"session_id": interrupted.session.session_id}):
                unavailable = await _call(server, "export_roast_log", context, **arguments)
                assert unavailable.ready is False
            assert not (session.log_writer.log_dir / "roast.csv").exists()
            assert not (session.log_writer.log_dir / "summary.json").exists()

    asyncio.run(exercise())


def test_trusted_simulation_export_refuses_occupied_marker_and_unsettled_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trusted simulation fails closed for occupied markers and unsettled writers."""
    occupied_root = tmp_path / "occupied"
    occupied_root.mkdir()
    config = _simulation_config(occupied_root)
    marker = occupied_root / "logs" / ".roastpilot-export-unavailable"
    marker.mkdir(parents=True)
    occupied = create_hottop_simulation_mcp_server(
        config_path=config,
        composition=HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0),
    )

    async def occupied_exercise() -> None:
        """Refuse the opaque export path from a preoccupied namespace."""
        lifespan = occupied.settings.lifespan
        assert lifespan is not None
        async with lifespan(occupied) as context:
            cast(
                StartRoastSessionResult,
                await _call(
                    occupied,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            with pytest.raises(ValueError, match="Roast export is unavailable\\."):
                await _call(occupied, "export_roast_log", context)

    asyncio.run(occupied_exercise())

    healthy_root = tmp_path / "unsettled"
    healthy_root.mkdir()
    healthy_config = _simulation_config(healthy_root)
    healthy = create_hottop_simulation_mcp_server(
        config_path=healthy_config,
        composition=HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0),
    )

    def wait_for_idle(*, timeout_seconds: float) -> bool:
        """Model one writer that cannot settle inside the bounded export wait."""
        assert timeout_seconds >= 0
        return False

    async def unsettled_exercise() -> None:
        """Refuse an otherwise healthy terminal composition with a held writer."""
        lifespan = healthy.settings.lifespan
        assert lifespan is not None
        async with lifespan(healthy) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    healthy,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(healthy, "mark_beans_added", context)
            await _call(healthy, "drop_beans", context)
            await _call(healthy, "start_cooling", context)
            await _call(healthy, "stop_cooling", context)
            session = context.session_store.get_latest_session()
            assert session is not None and session.canonical_writer is not None
            monkeypatch.setattr(session.canonical_writer, "wait_for_idle", wait_for_idle)
            unavailable = await _call(
                healthy, "export_roast_log", context, session_id=started.session.session_id
            )
            assert unavailable.ready is False
            assert not (session.log_writer.log_dir / "roast.csv").exists()  # type: ignore[union-attr]
            assert not (session.log_writer.log_dir / "summary.json").exists()  # type: ignore[union-attr]

    asyncio.run(unsettled_exercise())


@pytest.mark.parametrize(
    "mutation",
    ["replaced_equal_bytes", "deleted_before_derived", "tampered_after_derived"],
)
def test_trusted_simulation_export_refuses_canonical_log_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Trusted export refuses replacement, deletion, and post-write tampering races."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)

    async def exercise() -> None:
        """Reach a terminal synthetic session, then race its sealed canonical bytes."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            await _call(server, "mark_beans_added", context)
            await _call(server, "drop_beans", context)
            await _call(server, "start_cooling", context)
            await _call(server, "stop_cooling", context)
            session = context.session_store.get_latest_session()
            assert (
                session is not None
                and session.log_writer is not None
                and session.canonical_writer is not None
            )
            canonical_path = session.log_writer.log_dir / "roast.jsonl"
            canonical_bytes = canonical_path.read_bytes()
            if mutation == "replaced_equal_bytes":
                replacement = canonical_path.with_name("replacement.jsonl")
                replacement.write_bytes(canonical_bytes)
                replacement.replace(canonical_path)
                assert canonical_path.read_bytes() == canonical_bytes
                assert (canonical_path.stat().st_dev, canonical_path.stat().st_ino) != (
                    session.canonical_writer._identity  # pyright: ignore[reportPrivateUsage]
                )
            else:
                original_export = mcp_server.export_roast_snapshot

                def race_derived_export(*args: Any, **kwargs: Any) -> Any:
                    """Run the real derived export after the selected canonical race."""
                    if mutation == "deleted_before_derived":
                        canonical_path.unlink()
                        return original_export(*args, **kwargs)
                    exported = original_export(*args, **kwargs)
                    canonical_path.write_bytes(b'{"kind":"tampered"}\n')
                    return exported

                monkeypatch.setattr(mcp_server, "export_roast_snapshot", race_derived_export)

            unavailable = await _call(
                server, "export_roast_log", context, session_id=started.session.session_id
            )
            assert unavailable.ready is False
            if mutation == "deleted_before_derived":
                assert not canonical_path.exists()
                assert not (session.log_writer.log_dir / "roast.csv").exists()
                assert not (session.log_writer.log_dir / "summary.json").exists()

    asyncio.run(exercise())


def test_trusted_simulation_active_export_refuses_during_held_canonical_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active trusted export remains inert while its canonical writer owns a turn."""
    config = _simulation_config(tmp_path)
    composition = HottopSimulationComposition(clock=_Clock(), receipt_age_bound_seconds=1.0)
    server = create_hottop_simulation_mcp_server(config_path=config, composition=composition)
    append_started = Event()
    release_append = Event()
    original_append = session_module._CanonicalGenerationWriter._append_verified  # pyright: ignore[reportPrivateUsage]

    def hold_append(writer: object, frame: bytes) -> None:
        """Hold the beans-added frame after its canonical write begins."""
        original_append(writer, frame)  # type: ignore[arg-type]
        if b'"beans_added"' in frame:
            append_started.set()
            assert release_append.wait(timeout=1.0)

    monkeypatch.setattr(
        session_module._CanonicalGenerationWriter,  # pyright: ignore[reportPrivateUsage]
        "_append_verified",
        hold_append,
    )

    async def exercise() -> None:
        """Observe refusal through the registered active export route."""
        lifespan = server.settings.lifespan
        assert lifespan is not None
        async with lifespan(server) as context:
            started = cast(
                StartRoastSessionResult,
                await _call(
                    server,
                    "start_roast_session",
                    context,
                    purpose="roast",
                    first_crack_observer_ack="manual/v1",
                ),
            )
            mark = asyncio.create_task(_call(server, "mark_beans_added", context))
            assert await asyncio.to_thread(append_started.wait, 1.0)
            session = context.session_store.get_active_session()
            assert session is not None and session.log_writer is not None
            unavailable = await _call(
                server, "export_roast_log", context, session_id=started.session.session_id
            )
            assert unavailable.ready is False
            assert not (session.log_writer.log_dir / "roast.csv").exists()
            assert not (session.log_writer.log_dir / "summary.json").exists()
            release_append.set()
            await mark
            await _call(server, "emergency_stop", context, reason="synthetic test cleanup")

    asyncio.run(exercise())
