"""Trusted in-process Hottop simulation composition coverage."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

import coffee_roaster_mcp.ambient_runtime as ambient_runtime_module
import coffee_roaster_mcp.artifacts as artifacts_module
import coffee_roaster_mcp.audio as audio_module
import coffee_roaster_mcp.mcp_server as mcp_server
from coffee_roaster_mcp.config import AmbientConfig, AppConfig, ConfigError, FirstCrackConfig
from coffee_roaster_mcp.detector import FirstCrackDetectorAdapter
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
    assert {
        tool.name
        for tool in simulated._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
    } == {
        tool.name
        for tool in legacy_server._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
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

    asyncio.run(exercise())
    assert session_id is not None
    recordings = list((tmp_path / "captures").rglob("*.wav"))
    assert len(recordings) == 1
    assert recordings[0].parent.name == session_id
    with wave.open(str(recordings[0]), "rb") as recording:
        assert recording.getnframes() == 480
        assert recording.getframerate() == 16000


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
