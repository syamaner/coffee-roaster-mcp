"""Trusted in-process Hottop simulation composition coverage."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import wave
from collections.abc import Callable
from pathlib import Path
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
from coffee_roaster_mcp.config import AmbientConfig, AppConfig, ConfigError, FirstCrackConfig
from coffee_roaster_mcp.detector import FirstCrackDetectorAdapter
from coffee_roaster_mcp.drivers import (
    HOTTOP_PACKET_LENGTH,
    HOTTOP_PACKET_PREFIX,
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


def _call(server: Any, name: str, context: ServerContext, **kwargs: object) -> object:
    """Call one registered tool with an actual lifecycle context."""
    manager = server._tool_manager  # pyright: ignore[reportPrivateUsage]
    tool = manager.get_tool(name)
    assert tool is not None
    return tool.fn(_tool_context(context), **kwargs)


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
                _call(
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
            readback = cast(RuntimeConfigSnapshot, _call(server, "get_runtime_config", context))
            assert readback.first_crack_event_authority == "manual"
            assert readback.first_crack_observer_protocol == "manual/v1"
            capture = cast(
                CaptureEvidenceBootstrapResult,
                _call(
                    server,
                    "get_capture_evidence",
                    context,
                    capture_protocol="capture_evidence/v1",
                    intent="bootstrap",
                ),
            )
            assert capture.capability == "manual_hottop_simulation_capture"
            _call(server, "mark_beans_added", context)
            before_mark = cast(RoastSessionState, _call(server, "get_roast_state", context))
            assert before_mark.phase == "roasting"
            assert before_mark.first_crack_at_utc is None
            marked = cast(EventCommandResult, _call(server, "mark_first_crack", context))
            after_mark = cast(RoastSessionState, _call(server, "get_roast_state", context))
            assert after_mark.phase == "development"
            assert after_mark.first_crack_at_utc == marked.event.recorded_at_utc
            assert [event.kind for event in after_mark.events].count("first_crack_detected") == 1
            canonical_session = context.session_store.get_active_session()
            assert canonical_session is not None
            assert canonical_session.first_crack_source == "manual_mark"
            state = cast(RoastSessionState, _call(server, "get_roast_state", context))
            assert state.ambient_status.status == "ok"
            assert state.ambient_status.temperature_c == 21.5
            assert state.ambient_status.last_reading_monotonic_seconds == 100.0
            assert state.device_state is not None
            assert state.device_state.connected is True

            _call(server, "emergency_stop", context, reason="synthetic test cleanup")

    asyncio.run(exercise())
    assert list((tmp_path / "recordings").rglob("*.wav"))
