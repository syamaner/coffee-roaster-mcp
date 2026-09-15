"""In-process MCP tool coverage for RoastPilot."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP

from coffee_roaster_mcp.ambient_runtime import AmbientRuntimeSnapshot, AmbientRuntimeState
from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow
from coffee_roaster_mcp.config import AppConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorOutput,
    build_first_crack_detector_adapter,
)
from coffee_roaster_mcp.drivers import (
    DriverLifecycleEvidence,
    EmergencyStopResult,
    MockRoasterDriver,
    RoasterState,
)
from coffee_roaster_mcp.first_crack_runtime import (
    FirstCrackRuntimeSnapshot,
    FirstCrackRuntimeState,
    FirstCrackSessionRuntime,
    RecordingArtifactPlan,
)
from coffee_roaster_mcp.mcp_server import (
    SDK_REQUEST_LOGGER_NAME,
    DriverEvidenceRead,
    RoasterDeviceState,
    SamplerFinalisationEvidence,
    ServerContext,
    _disconnect_finalisation,  # pyright: ignore[reportPrivateUsage]
    _evidence_admission_rejection,  # pyright: ignore[reportPrivateUsage]
    _fail_closed_after_stale_driver_command,  # pyright: ignore[reportPrivateUsage]
    _fault_active_session_after_sampler_failure,  # pyright: ignore[reportPrivateUsage]
    _finalise_cold_characterisation_session,  # pyright: ignore[reportPrivateUsage]
    _process_ambient_runtime_for_active_session,  # pyright: ignore[reportPrivateUsage]
    _process_auto_t0_for_active_session,  # pyright: ignore[reportPrivateUsage]
    _process_first_crack_runtime_for_active_session,  # pyright: ignore[reportPrivateUsage]
    _read_driver_lifecycle_evidence,  # pyright: ignore[reportPrivateUsage]
    _recording_evidence,  # pyright: ignore[reportPrivateUsage]
    _sample_active_session_telemetry,  # pyright: ignore[reportPrivateUsage]
    _serialize_first_crack_status,  # pyright: ignore[reportPrivateUsage]
    _TelemetrySampler,  # pyright: ignore[reportPrivateUsage]
    build_server_context,
    create_mcp_server,
    quiet_sdk_per_request_log,
)
from coffee_roaster_mcp.session import (
    DriverCommandReservation,
    RoastSession,
    RoastSessionStore,
    SessionLifecycleError,
)


def test_sdk_request_logger_name_matches_installed_sdk() -> None:
    """The pinned SDK logger name must match the SDK we suppress, or the guard misses."""
    import mcp.server.lowlevel.server as sdk_server

    assert sdk_server.logger.name == SDK_REQUEST_LOGGER_NAME


def test_cold_characterisation_finalisation_is_clean_and_idempotent(tmp_path: Path) -> None:
    """A safe mock cold session tears down without issuing an actuator command."""
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("logging:\n  sample_interval_seconds: 0.01\n", encoding="utf-8")
    context = build_server_context(config_path=config_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    context.telemetry_sampler.start_for_session(session.id)

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "clean"
    assert result.clean is True
    assert result.session_purpose == "cold_characterisation"
    assert result.final_driver_evidence is not None
    assert result.final_driver_evidence.evidence is not None
    assert result.final_driver_evidence.evidence.connected is False
    assert context.session_store.get_session_snapshot(session_id=session.id).active is False
    assert _finalise_cold_characterisation_session(context, session.id) == result


def test_initial_finalisation_construction_error_releases_fresh_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error before record attach releases only the fresh finalisation admission."""
    import coffee_roaster_mcp.mcp_server as server_module

    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    restarted: list[str] = []
    monkeypatch.setattr(context.telemetry_sampler, "start_for_session", restarted.append)

    def fail_initial(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("initial result failed")

    monkeypatch.setattr(server_module, "_initial_finalisation_result", fail_initial)
    with pytest.raises(RuntimeError, match="initial result failed"):
        _finalise_cold_characterisation_session(context, session.id)
    assert session.pending_driver_command_token is None
    assert session.pending_driver_command_kind is None
    assert session.finalisation is None
    assert session.id not in context.session_store._finalisation_in_progress  # pyright: ignore[reportPrivateUsage]
    assert restarted == [session.id]


def test_confirmed_disconnect_with_nonzero_final_evidence_is_not_clean(tmp_path: Path) -> None:
    """Disconnect confirmation still records unsafe final driver evidence."""
    context = _cold_finalisation_context(tmp_path)

    class UnsafeAfterDisconnectDriver(LifecycleRecordingDriver):
        def disconnect(self) -> None:
            super().disconnect()
            self.non_zero_dimension = "drum_motor_on"

    driver = UnsafeAfterDisconnectDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "completed_not_clean"
    assert result.disconnect.connected_false_confirmed is True
    assert result.stages[3].status == "failed"
    assert result.failures[-1].code == "final_driver_not_safe_zero"


def test_normal_roast_sampler_failure_still_fails_closed(tmp_path: Path) -> None:
    """A normal session's sampler failure remains an emergency-stop path."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session()
    driver.connect()

    _fault_active_session_after_sampler_failure(
        context, session=session, error=RuntimeError("lost")
    )

    assert driver.actions[-1].startswith("emergency_stop:")
    snapshot = context.session_store.get_session_snapshot(session_id=session.id)
    assert snapshot.phase == "fault"


def test_registered_finalisation_tool_runs_in_process_wrapper(tmp_path: Path) -> None:
    """The registered async tool delegates cold finalisation to its worker thread."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    server = create_mcp_server()
    result = asyncio.run(
        _call_tool(
            server, "finalise_cold_characterisation_session", _ctx(context), session_id=session.id
        )
    )
    assert result.status == "clean"


def test_lifecycle_evidence_conversion_and_impossible_none_fail_closed() -> None:
    """Malformed lifecycle values and absent read evidence remain non-admissible."""

    class CorruptEvidence(DriverLifecycleEvidence):
        poisoned = False

        def __getattribute__(self, name: str) -> object:
            if name == "connected" and type(self).poisoned:
                raise ValueError("corrupt lifecycle evidence")
            return super().__getattribute__(name)

    raw = CorruptEvidence(
        driver="mock",
        connected=True,
        command_streaming_required=False,
        command_loop_running=None,
        serial_open=None,
        heat_level_percent=0,
        roast_fan_level_percent=0,
        main_fan_level_percent=0,
        drum_motor_on=False,
        cooling_motor_on=False,
        solenoid_open=False,
        command_send_attempts=None,
        command_write_count=None,
        last_command_write_size=None,
        command_loop_error_count=None,
        status_packet_count=None,
        status_read_error_count=None,
    )
    CorruptEvidence.poisoned = True

    class CorruptDriver:
        def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
            return raw

    driver = CorruptDriver()
    context = SimpleNamespace(roaster_driver=driver)
    assert _read_driver_lifecycle_evidence(cast(ServerContext, context)).outcome == "malformed"
    assert (
        _evidence_admission_rejection(DriverEvidenceRead("now", "read", None, None))
        == "driver_state_malformed"
    )


def test_unexpected_evidence_property_error_releases_finalisation_admission(tmp_path: Path) -> None:
    """A property failure is typed malformed evidence, never a leaked reservation."""

    class ExplodingEvidence(DriverLifecycleEvidence):
        poisoned = False

        def __getattribute__(self, name: str) -> object:
            if name == "heat_level_percent" and type(self).poisoned:
                raise RuntimeError("property failed")
            return super().__getattribute__(name)

    evidence = ExplodingEvidence(
        driver="mock",
        connected=True,
        command_streaming_required=False,
        command_loop_running=None,
        serial_open=None,
        heat_level_percent=0,
        roast_fan_level_percent=0,
        main_fan_level_percent=0,
        drum_motor_on=False,
        cooling_motor_on=False,
        solenoid_open=False,
        command_send_attempts=None,
        command_write_count=None,
        last_command_write_size=None,
        command_loop_error_count=None,
        status_packet_count=None,
        status_read_error_count=None,
    )
    ExplodingEvidence.poisoned = True

    class ExplodingDriver:
        def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
            return evidence

    context = _cold_finalisation_context(tmp_path)
    object.__setattr__(context, "roaster_driver", ExplodingDriver())
    session = context.session_store.start_session(purpose="cold_characterisation")
    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.rejection_reason == "driver_state_malformed"
    assert session.pending_driver_command_token is None
    context.session_store.record_event(session, "beans_added")


@pytest.mark.parametrize("stage_index", (0, 1, 2))
def test_emergency_abort_retains_each_persisted_finalisation_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage_index: int
) -> None:
    """An abort after a stage persist retains that stage's evidence and status."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    runtime = FakeFirstCrackRuntime()
    runtime.finalise_for_session = lambda _session_id: (  # type: ignore[method-assign]
        "not_active",
        None,
        False,
    )
    runtime.recording_for_session = lambda _session_id: (None, None)  # type: ignore[method-assign]
    if stage_index == 2:
        primary, sidecar, annotation = (tmp_path / name for name in ("p.wav", "r.json", "a.json"))
        primary.write_bytes(b"x" * 45)
        sidecar.write_text("{}", encoding="utf-8")
        annotation.write_text("{}", encoding="utf-8")
        runtime.recording_for_session = lambda _session_id: (  # type: ignore[method-assign]
            SimpleNamespace(started_monotonic_seconds=1.0),
            RecordingArtifactPlan(primary, sidecar, annotation, ()),
        )
    _set_first_crack_runtime(context, runtime)
    tripped = False
    original = context.session_store.persist_finalisation

    def persist_then_abort(live_session: RoastSession, record: object) -> object:
        nonlocal tripped
        persisted = original(live_session, record)
        stages = cast(Any, persisted).stages
        if not tripped and stages[stage_index].status in ("completed", "not_applicable", "failed"):
            tripped = True
            context.session_store.emergency_stop(live_session, reason="stage race")
        return persisted

    monkeypatch.setattr(context.session_store, "persist_finalisation", persist_then_abort)
    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "aborted"
    assert result.stages[stage_index].status in ("completed", "not_applicable", "failed")


def test_recording_evidence_includes_additional_wavs(tmp_path: Path) -> None:
    """Every planned additional WAV is retained in final recording evidence."""
    paths = [
        tmp_path / name for name in ("main.wav", "recording.json", "annotation.json", "extra.wav")
    ]
    for path in paths:
        path.write_bytes(b"x" * 45)
    plan = RecordingArtifactPlan(paths[0], paths[1], paths[2], (paths[3],))
    recorder = SimpleNamespace(started_monotonic_seconds=1.0)
    evidence = _recording_evidence(plan, recorder)
    assert evidence.outcome == "finalised"
    assert [item.role for item in evidence.artifacts] == [
        "primary_wav",
        "recording_sidecar",
        "annotation_session_sidecar",
        "additional_wav",
    ]


def test_additional_wav_metadata_error_is_retained_as_missing_artifact(tmp_path: Path) -> None:
    """A failed additional-WAV metadata read becomes terminal recording evidence."""

    class UnreadablePath:
        name = "extra.wav"

        def is_file(self) -> bool:
            raise OSError("unreadable")

        def stat(self) -> object:
            raise OSError("unreadable")

        def __str__(self) -> str:
            return "extra.wav"

    primary, sidecar, annotation = (tmp_path / name for name in ("p.wav", "r.json", "a.json"))
    for path in (primary, sidecar, annotation):
        path.write_bytes(b"x" * 44)
    plan = RecordingArtifactPlan(primary, sidecar, annotation, (cast(Path, UnreadablePath()),))
    evidence = _recording_evidence(plan, SimpleNamespace(started_monotonic_seconds=1.0))
    assert evidence.outcome == "failed"
    assert evidence.artifacts[-1].exists is False
    assert evidence.artifacts[-1].size_bytes is None


def test_recording_evidence_rejects_header_only_primary_wav(tmp_path: Path) -> None:
    """A valid but zero-frame WAV header is not recording completion evidence."""
    primary, sidecar, annotation = (tmp_path / name for name in ("p.wav", "r.json", "a.json"))
    primary.write_bytes(b"R" * 44)
    sidecar.write_text("{}", encoding="utf-8")
    annotation.write_text("{}", encoding="utf-8")

    evidence = _recording_evidence(
        RecordingArtifactPlan(primary, sidecar, annotation, ()),
        SimpleNamespace(started_monotonic_seconds=1.0),
    )

    assert evidence.outcome == "failed"
    assert evidence.artifacts[0].size_bytes == 44


@pytest.mark.parametrize("size, expected", ((44, "failed"), (45, "finalised")))
def test_recording_evidence_uses_the_wav_threshold_for_every_wav(
    tmp_path: Path, size: int, expected: str
) -> None:
    """Primary and additional WAVs require at least one frame beyond the header."""
    primary, sidecar, annotation, additional = (
        tmp_path / name for name in ("p.wav", "r.json", "a.json", "extra.wav")
    )
    primary.write_bytes(b"x" * size)
    additional.write_bytes(b"x" * size)
    sidecar.write_text("{}", encoding="utf-8")
    annotation.write_text("{}", encoding="utf-8")

    evidence = _recording_evidence(
        RecordingArtifactPlan(primary, sidecar, annotation, (additional,)),
        SimpleNamespace(started_monotonic_seconds=1.0),
    )

    assert evidence.outcome == expected


def test_failed_capture_start_finalises_not_clean_without_a_retry(tmp_path: Path) -> None:
    """No-resource capture startup failure is terminal evidence, not a partial retry."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    runtime = FakeFirstCrackRuntime()
    runtime.finalise_for_session = lambda _session_id: (  # type: ignore[method-assign]
        "stop_failed",
        "Audio capture did not start.",
        False,
    )
    runtime.recording_for_session = lambda _session_id: (None, None)  # type: ignore[method-assign]
    _set_first_crack_runtime(context, runtime)

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "completed_not_clean"
    assert result.stages[1].status == "failed"
    assert result.disconnect.attempt_count == 1
    assert session.pending_driver_command_token is None
    assert _finalise_cold_characterisation_session(context, session.id) == result


def test_invalid_finalisation_reservation_returns_its_retained_abort(tmp_path: Path) -> None:
    """A later invocation returns the aborted retained result without disconnecting."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    _, rejection, generation = context.session_store.begin_finalisation(session.id)
    assert rejection is None and generation is not None
    record = SimpleNamespace(status="partial", reservation_generation=generation, retained=False)
    context.session_store.attach_finalisation(session, record)
    session.pending_driver_command_token = None
    session.pending_driver_command_kind = None
    context.session_store.finish_finalisation_invocation(session)
    returned = _finalise_cold_characterisation_session(context, session.id)
    assert returned is not record and returned.status == record.status == "aborted"
    assert driver.actions == []


def test_recording_metadata_filesystem_error_is_terminal_and_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable recording metadata is retained as a terminal recording failure."""
    context = _cold_finalisation_context(tmp_path)
    _set_first_crack_runtime(context, RecordingFailureRuntime(tmp_path, "recording_sidecar"))
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()

    def unreadable_path(_self: Path) -> bool:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(Path, "is_file", unreadable_path)
    result = _finalise_cold_characterisation_session(context, session.id)
    assert result.status == "completed_not_clean"
    assert result.stages[2].status == "failed"
    assert result.disconnect.connected_false_confirmed is True


def test_pre_disconnect_unreadable_evidence_retains_partial_result(tmp_path: Path) -> None:
    """A failed revalidation never starts disconnect and remains resumable."""

    class UnreadableBeforeDisconnectDriver(LifecycleRecordingDriver):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("unreadable before disconnect")
            return super().read_lifecycle_evidence()

    context = _cold_finalisation_context(tmp_path)
    driver = UnreadableBeforeDisconnectDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    result = _finalise_cold_characterisation_session(context, session.id)
    assert result.status == "partial"
    assert result.failures[-1].code == "driver_state_unreadable"
    assert driver.actions == ["connect"]


def test_disconnect_exception_is_retained_for_disconnect_only_retry(tmp_path: Path) -> None:
    """An exception after a committed disconnect attempt is confirmation-indeterminate."""

    class RaisingDisconnectDriver(LifecycleRecordingDriver):
        def disconnect(self) -> None:
            self.actions.append("disconnect")
            raise RuntimeError("disconnect failed")

    context = _cold_finalisation_context(tmp_path)
    driver = RaisingDisconnectDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    result = _finalise_cold_characterisation_session(context, session.id)
    assert result.status == "disconnect_indeterminate"
    assert result.disconnect.last_error == "RuntimeError: disconnect failed"


def test_resume_returns_retained_abort_when_revalidation_invalidates_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An emergency-path invalidation between admission and work returns its retained abort."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    aborted = SimpleNamespace(status="aborted", abort_reason="session_or_reservation_changed")

    def return_aborted(_session: RoastSession, _generation: int | None) -> object:
        return aborted

    monkeypatch.setattr(
        context.session_store,
        "abort_finalisation_if_invalid",
        return_aborted,
    )
    assert _finalise_cold_characterisation_session(context, session.id) is aborted


def test_normal_session_finalisation_is_rejected_without_disconnect(tmp_path: Path) -> None:
    """Normal roast sessions are not eligible for cold-characterisation teardown."""
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    context = build_server_context(config_path=config_path)
    session = context.session_store.start_session()
    context.roaster_driver.connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "rejected"
    assert result.rejection_reason == "session_purpose_not_eligible"
    assert context.roaster_driver.read_state().connected is True


@pytest.mark.parametrize(
    "dimension",
    (
        "heat_level_percent",
        "roast_fan_level_percent",
        "main_fan_level_percent",
        "drum_motor_on",
        "cooling_motor_on",
        "solenoid_open",
    ),
)
def test_cold_finalisation_rejects_each_nonzero_lifecycle_dimension(
    tmp_path: Path, dimension: str
) -> None:
    """N1-N6: every non-zero command dimension fails admission without teardown."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver(non_zero_dimension=dimension)
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "rejected"
    assert result.rejection_reason == "driver_state_not_safe_zero"
    assert result.admission_driver_evidence is None
    assert driver.actions == ["connect"]
    assert context.session_store.get_session_snapshot(session_id=session.id).active is True


@pytest.mark.parametrize(
    ("driver_kind", "reason"),
    (
        ("unsupported", "driver_lifecycle_evidence_unsupported"),
        ("unreadable", "driver_state_unreadable"),
        ("malformed", "driver_state_malformed"),
        ("disconnected", "driver_not_connected"),
    ),
)
def test_cold_finalisation_rejects_unadmissible_driver_evidence(
    tmp_path: Path, driver_kind: str, reason: str
) -> None:
    """N7-N10: unsupported, unreadable, malformed, and disconnected evidence fails closed."""
    context = _cold_finalisation_context(tmp_path)
    driver: object
    if driver_kind == "unsupported":
        driver = RecordingRoasterDriver()
    elif driver_kind == "unreadable":
        driver = BrokenLifecycleDriver("raise")
    elif driver_kind == "malformed":
        driver = BrokenLifecycleDriver("malformed")
    else:
        driver = LifecycleRecordingDriver(initially_connected=False)
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    if hasattr(driver, "connect") and not isinstance(driver, LifecycleRecordingDriver):
        cast(Any, driver).connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "rejected"
    assert result.rejection_reason == reason
    assert context.session_store.get_session_snapshot(session_id=session.id).active is True


def test_rejected_admission_restarts_sampler_without_fencing_commands(tmp_path: Path) -> None:
    """Evidence rejection restores a cold session sampler after reservation cleanup."""

    class RestartTrackingSampler:
        def __init__(self) -> None:
            self.started: list[str] = []

        def start_for_session(self, session_id: str) -> None:
            self.started.append(session_id)

    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver(non_zero_dimension="drum_motor_on")
    sampler = RestartTrackingSampler()
    object.__setattr__(context, "roaster_driver", driver)
    object.__setattr__(context, "telemetry_sampler", sampler)
    session = context.session_store.start_session(purpose="cold_characterisation")
    sampler.start_for_session(session.id)
    driver.connect()
    result = _finalise_cold_characterisation_session(context, session.id)
    assert result.status == "rejected"
    assert sampler.started == [session.id, session.id]
    assert context.session_store.reserve_driver_command(session, kind="control").kind == "control"


def test_finalisation_reservation_fences_ordinary_commands_and_events(tmp_path: Path) -> None:
    """N11: a retained finalisation reservation excludes normal command and event mutation."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()
    reserved, rejection, _ = context.session_store.begin_finalisation(session.id)

    assert reserved is session
    assert rejection is None
    with pytest.raises(SessionLifecycleError, match="Another driver command"):
        context.session_store.reserve_driver_command(session, kind="control")
    with pytest.raises(SessionLifecycleError, match="finalisation"):
        context.session_store.record_event_snapshot(session, "beans_added")
    with pytest.raises(SessionLifecycleError, match="finalisation"):
        context.session_store.record_telemetry_sample(
            session,
            bean_temp_c=20.0,
            env_temp_c=20.0,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
        )
    context.session_store.abandon_finalisation_admission(session)


def test_finalisation_result_has_exact_safe_request_and_terminal_schema(tmp_path: Path) -> None:
    """N12: the registered stdio tool takes only a session id and returns terminal evidence."""
    context = _cold_finalisation_context(tmp_path)
    server = create_mcp_server()
    tool = server._tool_manager.get_tool("finalise_cold_characterisation_session")  # pyright: ignore[reportPrivateUsage]
    assert tool is not None
    assert set(tool.parameters["properties"]) == {"session_id"}
    assert tool.parameters["required"] == ["session_id"]
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "clean"
    assert result.disconnect.connected_false_confirmed is True
    assert tuple(stage.stage for stage in result.stages) == (
        "telemetry_sampler",
        "first_crack_runtime",
        "recording",
        "driver_disconnect",
    )


def test_finalisation_path_has_no_forbidden_actuator_calls() -> None:
    """N13: static proof keeps cold finalisation non-actuating except disconnect."""
    tree = ast.parse(
        inspect.getsource(_finalise_cold_characterisation_session)
        + inspect.getsource(_disconnect_finalisation)
        + inspect.getsource(FirstCrackSessionRuntime.finalise_for_session)
    )
    forbidden = {
        "set_heat",
        "set_fan",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "connect",
    }
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & forbidden


def test_finalisation_admission_fences_background_work_before_record_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admission reservation prevents sampler and runtime work before record attach."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    _, rejection, _ = context.session_store.begin_finalisation(session.id)
    assert rejection is None

    def forbidden_read() -> RoasterState:
        raise AssertionError("fenced sampler read the driver")

    monkeypatch.setattr(context.roaster_driver, "read_state", forbidden_read)
    assert _sample_active_session_telemetry(context, session_id=session.id) is False
    _process_first_crack_runtime_for_active_session(context, session_id=session.id)
    _process_ambient_runtime_for_active_session(context, session_id=session.id)


def test_auto_t0_skips_when_finalisation_reservation_is_active(tmp_path: Path) -> None:
    """Automatic T0 cannot mutate a cold session held for finalisation."""
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("session:\n  auto_t0_detection_enabled: true\n", encoding="utf-8")
    context = build_server_context(config_path=config_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    _, rejection, _ = context.session_store.begin_finalisation(session.id)
    assert rejection is None
    device = RoasterDeviceState("mock", True, 100.0, None, 0, 0, False, {})
    assert (
        _process_auto_t0_for_active_session(context, session_id=session.id, device_state=device)
        is False
    )
    assert session.auto_t0_preheat_sample_count == 0


def test_stale_command_for_different_active_session_skips_driver_stop(tmp_path: Path) -> None:
    """A stale command cannot emergency-stop a newer session owner."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver()
    object.__setattr__(context, "roaster_driver", driver)
    old = context.session_store.start_session(purpose="cold_characterisation")
    old.monotonic_stop = old.monotonic_start
    current = context.session_store.start_session(purpose="cold_characterisation")
    _fail_closed_after_stale_driver_command(
        context,
        reservation=DriverCommandReservation(old.id, "stale", "control"),
    )
    assert context.session_store.get_active_session() is current
    assert driver.actions == []


def test_lifecycle_evidence_serializes_safe_zero_and_streaming_transitions(tmp_path: Path) -> None:
    """N14: evidence reports all safe-zero fields before and after a disconnect."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver(streaming=True)
    object.__setattr__(context, "roaster_driver", driver)
    driver.connect()
    before = _read_driver_lifecycle_evidence(context)
    driver.disconnect()
    after = _read_driver_lifecycle_evidence(context)

    assert before.outcome == "read"
    assert before.evidence is not None and before.evidence.safe_zero is True
    assert before.evidence.command_loop_running is True
    assert before.evidence.serial_open is True
    assert after.evidence is not None and after.evidence.connected is False
    assert after.evidence.command_loop_running is False
    assert after.evidence.serial_open is False


def test_emergency_stop_during_finalisation_prevents_later_disconnect(tmp_path: Path) -> None:
    """N5/N12: an emergency stop during teardown must fence the later disconnect commit."""
    context = _cold_finalisation_context(tmp_path)
    driver = LifecycleRecordingDriver()
    object.__setattr__(context, "roaster_driver", driver)
    runtime = BlockingFinalisationRuntime()
    _set_first_crack_runtime(context, runtime)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    results: list[object] = []
    errors: list[BaseException] = []
    thread = Thread(
        target=lambda: _record_finalisation_result(results, errors, context, session.id)
    )
    thread.start()
    assert runtime.finalise_started.wait(timeout=1.0)

    context.session_store.emergency_stop(session, reason="test race")
    runtime.release_finalise.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert not errors
    assert driver.actions == ["connect"]
    assert results and cast(Any, results[0]).status == "aborted"
    assert cast(Any, results[0]).abort_reason == "emergency_stop"
    assert (
        cast(Any, results[0]).emergency_stop_ordering == "emergency_stop_before_disconnect_commit"
    )
    assert session.pending_driver_command_token is None


def test_nonterminal_finalisation_cannot_resume_after_a_later_session_starts(
    tmp_path: Path,
) -> None:
    """N12/N13: an old retained result must not bypass latest-session admission."""
    context = _cold_finalisation_context(tmp_path)
    old = context.session_store.start_session(purpose="cold_characterisation")
    old.finalisation = SimpleNamespace(reservation_generation=7, status="partial")
    old.monotonic_stop = old.monotonic_start
    context.session_store.start_session(purpose="cold_characterisation")

    session, rejection, generation = context.session_store.begin_finalisation(old.id)

    assert session is old
    assert rejection == "not_latest_session"
    assert generation is None


def test_finalisation_reservation_fences_first_crack_and_auto_t0_mutation(tmp_path: Path) -> None:
    """All inference and automatic-T0 mutation paths stop at a finalisation reservation."""
    context = _cold_finalisation_context(tmp_path)
    session = context.session_store.start_session(purpose="cold_characterisation")
    _, rejection, _ = context.session_store.begin_finalisation(session.id)

    assert rejection is None
    with pytest.raises(SessionLifecycleError, match="finalisation"):
        context.session_store.record_first_crack_window_observation(
            session,
            window_sequence_number=1,
            confidence=0.5,
            positive_window_count=1,
            confirmed=False,
            fc_status="listening",
        )
    with pytest.raises(SessionLifecycleError, match="finalisation"):
        context.session_store.record_first_crack_detection_snapshot(
            session, detected_at_monotonic_seconds=session.monotonic_start
        )
    event, snapshot = context.session_store.process_auto_t0_reading_snapshot(
        session, bean_temp_c=100.0, drop_threshold_c=25.0
    )
    assert event is None
    assert snapshot.auto_t0_preheat_sample_count == 0
    context.session_store.abandon_finalisation_admission(session)


@pytest.mark.parametrize(
    "kind",
    ("unknown", "stopped", "faulted", "held"),
)
def test_finalisation_store_rejects_ineligible_sessions(tmp_path: Path, kind: str) -> None:
    """Unknown, stopped, faulted, and held sessions cannot start finalisation."""
    context = _cold_finalisation_context(tmp_path)
    if kind == "unknown":
        session, rejection, _ = context.session_store.begin_finalisation("missing")
        assert session is None and rejection == "unknown_session"
        return
    session = context.session_store.start_session(purpose="cold_characterisation")
    if kind == "stopped":
        session.monotonic_stop = session.monotonic_start
    elif kind == "faulted":
        context.session_store.emergency_stop(session, reason="test")
    else:
        context.session_store.reserve_driver_command(session, kind="control")

    _, rejection, _ = context.session_store.begin_finalisation(session.id)

    assert rejection in {"session_not_active", "session_faulted", "command_in_progress"}


def test_disconnect_indeterminate_retries_only_disconnect_for_same_session(tmp_path: Path) -> None:
    """A failed confirmation retains stages and retries only the bounded disconnect step."""
    context = _cold_finalisation_context(tmp_path)
    driver = RetryLifecycleDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()

    first = _finalise_cold_characterisation_session(context, session.id)
    assert first.status == "disconnect_indeterminate"
    assert first.recovered_after_failure is False
    assert first.disconnect.attempt_count == 1
    assert [stage.status for stage in first.stages[:3]] == [
        "completed",
        "not_applicable",
        "not_applicable",
    ]
    driver.confirm_disconnect = True

    second = _finalise_cold_characterisation_session(context, session.id)
    assert second.status == "clean"
    assert second.recovered_after_failure is True
    assert second.disconnect.attempt_count == 2
    assert second.stages[3].status == "completed"
    assert driver.actions == ["connect", "disconnect", "disconnect"]


def test_returned_indeterminate_result_is_detached_from_later_emergency_abort(
    tmp_path: Path,
) -> None:
    """Finalisation callers retain a stable copy when the store record later aborts."""
    context = _cold_finalisation_context(tmp_path)
    driver = RetryLifecycleDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    returned = _finalise_cold_characterisation_session(context, session.id)
    assert returned.status == "disconnect_indeterminate"
    context.session_store.emergency_stop(session, reason="test")
    assert returned.status == "disconnect_indeterminate"
    assert _finalise_cold_characterisation_session(context, session.id).status == "aborted"


def test_not_applicable_first_crack_stage_is_not_rerun_on_disconnect_retry(tmp_path: Path) -> None:
    """A retained not-applicable stage keeps its original completion evidence."""

    class NotApplicableRuntime(FakeFirstCrackRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.finalise_calls = 0

        def finalise_for_session(self, session_id: str) -> tuple[str, str | None, bool]:
            del session_id
            self.finalise_calls += 1
            return "not_active", None, False

        def recording_for_session(self, session_id: str) -> tuple[None, None]:
            del session_id
            return None, None

    context = _cold_finalisation_context(tmp_path)
    driver = RetryLifecycleDriver()
    runtime = NotApplicableRuntime()
    object.__setattr__(context, "roaster_driver", driver)
    _set_first_crack_runtime(context, runtime)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    first = _finalise_cold_characterisation_session(context, session.id)
    stage = first.stages[1]
    assert stage.status == "not_applicable"
    driver.confirm_disconnect = True
    second = _finalise_cold_characterisation_session(context, session.id)
    assert second.status == "clean"
    assert runtime.finalise_calls == 1
    assert second.stages[1].completed_at_utc == stage.completed_at_utc
    assert second.stages[1].completed_in_attempt == stage.completed_in_attempt


def test_sampler_join_timeout_resumes_only_the_sampler_stage(tmp_path: Path) -> None:
    """A bounded sampler timeout retains partial finalisation and resumes cleanly."""
    context = _cold_finalisation_context(tmp_path)
    sampler = RetryFinalisationSampler()
    object.__setattr__(context, "telemetry_sampler", sampler)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()

    partial = _finalise_cold_characterisation_session(context, session.id)
    assert partial.status == "partial"
    assert partial.recovered_after_failure is False
    assert partial.stages[0].status == "incomplete"
    completed = _finalise_cold_characterisation_session(context, session.id)

    assert completed.status == "clean"
    assert completed.recovered_after_failure is True
    assert sampler.calls == 2


def test_real_sampler_retains_thread_owner_until_timeout_retry_joins() -> None:
    """A timed-out finalisation retry joins the same stopped sampler thread."""
    entered = Event()
    release = Event()

    def block_sample(_session_id: str) -> bool:
        entered.set()
        assert release.wait(timeout=2.0)
        return False

    sampler = _TelemetrySampler(interval_seconds=0.01, sample_callback=block_sample)
    sampler.start_for_session("cold-session")
    assert entered.wait(timeout=1.0)
    first = sampler.stop_and_join_for_finalisation("cold-session")
    assert first.owned_by_session_before_stop is True
    assert first.thread_alive_after_join is True
    release.set()
    second = sampler.stop_and_join_for_finalisation("cold-session")
    assert second.owned_by_session_before_stop is True
    assert second.thread_alive_after_join is False


def test_first_crack_stop_failure_resumes_without_rerunning_sampler(tmp_path: Path) -> None:
    """Capture-stop failure is partial and retries only the first-crack stage."""
    context = _cold_finalisation_context(tmp_path)
    runtime = RetryFinalisationRuntime()
    sampler = RetryFinalisationSampler(alive_on_first=False)
    _set_first_crack_runtime(context, runtime)
    object.__setattr__(context, "telemetry_sampler", sampler)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()

    partial = _finalise_cold_characterisation_session(context, session.id)
    assert partial.status == "partial"
    assert partial.recovered_after_failure is False
    assert partial.stages[1].status == "incomplete"
    completed = _finalise_cold_characterisation_session(context, session.id)

    assert completed.status == "clean"
    assert completed.recovered_after_failure is True
    assert runtime.calls == 2
    assert sampler.calls == 1


@pytest.mark.parametrize("failure", ("not_started", "recording_sidecar", "annotation_sidecar"))
def test_recording_failure_is_terminal_not_clean_and_idempotent(
    tmp_path: Path, failure: str
) -> None:
    """Every recording failure still disconnects and retains one terminal result."""
    context = _cold_finalisation_context(tmp_path)
    runtime = RecordingFailureRuntime(tmp_path, failure)
    _set_first_crack_runtime(context, runtime)
    session = context.session_store.start_session(purpose="cold_characterisation")
    context.roaster_driver.connect()

    result = _finalise_cold_characterisation_session(context, session.id)

    assert result.status == "completed_not_clean"
    assert result.clean is False and result.retained is True
    assert result.recovered_after_failure is False
    assert result.stages[2].status == "failed"
    assert result.disconnect.connected_false_confirmed is True
    assert result.failures[-1].stage == "recording"
    assert _finalise_cold_characterisation_session(context, session.id) == result


def test_disconnect_commit_wins_over_waiting_emergency_stop(tmp_path: Path) -> None:
    """An emergency request cannot interleave after finalisation commits disconnect."""
    context = _cold_finalisation_context(tmp_path)
    driver = BlockingDisconnectDriver()
    object.__setattr__(context, "roaster_driver", driver)
    server = create_mcp_server()
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    results: list[object] = []
    errors: list[BaseException] = []
    finaliser = Thread(
        target=lambda: _record_finalisation_result(results, errors, context, session.id)
    )
    finaliser.start()
    assert driver.disconnect_started.wait(timeout=1.0)
    emergency = Thread(
        target=lambda: _record_tool_error(errors, server, "emergency_stop", _ctx(context))
    )
    emergency.start()
    driver.release_disconnect.set()
    finaliser.join(timeout=1.0)
    emergency.join(timeout=1.0)
    assert not finaliser.is_alive() and not emergency.is_alive()
    assert (
        results and cast(Any, results[0]).emergency_stop_ordering == "finalisation_committed_first"
    )
    assert driver.actions == ["connect", "disconnect"]
    assert len(errors) == 1 and isinstance(errors[0], ValueError)


def test_sampler_fault_waits_for_committed_disconnect_without_corrupting_result(
    tmp_path: Path,
) -> None:
    """A sampler fault cannot interleave its driver stop with committed disconnect."""
    context = _cold_finalisation_context(tmp_path)
    driver = BlockingDisconnectDriver()
    object.__setattr__(context, "roaster_driver", driver)
    session = context.session_store.start_session(purpose="cold_characterisation")
    driver.connect()
    results: list[object] = []
    errors: list[BaseException] = []
    finaliser = Thread(
        target=lambda: _record_finalisation_result(results, errors, context, session.id)
    )
    finaliser.start()
    assert driver.disconnect_started.wait(timeout=1.0)
    fault_started = Event()

    def fault() -> None:
        fault_started.set()
        _fault_active_session_after_sampler_failure(
            context, session=session, error=RuntimeError("sampler")
        )

    fault_thread = Thread(target=fault)
    fault_thread.start()
    assert fault_started.wait(timeout=1.0)
    assert driver.actions == ["connect", "disconnect"]
    driver.release_disconnect.set()
    finaliser.join(timeout=1.0)
    fault_thread.join(timeout=1.0)
    assert not finaliser.is_alive() and not fault_thread.is_alive()
    assert not errors
    assert cast(Any, results[0]).status == "clean"
    assert driver.actions == ["connect", "disconnect"]
    snapshot = context.session_store.get_session_snapshot(session_id=session.id)
    assert snapshot.phase != "fault" and cast(Any, snapshot.finalisation).status == "clean"


def test_quiet_sdk_per_request_log_suppresses_info_keeps_warning() -> None:
    """Quieting raises the SDK per-request logger to WARNING without touching others."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    project_logger = logging.getLogger("coffee_roaster_mcp.audio")
    original_sdk_level = sdk_logger.level
    original_project_level = project_logger.level
    sdk_logger.setLevel(logging.INFO)
    project_logger.setLevel(logging.INFO)
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.WARNING
        assert sdk_logger.isEnabledFor(logging.WARNING)
        assert not sdk_logger.isEnabledFor(logging.INFO)
        # The project's own INFO logging (e.g. mic-overflow recovery) is untouched.
        assert project_logger.level == logging.INFO
    finally:
        sdk_logger.setLevel(original_sdk_level)
        project_logger.setLevel(original_project_level)


def test_quiet_sdk_per_request_log_only_raises_never_lowers() -> None:
    """A stricter user/config level (e.g. ERROR) is preserved, not trampled to WARNING."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    original_sdk_level = sdk_logger.level
    sdk_logger.setLevel(logging.ERROR)
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.ERROR
    finally:
        sdk_logger.setLevel(original_sdk_level)


def test_quiet_sdk_per_request_log_pins_warning_before_logging_configured() -> None:
    """Regression (#162): the production order is quiet() THEN the SDK's .run() configures
    INFO. At the quiet call the effective level is the inherited default WARNING; the guard
    (`<= WARNING`, vs the old `<`) pins an EXPLICIT WARNING so it survives the later root INFO
    config — fixing the flood."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    root = logging.getLogger()
    original_sdk_level = sdk_logger.level
    original_root_level = root.level
    sdk_logger.setLevel(logging.NOTSET)  # the real startup state — no explicit level
    root.setLevel(logging.WARNING)  # nothing has configured INFO yet
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.WARNING  # explicit, set despite inherited WARNING
        # the SDK's .run() configures INFO on the root afterwards; the explicit WARNING wins.
        root.setLevel(logging.INFO)
        assert sdk_logger.getEffectiveLevel() == logging.WARNING
        assert not sdk_logger.isEnabledFor(logging.INFO)
    finally:
        sdk_logger.setLevel(original_sdk_level)
        root.setLevel(original_root_level)


def test_quiet_sdk_per_request_log_preserves_inherited_stricter_level() -> None:
    """Augment #182: a NOTSET SDK logger under a STRICTER root (e.g. ERROR) must be left
    alone — pinning WARNING there would LOWER the effective threshold, breaking "only raises".
    Keying off getEffectiveLevel() (not .level) preserves the inherited ERROR."""
    sdk_logger = logging.getLogger(SDK_REQUEST_LOGGER_NAME)
    root = logging.getLogger()
    original_sdk_level = sdk_logger.level
    original_root_level = root.level
    sdk_logger.setLevel(logging.NOTSET)
    root.setLevel(logging.ERROR)
    try:
        quiet_sdk_per_request_log()

        assert sdk_logger.level == logging.NOTSET  # untouched
        assert sdk_logger.getEffectiveLevel() == logging.ERROR  # inherited ERROR preserved
    finally:
        sdk_logger.setLevel(original_sdk_level)
        root.setLevel(original_root_level)


def test_in_process_mcp_tools_cover_mock_roast_and_export(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    server_info = _call_tool(server, "get_server_info", ctx)
    assert server_info.bootstrap_safe is True
    assert "export_roast_log" in server_info.available_bootstrap_tools

    runtime_config = _call_tool(server, "get_runtime_config", ctx)
    assert runtime_config.config_source == str(config_path)
    assert runtime_config.first_crack_mode == "disabled"

    start_result = _call_tool(server, "start_roast_session", ctx)
    session_id = start_result.session.session_id
    assert start_result.session.phase == "pre_roast"
    assert start_result.session.log_dir is not None

    heat_result = _call_tool(server, "set_heat", ctx, heat_level_percent=70)
    fan_result = _call_tool(server, "set_fan", ctx, fan_level_percent=40)
    assert heat_result.heat_level_percent == 70
    assert fan_result.fan_level_percent == 40

    beans_added = _call_tool(server, "mark_beans_added", ctx)
    first_crack = _call_tool(server, "mark_first_crack", ctx)
    drop = _call_tool(server, "drop_beans", ctx)
    cooling = _call_tool(server, "start_cooling", ctx)
    complete = _call_tool(server, "stop_cooling", ctx)
    assert beans_added.event.kind == "beans_added"
    assert first_crack.event.kind == "first_crack_detected"
    assert drop.phase == "cooling"
    assert cooling.phase == "cooling"
    assert complete.phase == "complete"

    state = _call_tool(server, "get_roast_state", ctx, session_id=session_id)
    assert state.session_id == session_id
    assert state.active is False
    assert state.phase == "complete"
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]
    assert state.first_crack_at_utc is not None
    assert state.development_time_seconds is not None

    export = _call_tool(server, "export_roast_log", ctx, session_id=session_id)
    assert export.ready is True
    assert export.session_id == session_id
    assert Path(export.jsonl_path).exists()
    assert Path(export.csv_path).exists()
    assert Path(export.summary_path).exists()

    rows = [json.loads(line) for line in Path(export.jsonl_path).read_text().splitlines()]
    events = [row for row in rows if row["type"] == "event"]
    assert [event["kind"] for event in events] == [
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
    ]


def test_set_recording_metadata_tool_stores_and_echoes(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    # The tool is registered and listed as a bootstrap-safe tool.
    server_info = _call_tool(server, "get_server_info", ctx)
    assert "set_recording_metadata" in server_info.available_bootstrap_tools

    result = _call_tool(server, "set_recording_metadata", ctx, origin="brazil", roast_num=7)
    assert result.origin == "brazil"
    assert result.roast_num == 7

    # The metadata reached the runtime, so the recorder it builds for a roast
    # names the WAV for the annotation pipeline.
    from dataclasses import replace

    from coffee_roaster_mcp.audio import RoastAudioRecorder
    from coffee_roaster_mcp.config import RecordingConfig
    from coffee_roaster_mcp.first_crack_runtime import build_session_recorder

    recording_config = replace(
        server_context.config,
        recording=RecordingConfig(enabled=True, autocapture=True, export_location=tmp_path),
    )
    session = server_context.session_store.start_session()
    metadata = server_context.first_crack_runtime.set_recording_metadata(
        origin="brazil", roast_num=7
    )
    recorder = build_session_recorder(recording_config, session, metadata=metadata)
    assert isinstance(recorder, RoastAudioRecorder)
    assert recorder.wav_path.name == "mic1-brazil-roast7.wav"

    # Invalid input is rejected.
    with pytest.raises(ValueError, match="origin must not be blank"):
        _call_tool(server, "set_recording_metadata", ctx, origin="  ", roast_num=1)


def test_in_process_mcp_tools_surface_errors_and_audio_bootstrap_state(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                "  allow_manual_override: false",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(server_context, FakeFirstCrackRuntime())
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    server_info = _call_tool(server, "get_server_info", ctx)
    runtime_config = _call_tool(server, "get_runtime_config", ctx)
    assert server_info.first_crack_mode == "audio"
    assert server_info.bootstrap_safe is False
    assert runtime_config.allow_manual_override is False

    with pytest.raises(ValueError, match="No active roast session"):
        _call_tool(server, "set_heat", ctx, heat_level_percent=10)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    with pytest.raises(ValueError, match="Manual first-crack override is disabled"):
        _call_tool(server, "mark_first_crack", ctx)

    with pytest.raises(ValueError, match="Unknown session_id"):
        _call_tool(server, "get_roast_state", ctx, session_id="missing-session")


def test_mcp_roast_controls_call_configured_driver_boundary(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    heat = _call_tool(server, "set_heat", ctx, heat_level_percent=65)
    fan = _call_tool(server, "set_fan", ctx, fan_level_percent=45)
    _call_tool(server, "mark_beans_added", ctx)
    drop = _call_tool(server, "drop_beans", ctx)
    repeated_drop = _call_tool(server, "drop_beans", ctx)
    cooling = _call_tool(server, "start_cooling", ctx)
    complete = _call_tool(server, "stop_cooling", ctx)

    assert driver.actions == [
        "connect",
        "set_heat:65",
        "set_fan:45",
        "drop_beans",
        "stop_cooling",
    ]
    assert heat.heat_level_percent == 65
    assert fan.fan_level_percent == 45
    assert drop.event.kind == "beans_dropped"
    assert drop.phase == "cooling"
    assert repeated_drop.event.kind == "beans_dropped"
    assert repeated_drop.phase == "cooling"
    assert cooling.event.kind == "cooling_started"
    assert complete.phase == "complete"


def test_get_roast_state_exposes_current_driver_state_and_event_timestamps(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(
        bean_temp_c=151.25,
        env_temp_c=204.5,
        raw_vendor_data={"status_packet_count": 7, "vendor_note": "ready"},
    )
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "set_heat", ctx, heat_level_percent=55)
    _call_tool(server, "set_fan", ctx, fan_level_percent=35)
    beans_added = _call_tool(server, "mark_beans_added", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    assert state.device_state is not None
    assert state.device_state.driver == "recording"
    assert state.device_state.connected is True
    assert state.device_state.bean_temp_c == 151.25
    assert state.device_state.env_temp_c == 204.5
    assert state.device_state.heat_level_percent == 55
    assert state.device_state.fan_level_percent == 35
    assert state.device_state.cooling_on is False
    assert state.device_state.raw_vendor_data == {
        "status_packet_count": 7,
        "vendor_note": "ready",
    }
    assert state.beans_added_at_utc == beans_added.event.recorded_at_utc
    assert state.beans_added_monotonic_seconds == beans_added.event.monotonic_seconds
    assert state.first_crack_status.status == "disabled"
    assert state.first_crack_status.mode == "disabled"
    assert state.first_crack_status.detected_at_utc is None
    assert state.first_crack_status.detected_monotonic_seconds is None
    assert state.t0_status.status == "detected"
    assert state.t0_status.auto_detection_enabled is False


def test_get_roast_state_appends_normalized_driver_telemetry_samples(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=151.25, env_temp_c=204.5)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    driver.bean_temp_c = 152.0
    driver.env_temp_c = 205.25
    driver.heat_level_percent = 45
    driver.fan_level_percent = 25
    driver.cooling_on = True
    _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    session = server_context.session_store.get_session_snapshot(
        session_id=start_result.session.session_id
    )
    samples = list(session.telemetry_buffer)
    assert len(samples) == 2
    assert [sample.bean_temp_c for sample in samples] == [151.25, 152.0]
    assert [sample.env_temp_c for sample in samples] == [204.5, 205.25]
    assert samples[1].heat_level_percent == 45
    assert samples[1].fan_level_percent == 25
    assert samples[1].cooling_on is True
    assert samples[0].monotonic_seconds <= samples[1].monotonic_seconds


def test_get_roast_state_driver_read_failure_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)

    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    driver.fail_read = False
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert [event.kind for event in state.events] == ["beans_added"]
    assert state.phase == "roasting"


def test_get_roast_state_auto_t0_driver_read_failure_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True, bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    driver.fail_read = False
    driver.bean_temp_c = 145.0
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "pre_roast"
    assert state.events == ()
    assert state.t0_status.status == "pending"
    assert state.t0_status.charge_temperature_c == 145.0
    assert state.t0_status.current_drop_c == 0.0


def test_get_roast_state_records_automatic_t0_after_configured_drop(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    first_state = _call_tool(server, "get_roast_state", ctx)
    assert first_state.phase == "pre_roast"
    assert first_state.beans_added_at_utc is None
    assert first_state.t0_status.status == "pending"
    assert first_state.t0_status.charge_temperature_c == 170.0
    assert first_state.t0_status.current_drop_c == 0.0

    driver.bean_temp_c = 145.0
    threshold_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )
    assert threshold_state.phase == "roasting"
    assert threshold_state.beans_added_at_utc is not None
    assert threshold_state.t0_status.status == "detected"
    assert threshold_state.t0_status.auto_detection_enabled is True
    assert threshold_state.t0_status.charge_temperature_c == 170.0
    assert threshold_state.t0_status.current_drop_c == 25.0
    assert threshold_state.t0_status.drop_threshold_c == 25.0
    assert threshold_state.t0_status.detected_bean_temperature_c == 145.0
    assert [event.kind for event in threshold_state.events] == ["beans_added"]
    t0_payload = threshold_state.events[0].payload
    assert t0_payload["source"] == "auto_t0"
    assert t0_payload["charge_temperature_c"] == 170.0
    assert t0_payload["detected_bean_temperature_c"] == 145.0
    assert t0_payload["drop_c"] == 25.0
    assert t0_payload["drop_threshold_c"] == 25.0
    # T0 is backdated to the candidate turning point (the 170 °C local max),
    # while the raw confirmation timestamp stays available (#167).
    assert "turning_point_monotonic_seconds" in t0_payload
    assert "confirmed_at_monotonic_seconds" in t0_payload
    assert "confirmed_at_utc" in t0_payload
    assert cast(float, t0_payload["turning_point_monotonic_seconds"]) <= cast(
        float, t0_payload["confirmed_at_monotonic_seconds"]
    )


def test_get_roast_state_discards_queued_first_crack_windows_after_auto_t0(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    runtime = FakeFirstCrackRuntime(record_first_crack_on_process=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    _set_first_crack_runtime(server_context, runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 145.0
    state = _call_tool(server, "get_roast_state", ctx)

    assert state.phase == "development"
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
    ]
    assert runtime.discarded_sessions == [state.session_id]
    assert runtime.processed_sessions == [state.session_id, state.session_id]


def test_get_roast_state_auto_t0_uses_max_preheat_and_ignores_small_drops(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 30",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=160.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 175.0
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 150.1
    small_drop_state = _call_tool(server, "get_roast_state", ctx)
    assert small_drop_state.phase == "pre_roast"
    assert small_drop_state.t0_status.status == "pending"
    assert small_drop_state.t0_status.charge_temperature_c == 175.0
    assert small_drop_state.t0_status.current_drop_c is not None
    assert abs(small_drop_state.t0_status.current_drop_c - 24.9) < 0.000001

    driver.bean_temp_c = 144.9
    detected_state = _call_tool(server, "get_roast_state", ctx)
    assert detected_state.phase == "roasting"
    assert detected_state.t0_status.status == "detected"
    assert detected_state.t0_status.charge_temperature_c == 175.0
    assert detected_state.t0_status.current_drop_c is not None
    assert abs(detected_state.t0_status.current_drop_c - 30.1) < 0.000001


def test_get_roast_state_auto_t0_ignores_disconnected_driver_readings(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.connected = False
    driver.bean_temp_c = 140.0
    disconnected_state = _call_tool(server, "get_roast_state", ctx)

    assert disconnected_state.phase == "pre_roast"
    assert disconnected_state.events == ()
    assert disconnected_state.device_state is not None
    assert disconnected_state.device_state.connected is False
    assert disconnected_state.t0_status.status == "pending"
    assert disconnected_state.t0_status.charge_temperature_c == 170.0
    assert disconnected_state.t0_status.current_drop_c == 0.0


def test_get_roast_state_auto_t0_pending_drop_does_not_round_to_threshold(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                "  auto_t0_drop_threshold_c: 25",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=170.0)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "get_roast_state", ctx)
    driver.bean_temp_c = 145.0004
    state = _call_tool(server, "get_roast_state", ctx)

    assert state.phase == "pre_roast"
    assert state.t0_status.status == "pending"
    assert state.t0_status.current_drop_c is not None
    assert abs(state.t0_status.current_drop_c - 24.9996) < 0.000001
    assert state.t0_status.current_drop_c < state.t0_status.drop_threshold_c


def test_get_roast_state_auto_t0_waits_for_valid_baseline(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "session:",
                "  auto_t0_detection_enabled: true",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(bean_temp_c=None)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    no_temp_state = _call_tool(server, "get_roast_state", ctx)
    assert no_temp_state.phase == "pre_roast"
    assert no_temp_state.t0_status.status == "pending"
    assert no_temp_state.t0_status.charge_temperature_c is None

    driver.bean_temp_c = 125.0
    first_temp_state = _call_tool(server, "get_roast_state", ctx)
    assert first_temp_state.phase == "pre_roast"
    assert first_temp_state.t0_status.status == "pending"
    assert first_temp_state.t0_status.charge_temperature_c == 125.0
    assert first_temp_state.t0_status.current_drop_c == 0.0


def test_get_roast_state_reads_driver_before_detector_side_effects(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_read=True)
    runtime = FakeFirstCrackRuntime(record_first_crack_on_process=False)
    object.__setattr__(server_context, "roaster_driver", driver)
    _set_first_crack_runtime(server_context, runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    runtime.record_first_crack_on_process = True

    with pytest.raises(RuntimeError, match="Could not read current roaster state"):
        _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    failed_read_snapshot = server_context.session_store.get_session_snapshot(
        session_id=start_result.session.session_id
    )
    assert failed_read_snapshot.first_crack_at_utc is None
    assert runtime.processed_sessions == [start_result.session.session_id]

    driver.fail_read = False
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)

    assert state.phase == "development"
    assert state.first_crack_at_utc is not None
    assert [event.kind for event in state.events] == [
        "beans_added",
        "first_crack_detected",
    ]


def test_mark_beans_added_returns_snapshot_after_immediate_detector_confirmation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(
        server_context,
        FakeFirstCrackRuntime(record_first_crack_on_process=True),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    beans_added = _call_tool(server, "mark_beans_added", ctx)

    assert beans_added.event.kind == "beans_added"
    assert beans_added.phase == "development"
    assert beans_added.event_count == 2


def test_get_roast_state_exposes_first_crack_statuses(tmp_path: Path) -> None:
    manual_config_path = tmp_path / "manual.yaml"
    manual_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: manual",
                f"logging:\n  log_dir: {tmp_path / 'manual-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    manual_context = build_server_context(config_path=manual_config_path)
    manual_server = create_mcp_server(config_path=manual_config_path)
    manual_ctx = _ctx(manual_context)
    manual_start = _call_tool(manual_server, "start_roast_session", manual_ctx)
    manual_state = _call_tool(
        manual_server,
        "get_roast_state",
        manual_ctx,
        session_id=manual_start.session.session_id,
    )
    assert manual_state.first_crack_status.status == "manual"
    assert manual_state.first_crack_status.allow_manual_override is True
    assert manual_state.first_crack_status.max_consecutive_overflow_count == 0
    assert manual_state.first_crack_status.last_inference_duration_ms == 0.0
    assert manual_state.first_crack_status.max_inference_duration_ms == 0.0
    assert manual_state.first_crack_status.inference_overrun_count == 0

    manual_unavailable_config_path = tmp_path / "manual-unavailable.yaml"
    manual_unavailable_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: manual",
                "  allow_manual_override: false",
                f"logging:\n  log_dir: {tmp_path / 'manual-unavailable-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    manual_unavailable_context = build_server_context(config_path=manual_unavailable_config_path)
    manual_unavailable_server = create_mcp_server(config_path=manual_unavailable_config_path)
    manual_unavailable_ctx = _ctx(manual_unavailable_context)
    manual_unavailable_start = _call_tool(
        manual_unavailable_server,
        "start_roast_session",
        manual_unavailable_ctx,
    )
    manual_unavailable_state = _call_tool(
        manual_unavailable_server,
        "get_roast_state",
        manual_unavailable_ctx,
        session_id=manual_unavailable_start.session.session_id,
    )
    assert manual_unavailable_state.first_crack_status.status == "unavailable"
    assert manual_unavailable_state.first_crack_status.allow_manual_override is False
    assert (
        manual_unavailable_state.first_crack_status.reason
        == "Manual first-crack mode is configured, but manual override is disabled."
    )
    assert manual_unavailable_state.first_crack_status.max_consecutive_overflow_count == 0
    assert manual_unavailable_state.first_crack_status.last_inference_duration_ms == 0.0
    assert manual_unavailable_state.first_crack_status.max_inference_duration_ms == 0.0
    assert manual_unavailable_state.first_crack_status.inference_overrun_count == 0

    audio_config_path = tmp_path / "audio.yaml"
    audio_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    audio_context = build_server_context(config_path=audio_config_path)
    audio_runtime = FakeFirstCrackRuntime()
    _set_first_crack_runtime(audio_context, audio_runtime)
    audio_server = create_mcp_server(config_path=audio_config_path)
    audio_ctx = _ctx(audio_context)
    audio_start = _call_tool(audio_server, "start_roast_session", audio_ctx)
    audio_state = _call_tool(
        audio_server,
        "get_roast_state",
        audio_ctx,
        session_id=audio_start.session.session_id,
    )
    assert audio_state.first_crack_status.status == "pending"

    _call_tool(audio_server, "mark_beans_added", audio_ctx)
    detected = _call_tool(audio_server, "mark_first_crack", audio_ctx)
    detected_state = _call_tool(
        audio_server,
        "get_roast_state",
        audio_ctx,
        session_id=audio_start.session.session_id,
    )
    assert detected_state.first_crack_status.status == "detected"
    assert detected_state.first_crack_status.detected_at_utc == detected.event.recorded_at_utc
    assert (
        detected_state.first_crack_status.detected_monotonic_seconds
        == detected.event.monotonic_seconds
    )
    assert audio_runtime.stopped_sessions == [audio_start.session.session_id]

    audio_unavailable_config_path = tmp_path / "audio-unavailable.yaml"
    audio_unavailable_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-unavailable-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    audio_unavailable_context = build_server_context(config_path=audio_unavailable_config_path)
    _set_first_crack_runtime(
        audio_unavailable_context,
        FakeFirstCrackRuntime(status="unavailable", reason="missing detector artifacts"),
    )
    audio_unavailable_server = create_mcp_server(config_path=audio_unavailable_config_path)
    audio_unavailable_ctx = _ctx(audio_unavailable_context)
    audio_unavailable_start = _call_tool(
        audio_unavailable_server,
        "start_roast_session",
        audio_unavailable_ctx,
    )
    audio_unavailable_state = _call_tool(
        audio_unavailable_server,
        "get_roast_state",
        audio_unavailable_ctx,
        session_id=audio_unavailable_start.session.session_id,
    )
    assert audio_unavailable_state.first_crack_status.status == "unavailable"
    assert audio_unavailable_state.first_crack_status.reason == "missing detector artifacts"

    fault_config_path = tmp_path / "fault.yaml"
    fault_config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'fault-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    fault_context = build_server_context(config_path=fault_config_path)
    _set_first_crack_runtime(fault_context, FakeFirstCrackRuntime())
    fault_server = create_mcp_server(config_path=fault_config_path)
    fault_ctx = _ctx(fault_context)
    fault_start = _call_tool(fault_server, "start_roast_session", fault_ctx)
    _call_tool(fault_server, "emergency_stop", fault_ctx, reason="unit-test")
    fault_state = _call_tool(
        fault_server,
        "get_roast_state",
        fault_ctx,
        session_id=fault_start.session.session_id,
    )
    assert fault_state.first_crack_status.status == "faulted"


def test_get_roast_state_exposes_ambient_status_disabled_by_default(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    assert start_result.session.ambient_status.status == "disabled"
    assert start_result.session.ambient_status.mode == "disabled"
    assert start_result.session.ambient_status.temperature_c is None

    state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )

    assert state.ambient_status.status == "disabled"
    assert state.ambient_status.ambient_running is False
    assert state.ambient_status.reason == "Ambient sensing is disabled by configuration."


def test_get_roast_state_exposes_ambient_status_unavailable_when_probe_missing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "ambient:",
                "  mode: yoctopuce",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_ambient_runtime(
        server_context,
        FakeAmbientRuntime(status="unavailable", reason="No Yocto-Meteo device found."),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)

    assert start_result.session.ambient_status.status == "unavailable"
    assert start_result.session.ambient_status.mode == "yoctopuce"
    assert start_result.session.ambient_status.reason == "No Yocto-Meteo device found."
    assert start_result.session.ambient_status.temperature_c is None


def test_get_roast_state_exposes_ambient_status_ok_with_readings(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "ambient:",
                "  mode: yoctopuce",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_ambient_runtime(
        server_context,
        FakeAmbientRuntime(
            status="ok",
            ambient_running=True,
            temperature_c=21.4,
            humidity_percent=42.0,
            pressure_hpa=1012.3,
            last_reading_monotonic_seconds=123.0,
        ),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )

    assert state.ambient_status.status == "ok"
    assert state.ambient_status.ambient_running is True
    assert state.ambient_status.temperature_c == 21.4
    assert state.ambient_status.humidity_percent == 42.0
    assert state.ambient_status.pressure_hpa == 1012.3
    assert state.ambient_status.last_reading_monotonic_seconds == 123.0


def test_ambient_runtime_stops_on_terminal_session_transitions(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "\n".join(
            [
                "ambient:",
                "  mode: yoctopuce",
                f"logging:\n  log_dir: {tmp_path / 'logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    fake_ambient = FakeAmbientRuntime(status="ok", ambient_running=True)
    _set_ambient_runtime(server_context, fake_ambient)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    assert fake_ambient.started_sessions == [start_result.session.session_id]

    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "drop_beans", ctx)
    assert fake_ambient.stopped_sessions == []

    _call_tool(server, "stop_cooling", ctx)

    assert fake_ambient.stopped_sessions == [start_result.session.session_id]


def test_get_roast_state_scopes_runtime_metrics_to_requested_session(tmp_path: Path) -> None:
    config_path = tmp_path / "audio.yaml"
    config_path.write_text(
        "\n".join(
            [
                "first_crack:",
                "  mode: audio",
                f"logging:\n  log_dir: {tmp_path / 'audio-logs'}",
            ]
        ),
        encoding="utf-8",
    )
    server_context = build_server_context(config_path=config_path)
    _set_first_crack_runtime(
        server_context,
        FakeFirstCrackRuntime(
            audio_running=True,
            queued_window_count=2,
            emitted_window_count=3,
            dropped_window_count=4,
            processed_window_count=5,
            mic_peak_dbfs=-6.02,
            mic_rms_dbfs=-9.03,
            max_consecutive_overflow_count=8,
            last_inference_duration_ms=125.0,
            max_inference_duration_ms=250.0,
            inference_overrun_count=3,
        ),
    )
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    first_start = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "mark_first_crack", ctx)
    _call_tool(server, "drop_beans", ctx)
    _call_tool(server, "start_cooling", ctx)
    _call_tool(server, "stop_cooling", ctx)
    second_start = _call_tool(server, "start_roast_session", ctx)
    first_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=first_start.session.session_id,
    )

    assert first_state.first_crack_status.status == "detected"
    assert first_state.first_crack_status.audio_running is False
    assert first_state.first_crack_status.queued_window_count == 0
    assert first_state.first_crack_status.emitted_window_count == 0
    assert first_state.first_crack_status.dropped_window_count == 0
    assert first_state.first_crack_status.processed_window_count == 0
    # The inactive session reports no live mic levels (#178).
    assert first_state.first_crack_status.mic_peak_dbfs is None
    assert first_state.first_crack_status.mic_rms_dbfs is None

    second_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=second_start.session.session_id,
    )
    assert second_state.first_crack_status.status == "pending"
    assert second_state.first_crack_status.audio_running is True
    assert second_state.first_crack_status.queued_window_count == 2
    assert second_state.first_crack_status.emitted_window_count == 3
    assert second_state.first_crack_status.dropped_window_count == 4
    assert second_state.first_crack_status.processed_window_count == 5
    # The live mic levels surface in the runtime readout (#178) so a mis-gained
    # or dead mic is visible under real conditions.
    assert second_state.first_crack_status.mic_peak_dbfs == -6.02
    assert second_state.first_crack_status.mic_rms_dbfs == -9.03
    assert second_state.first_crack_status.max_consecutive_overflow_count == 8
    assert second_state.first_crack_status.last_inference_duration_ms == 125.0
    assert second_state.first_crack_status.max_inference_duration_ms == 250.0
    assert second_state.first_crack_status.inference_overrun_count == 3

    # Runtime-bearing paths retain the additive fields; inactive/manual
    # defaults remain wire-compatible zero values.
    assert first_state.first_crack_status.max_consecutive_overflow_count == 0
    assert first_state.first_crack_status.last_inference_duration_ms == 0.0
    assert first_state.first_crack_status.max_inference_duration_ms == 0.0
    assert first_state.first_crack_status.inference_overrun_count == 0


@pytest.mark.parametrize("runtime_status", ["faulted", "unavailable"])
def test_first_crack_status_runtime_branches_preserve_instrumentation_sentinels(
    runtime_status: FirstCrackRuntimeState,
) -> None:
    """Every runtime-bearing status branch propagates non-default sentinels."""
    config = AppConfig(first_crack=FirstCrackConfig(mode="audio"))
    max_consecutive_overflow_count = 17
    last_inference_duration_ms = 123.5
    max_inference_duration_ms = 456.75
    inference_overrun_count = 8

    def runtime_for(
        session_id: str,
        *,
        status: FirstCrackRuntimeState,
    ) -> FirstCrackRuntimeSnapshot:
        return FirstCrackRuntimeSnapshot(
            status=status,
            active_session_id=session_id,
            active=True,
            max_consecutive_overflow_count=max_consecutive_overflow_count,
            last_inference_duration_ms=last_inference_duration_ms,
            max_inference_duration_ms=max_inference_duration_ms,
            inference_overrun_count=inference_overrun_count,
        )

    detected_store = RoastSessionStore()
    detected_session = detected_store.start_session()
    detected_store.record_event(detected_session, "beans_added")
    detected_store.record_event(detected_session, "first_crack_detected")
    detected = _serialize_first_crack_status(
        detected_session,
        config=config,
        first_crack_runtime=runtime_for(detected_session.id, status="detected"),
    )

    session_fault_store = RoastSessionStore()
    session_fault = session_fault_store.start_session()
    session_fault_store.record_event(session_fault, "fault", payload={"reason": "test"})
    faulted_before_fc = _serialize_first_crack_status(
        session_fault,
        config=config,
        first_crack_runtime=runtime_for(session_fault.id, status="pending"),
    )

    runtime_fault_store = RoastSessionStore()
    runtime_fault = runtime_fault_store.start_session()
    runtime_faulted = _serialize_first_crack_status(
        runtime_fault,
        config=config,
        first_crack_runtime=runtime_for(runtime_fault.id, status=runtime_status),
    )

    pending_store = RoastSessionStore()
    pending_session = pending_store.start_session()
    pending = _serialize_first_crack_status(
        pending_session,
        config=config,
        first_crack_runtime=runtime_for(pending_session.id, status="pending"),
    )

    for status in (detected, faulted_before_fc, runtime_faulted, pending):
        assert status.max_consecutive_overflow_count == max_consecutive_overflow_count
        assert status.last_inference_duration_ms == last_inference_duration_ms
        assert status.max_inference_duration_ms == max_inference_duration_ms
        assert status.inference_overrun_count == inference_overrun_count


def test_driver_command_failure_does_not_mutate_session_state(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(fail_heat=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(RuntimeError, match="heat command failed"):
        _call_tool(server, "set_heat", ctx, heat_level_percent=65)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert driver.actions == ["connect", "set_heat:65"]
    assert state.heat_level_percent == 0
    assert state.fan_level_percent == 0
    assert state.cooling_on is False
    assert state.events == ()


def test_invalid_event_phase_blocks_driver_command(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(SessionLifecycleError, match="roasting, development"):
        _call_tool(server, "drop_beans", ctx)
    with pytest.raises(
        SessionLifecycleError, match="Cooling can only start after beans are dropped"
    ):
        _call_tool(server, "start_cooling", ctx)
    with pytest.raises(SessionLifecycleError, match="Cooling cannot stop before beans are dropped"):
        _call_tool(server, "stop_cooling", ctx)

    assert driver.actions == ["connect"]


def test_driver_connect_failure_prevents_session_creation(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    object.__setattr__(server_context, "roaster_driver", RecordingRoasterDriver(fail_connect=True))
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    with pytest.raises(RuntimeError, match="connect failed"):
        _call_tool(server, "start_roast_session", ctx)
    with pytest.raises(ValueError, match="No roast session exists"):
        _call_tool(server, "get_roast_state", ctx)


def test_concurrent_session_start_reserves_before_driver_connect(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    connect_started = Event()
    release_connect = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_connect=(connect_started, release_connect))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    results: list[object] = []
    errors: list[BaseException] = []

    start_thread = Thread(
        target=_record_tool_result,
        args=(results, errors, server, "start_roast_session", ctx),
    )
    start_thread.start()
    assert connect_started.wait(timeout=1.0)

    with pytest.raises(SessionLifecycleError, match="start is already in progress"):
        _call_tool(server, "start_roast_session", ctx)

    release_connect.set()
    start_thread.join(timeout=1.0)

    assert not start_thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert driver.actions == ["connect"]


def test_stale_heat_command_fails_closed_after_emergency_stop(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_heat=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    _call_tool(server, "start_roast_session", ctx)
    heat_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "set_heat", ctx),
        kwargs={"heat_level_percent": 65},
    )
    heat_thread.start()
    assert command_started.wait(timeout=1.0)

    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    release_command.set()
    heat_thread.join(timeout=1.0)

    assert not heat_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    assert emergency.event.kind == "fault"
    assert driver.heat_level_percent == 0
    assert driver.fan_level_percent == 100
    assert driver.cooling_on is True
    assert driver.actions == [
        "connect",
        "set_heat:65",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_pending_post_fault_cooling_blocks_newer_active_session(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_heat=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    first_start = _call_tool(server, "start_roast_session", ctx)
    heat_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "set_heat", ctx),
        kwargs={"heat_level_percent": 65},
    )
    heat_thread.start()
    assert command_started.wait(timeout=1.0)

    _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    with pytest.raises(SessionLifecycleError, match="post-fault cooling recovery"):
        _call_tool(server, "start_roast_session", ctx)
    release_command.set()
    heat_thread.join(timeout=1.0)

    assert not heat_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=first_start.session.session_id,
    )
    assert state.active is False
    assert state.phase == "fault"
    assert state.cooling_on is True
    assert driver.actions == [
        "connect",
        "set_heat:65",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_blocked_drop_command_does_not_block_emergency_stop(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_drop=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    drop_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "drop_beans", ctx),
    )
    drop_thread.start()
    assert command_started.wait(timeout=1.0)

    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    release_command.set()
    drop_thread.join(timeout=1.0)

    assert not drop_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    assert emergency.event.kind == "fault"
    assert driver.actions == [
        "connect",
        "drop_beans",
        "emergency_stop:unit-test",
        "emergency_stop:stale driver command after session state changed",
    ]


class _QueuedWindowAudioPipeline:
    """Fake audio pipeline exposing detector windows queued after construction.

    Models the real race behind coffee-roaster-mcp#191: a first-crack-confirming
    window was already captured but the poll cadence has not drained it yet.
    Appends to a SHARED call-order log so a test can prove drain_windows runs
    strictly after the driver's drop_beans call, never before it.
    """

    def __init__(self, *, call_order: list[str]) -> None:
        self._windows: list[AudioWindow] = []
        self._call_order = call_order
        self.stopped = False

    def queue_window(self, window: AudioWindow) -> None:
        self._windows.append(window)

    def start(self) -> AudioCaptureSnapshot:
        return self.snapshot()

    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        self.stopped = True
        self._call_order.append("pipeline.stop")
        return self.snapshot()

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        self._call_order.append("pipeline.drain_windows")
        drained = tuple(self._windows)
        self._windows.clear()
        return drained

    def discard_pending_audio(self, *, timeout_seconds: float = 1.0) -> None:
        # This fake models the whole pipeline as one queued-window list (no
        # separate reader-backlog/sample-buffer stage), so discarding is
        # equivalent to draining everything (coffee-roaster-mcp#195).
        self._call_order.append("pipeline.discard_pending_audio")
        self._windows.clear()

    def snapshot(self) -> AudioCaptureSnapshot:
        return AudioCaptureSnapshot(
            running=not self.stopped,
            queued_window_count=len(self._windows),
            emitted_window_count=1,
            dropped_window_count=0,
            latest_error=None,
            peak_dbfs=None,
            rms_dbfs=None,
        )

    @property
    def shutdown_confirmed(self) -> bool:
        return self.stopped


class _OneShotDetectorBackend:
    """Backend double confirming first crack on the first window it sees."""

    def __init__(self, *, call_order: list[str]) -> None:
        self._call_order = call_order

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:  # noqa: ARG002
        self._call_order.append("detector.detect")
        return FirstCrackDetectorOutput(confirmed=True, confidence=0.97)


def _resolved_detector_artifacts_for_test() -> ResolvedDetectorArtifacts:
    return ResolvedDetectorArtifacts(
        onnx_model=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="v0.1.0",
            filename="onnx/int8/model_quantized.onnx",
            local_path=Path("/tmp/model_quantized.onnx"),
        ),
        feature_extractor_config=ResolvedArtifact(
            repo_id="syamaner/coffee-first-crack-detection",
            revision="v0.1.0",
            filename="onnx/int8/preprocessor_config.json",
            local_path=Path("/tmp/preprocessor_config.json"),
        ),
    )


def test_drop_beans_never_delays_drop_and_never_writes_a_session_fc_event(
    tmp_path: Path,
) -> None:
    """Regression (coffee-roaster-mcp#191), option 1 per the safety review.

    Two properties, proved together against ONE shared call-order log:

    1. The driver's `drop_beans()` call happens BEFORE any detector inference
       runs — a confirming window queued pre-drop must never delay the
       hardware drop command itself (the naive pre-drop-drain fix traded a
       lost milestone for a delayed drop; that trade is rejected).
    2. The session event log stays untouched: NO first_crack_detected event
       is ever recorded for a window classified after the drop, by design —
       recovery happens only in the recording sidecar's milestone (see
       test_first_crack_runtime.py for that half, driven through the real
       recorder), never on the causal, phase-ordered control timeline.
    """
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)

    call_order: list[str] = []
    driver = RecordingRoasterDriver()
    driver.actions = call_order  # share the SAME list the pipeline/detector append to
    object.__setattr__(server_context, "roaster_driver", driver)

    pipeline = _QueuedWindowAudioPipeline(call_order=call_order)
    backend = _OneShotDetectorBackend(call_order=call_order)
    real_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts_for_test(),
            backend,
        ),
    )
    object.__setattr__(server_context, "first_crack_runtime", real_runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    call_order.clear()  # only the drop_beans call itself matters from here

    # Queue the confirming window AFTER beans_added (so it postdates session
    # start) and with a short duration ending comfortably before the drop
    # fires moments below — a genuinely complete pre-drop window that was
    # simply sitting undrained. No get_roast_state poll runs here: the poll
    # cadence has not drained it, matching the real #191 race.
    # A generous 1s safety margin absorbs real wall-clock overhead in the
    # drop_beans call itself (session-store locking, event validation, the
    # mock driver call) between "now" and when beans_dropped is actually
    # recorded, so the window's END is unambiguously before the drop cutoff.
    pipeline.queue_window(
        AudioWindow(
            sequence_number=1,
            input_device="fake-mic",
            sample_rate=16_000,
            started_at_monotonic_seconds=time.monotonic() - 1.0,
            duration_seconds=0.001,
            samples=(0.0,) * 16_000,
        )
    )
    drop_result = _call_tool(server, "drop_beans", ctx)

    # Property 1: the driver's drop_beans() ran BEFORE any inference/drain.
    assert "drop_beans" in call_order
    assert "detector.detect" in call_order
    assert call_order.index("drop_beans") < call_order.index("detector.detect")
    assert call_order.index("drop_beans") < call_order.index("pipeline.drain_windows")

    # Property 2: the session event log stays untouched — no post-drop
    # first_crack_detected, regardless of how confidently the classifier
    # confirmed the pre-drop window.
    state = _call_tool(server, "get_roast_state", ctx, session_id=drop_result.session_id)
    assert [event.kind for event in state.events] == [
        "beans_added",
        "beans_dropped",
        "cooling_started",
    ]
    assert state.first_crack_at_utc is None


def test_drop_beans_ignores_a_window_that_straddles_the_drop(tmp_path: Path) -> None:
    """A window whose capture END is at/after the drop is rejected, even
    though its START predates it (coffee-roaster-mcp#191): the drop itself is
    acoustically crack-like (cascading beans), so a straddling window's tail
    could contain drop clatter — a phantom milestone would poison the
    annotation dataset. Only a window that finished capturing strictly before
    the drop is eligible for the post-drop recovery.
    """
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)

    call_order: list[str] = []
    pipeline = _QueuedWindowAudioPipeline(call_order=call_order)
    backend = _OneShotDetectorBackend(call_order=call_order)
    real_runtime = FirstCrackSessionRuntime(
        config=AppConfig(first_crack=FirstCrackConfig(mode="audio", revision="v0.1.0")),
        audio_pipeline_factory=lambda _: pipeline,
        detector_adapter_factory=lambda config: build_first_crack_detector_adapter(
            config,
            _resolved_detector_artifacts_for_test(),
            backend,
        ),
    )
    object.__setattr__(server_context, "first_crack_runtime", real_runtime)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)

    # Queue a window whose START is now (before the drop call below), but
    # whose 10s duration carries its END well past the drop — a straddler.
    pipeline.queue_window(
        AudioWindow(
            sequence_number=1,
            input_device="fake-mic",
            sample_rate=16_000,
            started_at_monotonic_seconds=time.monotonic(),
            duration_seconds=10.0,
            samples=(0.0,) * 16_000,
        )
    )
    drop_result = _call_tool(server, "drop_beans", ctx)

    assert "detector.detect" not in call_order
    state = _call_tool(server, "get_roast_state", ctx, session_id=drop_result.session_id)
    assert "first_crack_detected" not in [event.kind for event in state.events]
    assert state.first_crack_at_utc is None


def test_stop_cooling_uses_driver_cooling_state_before_completing(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_stays_on=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "drop_beans", ctx)

    with pytest.raises(SessionLifecycleError, match="still reports cooling active"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.active is True
    assert state.phase == "cooling"
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == [
        "beans_added",
        "beans_dropped",
        "cooling_started",
    ]
    assert driver.actions == [
        "connect",
        "drop_beans",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_recovers_after_emergency_stop_leaves_cooling_on(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    emergency = _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    assert emergency.phase == "fault"
    emergency_state = _call_tool(
        server,
        "get_roast_state",
        ctx,
        session_id=start_result.session.session_id,
    )
    assert emergency_state.active is False
    assert emergency_state.cooling_on is True

    recovered = _call_tool(server, "stop_cooling", ctx)

    assert recovered.session_id == start_result.session.session_id
    assert recovered.event.kind == "cooling_stopped"
    assert recovered.event.payload["recovery_after_fault"] is True
    assert recovered.phase == "fault"
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.cooling_on is False
    assert [event.kind for event in state.events] == ["fault", "cooling_stopped"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
    ]


def test_stop_cooling_recovery_keeps_fault_when_driver_reports_cooling_on(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_stays_on=True)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")

    with pytest.raises(SessionLifecycleError, match="still reports cooling active"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.active is False
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == ["fault"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_recovery_rejects_driver_heat_after_fault(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(stop_cooling_heat_level_percent=20)
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")

    with pytest.raises(SessionLifecycleError, match="Heat must be off"):
        _call_tool(server, "stop_cooling", ctx)

    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.active is False
    assert state.heat_level_percent == 0
    assert state.cooling_on is True
    assert [event.kind for event in state.events] == ["fault"]
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stale_stop_cooling_recovery_fails_closed(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    command_started = Event()
    release_command = Event()
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver(block_stop_cooling=(command_started, release_command))
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)
    errors: list[BaseException] = []

    start_result = _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "emergency_stop", ctx, reason="unit-test")
    stop_thread = Thread(
        target=_record_tool_error,
        args=(errors, server, "stop_cooling", ctx),
    )
    stop_thread.start()
    assert command_started.wait(timeout=1.0)

    latest_session = server_context.session_store.get_latest_session()
    assert latest_session is not None
    server_context.session_store.cancel_pending_driver_command(latest_session)
    release_command.set()
    stop_thread.join(timeout=1.0)

    assert not stop_thread.is_alive()
    assert isinstance(errors[0], SessionLifecycleError)
    state = _call_tool(server, "get_roast_state", ctx, session_id=start_result.session.session_id)
    assert state.phase == "fault"
    assert state.cooling_on is True
    assert driver.actions == [
        "connect",
        "emergency_stop:unit-test",
        "stop_cooling",
        "emergency_stop:stale driver command after session state changed",
    ]


def test_stop_cooling_still_rejects_completed_inactive_session(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(f"logging:\n  log_dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    server_context = build_server_context(config_path=config_path)
    driver = RecordingRoasterDriver()
    object.__setattr__(server_context, "roaster_driver", driver)
    server = create_mcp_server(config_path=config_path)
    ctx = _ctx(server_context)

    _call_tool(server, "start_roast_session", ctx)
    _call_tool(server, "mark_beans_added", ctx)
    _call_tool(server, "drop_beans", ctx)
    _call_tool(server, "stop_cooling", ctx)

    with pytest.raises(ValueError, match="No active roast session exists"):
        _call_tool(server, "stop_cooling", ctx)


class RecordingRoasterDriver:
    """Driver double that records MCP boundary calls."""

    name = "recording"

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        fail_heat: bool = False,
        fail_read: bool = False,
        block_connect: tuple[Event, Event] | None = None,
        block_heat: tuple[Event, Event] | None = None,
        block_drop: tuple[Event, Event] | None = None,
        block_stop_cooling: tuple[Event, Event] | None = None,
        stop_cooling_stays_on: bool = False,
        stop_cooling_heat_level_percent: int = 0,
        bean_temp_c: float | None = None,
        env_temp_c: float | None = None,
        raw_vendor_data: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        """Initialize a deterministic recording driver."""
        self.actions: list[str] = []
        self.fail_connect = fail_connect
        self.fail_heat = fail_heat
        self.fail_read = fail_read
        self.block_connect = block_connect
        self.block_heat = block_heat
        self.block_drop = block_drop
        self.block_stop_cooling = block_stop_cooling
        self.stop_cooling_stays_on = stop_cooling_stays_on
        self.stop_cooling_heat_level_percent = stop_cooling_heat_level_percent
        self.connected = False
        self.heat_level_percent = 0
        self.fan_level_percent = 0
        self.cooling_on = False
        self.bean_temp_c = bean_temp_c
        self.env_temp_c = env_temp_c
        self.raw_vendor_data = {} if raw_vendor_data is None else dict(raw_vendor_data)

    @property
    def capabilities(self) -> object:
        """Return mock-compatible capabilities for tests."""
        return MockRoasterDriver().capabilities

    def connect(self) -> None:
        """Record connect calls."""
        self.actions.append("connect")
        if self.block_connect is not None:
            started, release = self.block_connect
            started.set()
            assert release.wait(timeout=1.0)
        if self.fail_connect:
            raise RuntimeError("connect failed")
        self.connected = True

    def disconnect(self) -> None:
        """Record disconnect calls."""
        self.actions.append("disconnect")
        self.connected = False

    def read_state(self) -> RoasterState:
        """Return the current test state."""
        if self.fail_read:
            raise RuntimeError("read failed")
        return self._state()

    def set_heat(self, *, heat_level_percent: int) -> RoasterState:
        """Record heat commands."""
        self.actions.append(f"set_heat:{heat_level_percent}")
        if self.fail_heat:
            raise RuntimeError("heat command failed")
        if self.block_heat is not None:
            started, release = self.block_heat
            started.set()
            assert release.wait(timeout=1.0)
        self.heat_level_percent = heat_level_percent
        return self._state()

    def set_fan(self, *, fan_level_percent: int) -> RoasterState:
        """Record fan commands."""
        self.actions.append(f"set_fan:{fan_level_percent}")
        self.fan_level_percent = fan_level_percent
        return self._state()

    def drop_beans(self) -> RoasterState:
        """Record drop commands and enter cooling."""
        self.actions.append("drop_beans")
        if self.block_drop is not None:
            started, release = self.block_drop
            started.set()
            assert release.wait(timeout=1.0)
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        return self._state()

    def start_cooling(self) -> RoasterState:
        """Record cooling-start commands."""
        self.actions.append("start_cooling")
        self.cooling_on = True
        return self._state()

    def stop_cooling(self) -> RoasterState:
        """Record cooling-stop commands."""
        self.actions.append("stop_cooling")
        if self.block_stop_cooling is not None:
            started, release = self.block_stop_cooling
            started.set()
            assert release.wait(timeout=1.0)
        self.cooling_on = self.stop_cooling_stays_on
        self.heat_level_percent = self.stop_cooling_heat_level_percent
        return self._state()

    def emergency_stop(self, *, reason: str) -> EmergencyStopResult:
        """Record emergency-stop commands."""
        self.actions.append(f"emergency_stop:{reason}")
        self.heat_level_percent = 0
        self.fan_level_percent = 100
        self.cooling_on = True
        return EmergencyStopResult(
            driver=self.name,
            safety_method="emergency_stop",
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
        )

    def _state(self) -> RoasterState:
        return RoasterState(
            driver=self.name,
            connected=self.connected,
            bean_temp_c=self.bean_temp_c,
            env_temp_c=self.env_temp_c,
            heat_level_percent=self.heat_level_percent,
            fan_level_percent=self.fan_level_percent,
            cooling_on=self.cooling_on,
            raw_vendor_data=self.raw_vendor_data,
        )


class FakeFirstCrackRuntime:
    """Runtime double that keeps audio-mode MCP tests network-free."""

    def __init__(
        self,
        *,
        status: str = "pending",
        reason: str | None = None,
        record_first_crack_on_process: bool = False,
        audio_running: bool = False,
        queued_window_count: int = 0,
        emitted_window_count: int = 0,
        dropped_window_count: int = 0,
        processed_window_count: int = 0,
        mic_peak_dbfs: float | None = None,
        mic_rms_dbfs: float | None = None,
        overflow_count_last_minute: int = 0,
        estimated_lost_audio_ms_last_minute: float = 0.0,
        total_overflow_count: int = 0,
        max_consecutive_overflow_count: int = 0,
        last_inference_duration_ms: float = 0.0,
        max_inference_duration_ms: float = 0.0,
        inference_overrun_count: int = 0,
    ) -> None:
        self.status = status
        self.reason = reason
        self.record_first_crack_on_process = record_first_crack_on_process
        self.audio_running = audio_running
        self.queued_window_count = queued_window_count
        self.emitted_window_count = emitted_window_count
        self.dropped_window_count = dropped_window_count
        self.processed_window_count = processed_window_count
        self.mic_peak_dbfs = mic_peak_dbfs
        self.mic_rms_dbfs = mic_rms_dbfs
        self.overflow_count_last_minute = overflow_count_last_minute
        self.estimated_lost_audio_ms_last_minute = estimated_lost_audio_ms_last_minute
        self.total_overflow_count = total_overflow_count
        self.max_consecutive_overflow_count = max_consecutive_overflow_count
        self.last_inference_duration_ms = last_inference_duration_ms
        self.max_inference_duration_ms = max_inference_duration_ms
        self.inference_overrun_count = inference_overrun_count
        self.active_session_id: str | None = None
        self.started_sessions: list[str] = []
        self.processed_sessions: list[str] = []
        self.processed_after_drop_sessions: list[bool] = []
        self.stopped_sessions: list[str] = []
        self.discarded_sessions: list[str] = []

    def start_for_session(self, session: RoastSession) -> FirstCrackRuntimeSnapshot:
        self.active_session_id = session.id
        self.started_sessions.append(session.id)
        return self.snapshot()

    def process_available_windows(
        self,
        *,
        session_store: RoastSessionStore,
        session: RoastSession,
    ) -> FirstCrackRuntimeSnapshot:
        self.processed_sessions.append(session.id)
        if (
            self.record_first_crack_on_process
            and session.phase == "roasting"
            and session.first_crack_at_utc is None
        ):
            session_store.record_event_snapshot(session, "first_crack_detected")
            self.status = "detected"
        return self.snapshot()

    def process_pending_windows_after_drop(
        self,
        *,
        session_store: RoastSessionStore,
        session: RoastSession,
    ) -> FirstCrackRuntimeSnapshot:
        """No-op double for the coffee-roaster-mcp#191 post-drop drain.

        Real callers never write a session event here (that's the whole
        point — see FirstCrackSessionRuntime.process_pending_windows_after_drop),
        so this fake simply records the call and returns the current snapshot.
        """
        del session_store, session
        self.processed_after_drop_sessions.append(True)
        return self.snapshot()

    def stop_for_session(self, session_id: str, *, reason: str) -> FirstCrackRuntimeSnapshot:
        self.stopped_sessions.append(session_id)
        self.reason = reason
        return self.snapshot()

    def discard_queued_windows_for_session(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> FirstCrackRuntimeSnapshot:
        self.discarded_sessions.append(session_id)
        self.reason = reason
        return self.snapshot()

    def shutdown(self) -> FirstCrackRuntimeSnapshot:
        return self.snapshot()

    def snapshot(self) -> FirstCrackRuntimeSnapshot:
        return FirstCrackRuntimeSnapshot(
            status=cast(FirstCrackRuntimeState, self.status),
            active_session_id=self.active_session_id,
            active=self.active_session_id is not None,
            reason=self.reason,
            audio_running=self.audio_running,
            queued_window_count=self.queued_window_count,
            emitted_window_count=self.emitted_window_count,
            dropped_window_count=self.dropped_window_count,
            processed_window_count=self.processed_window_count,
            mic_peak_dbfs=self.mic_peak_dbfs,
            mic_rms_dbfs=self.mic_rms_dbfs,
            overflow_count_last_minute=self.overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=self.estimated_lost_audio_ms_last_minute,
            total_overflow_count=self.total_overflow_count,
            max_consecutive_overflow_count=self.max_consecutive_overflow_count,
            last_inference_duration_ms=self.last_inference_duration_ms,
            max_inference_duration_ms=self.max_inference_duration_ms,
            inference_overrun_count=self.inference_overrun_count,
        )


class FakeAmbientRuntime:
    """Runtime double that keeps ambient-mode MCP tests hardware-free (#185)."""

    def __init__(
        self,
        *,
        status: str = "unavailable",
        reason: str | None = None,
        ambient_running: bool = False,
        temperature_c: float | None = None,
        humidity_percent: float | None = None,
        pressure_hpa: float | None = None,
        last_reading_monotonic_seconds: float | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.ambient_running = ambient_running
        self.temperature_c = temperature_c
        self.humidity_percent = humidity_percent
        self.pressure_hpa = pressure_hpa
        self.last_reading_monotonic_seconds = last_reading_monotonic_seconds
        self.active_session_id: str | None = None
        self.started_sessions: list[str] = []
        self.stopped_sessions: list[str] = []

    def start_for_session(self, session: RoastSession) -> AmbientRuntimeSnapshot:
        self.active_session_id = session.id
        self.started_sessions.append(session.id)
        return self.snapshot()

    def poll(self) -> AmbientRuntimeSnapshot:
        return self.snapshot()

    def stop_for_session(self, session_id: str, *, reason: str) -> AmbientRuntimeSnapshot:
        self.stopped_sessions.append(session_id)
        self.reason = reason
        return self.snapshot()

    def shutdown(self) -> AmbientRuntimeSnapshot:
        return self.snapshot()

    def snapshot(self) -> AmbientRuntimeSnapshot:
        return AmbientRuntimeSnapshot(
            status=cast(AmbientRuntimeState, self.status),
            active_session_id=self.active_session_id,
            reason=self.reason,
            ambient_running=self.ambient_running,
            temperature_c=self.temperature_c,
            humidity_percent=self.humidity_percent,
            pressure_hpa=self.pressure_hpa,
            last_reading_monotonic_seconds=self.last_reading_monotonic_seconds,
        )


def _ctx(server_context: ServerContext) -> Any:
    """Build the minimal context shape used by FastMCP tool functions."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server_context))


def _set_first_crack_runtime(
    server_context: ServerContext,
    runtime: FakeFirstCrackRuntime,
) -> None:
    object.__setattr__(server_context, "first_crack_runtime", runtime)


def _set_ambient_runtime(
    server_context: ServerContext,
    runtime: FakeAmbientRuntime,
) -> None:
    object.__setattr__(server_context, "ambient_runtime", runtime)


def _record_tool_error(
    errors: list[BaseException],
    server: FastMCP,
    tool_name: str,
    ctx: Any,
    **kwargs: object,
) -> None:
    """Run one tool in a background thread and record any exception."""
    try:
        _call_tool(server, tool_name, ctx, **kwargs)
    except BaseException as exc:
        errors.append(exc)


def _record_tool_result(
    results: list[object],
    errors: list[BaseException],
    server: FastMCP,
    tool_name: str,
    ctx: Any,
    **kwargs: object,
) -> None:
    """Run one tool in a background thread and record its result or exception."""
    try:
        results.append(_call_tool(server, tool_name, ctx, **kwargs))
    except BaseException as exc:
        errors.append(exc)


def _call_tool(server: FastMCP, tool_name: str, ctx: Any, **kwargs: object) -> Any:
    """Call one registered FastMCP tool function directly."""
    tool_manager = server._tool_manager  # pyright: ignore[reportPrivateUsage]
    tool = tool_manager.get_tool(tool_name)
    assert tool is not None
    return tool.fn(ctx, **kwargs)


def _cold_finalisation_context(tmp_path: Path) -> ServerContext:
    """Build a hardware-free server context for one cold-finalisation test."""
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    return build_server_context(config_path=config_path)


def _record_finalisation_result(
    results: list[object], errors: list[BaseException], context: ServerContext, session_id: str
) -> None:
    """Run finalisation in a test thread and retain its outcome."""
    try:
        results.append(_finalise_cold_characterisation_session(context, session_id))
    except BaseException as exc:  # noqa: BLE001 - test thread reports exact failure.
        errors.append(exc)


class LifecycleRecordingDriver(RecordingRoasterDriver):
    """Recording driver with controllable, read-only lifecycle evidence."""

    def __init__(
        self,
        *,
        non_zero_dimension: str | None = None,
        streaming: bool = False,
        initially_connected: bool = True,
    ) -> None:
        """Initialize a safe-zero evidence double with one optional non-zero dimension."""
        super().__init__()
        self.non_zero_dimension = non_zero_dimension
        self.streaming = streaming
        self.connected = initially_connected

    def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
        """Return a complete, non-actuating evidence snapshot."""
        values: dict[str, int | bool] = {
            "heat_level_percent": 0,
            "roast_fan_level_percent": 0,
            "main_fan_level_percent": 0,
            "drum_motor_on": False,
            "cooling_motor_on": False,
            "solenoid_open": False,
        }
        if self.non_zero_dimension is not None:
            values[self.non_zero_dimension] = (
                1 if self.non_zero_dimension.endswith("percent") else True
            )
        return DriverLifecycleEvidence(
            driver=self.name,
            connected=self.connected,
            command_streaming_required=self.streaming,
            command_loop_running=self.connected if self.streaming else None,
            serial_open=self.connected if self.streaming else None,
            heat_level_percent=cast(int, values["heat_level_percent"]),
            roast_fan_level_percent=cast(int, values["roast_fan_level_percent"]),
            main_fan_level_percent=cast(int, values["main_fan_level_percent"]),
            drum_motor_on=cast(bool, values["drum_motor_on"]),
            cooling_motor_on=cast(bool, values["cooling_motor_on"]),
            solenoid_open=cast(bool, values["solenoid_open"]),
            command_send_attempts=0 if self.streaming else None,
            command_write_count=0 if self.streaming else None,
            last_command_write_size=0 if self.streaming else None,
            command_loop_error_count=0 if self.streaming else None,
            status_packet_count=0 if self.streaming else None,
            status_read_error_count=0 if self.streaming else None,
        )


class BrokenLifecycleDriver:
    """Lifecycle-evidence double for fail-closed admission paths."""

    name = "broken"

    def __init__(self, outcome: str) -> None:
        """Initialize an unreadable or malformed evidence response."""
        self.outcome = outcome

    def read_lifecycle_evidence(self) -> DriverLifecycleEvidence:
        """Raise or return a deliberately invalid evidence result."""
        if self.outcome == "raise":
            raise RuntimeError("evidence unavailable")
        return cast(DriverLifecycleEvidence, object())


class RetryLifecycleDriver(LifecycleRecordingDriver):
    """Lifecycle driver that withholds disconnect confirmation until a retry."""

    def __init__(self) -> None:
        """Initialize with disconnect confirmation deliberately disabled."""
        super().__init__()
        self.confirm_disconnect = False

    def disconnect(self) -> None:
        """Record every disconnect attempt and confirm only when enabled."""
        self.actions.append("disconnect")
        if self.confirm_disconnect:
            self.connected = False


class BlockingDisconnectDriver(LifecycleRecordingDriver):
    """Driver double that pauses after disconnect commit."""

    def __init__(self) -> None:
        """Create commit and release rendezvous events."""
        super().__init__()
        self.disconnect_started = Event()
        self.release_disconnect = Event()

    def disconnect(self) -> None:
        """Hold the lifecycle barrier until the test releases confirmation."""
        self.actions.append("disconnect")
        self.disconnect_started.set()
        assert self.release_disconnect.wait(timeout=1.0)
        self.connected = False


class RetryFinalisationSampler:
    """Sampler double that reports one optional bounded join timeout."""

    def __init__(self, *, alive_on_first: bool = True) -> None:
        """Initialize the deterministic join sequence."""
        self.alive_on_first = alive_on_first
        self.calls = 0

    def stop_and_join_for_finalisation(self, session_id: str) -> SamplerFinalisationEvidence:
        """Return one timeout observation followed by a joined sampler."""
        del session_id
        self.calls += 1
        return SamplerFinalisationEvidence(
            owned_by_session_before_stop=True,
            thread_alive_after_join=self.alive_on_first and self.calls == 1,
            last_error=None,
        )


class RetryFinalisationRuntime(FakeFirstCrackRuntime):
    """Runtime double that fails its first bounded finalise call only."""

    def __init__(self) -> None:
        """Initialize the finalisation attempt counter."""
        super().__init__()
        self.calls = 0

    def finalise_for_session(self, session_id: str) -> tuple[str, str | None, bool]:
        """Report one stop failure followed by a clean stopped state."""
        del session_id
        self.calls += 1
        if self.calls == 1:
            return "stop_failed", "test stop failure", True
        return "not_active", None, False

    def recording_for_session(self, session_id: str) -> tuple[None, None]:
        """Keep recording out of this runtime-stage test."""
        del session_id
        return None, None


class RecordingFailureRuntime(FakeFirstCrackRuntime):
    """Runtime double with one generated recording artifact omitted."""

    def __init__(self, root: Path, failure: str) -> None:
        """Create a recording plan with a selected terminal failure."""
        super().__init__()
        primary, sidecar, annotation = root / "p.wav", root / "r.json", root / "a.json"
        primary.write_bytes(b"x" * 44)
        if failure != "recording_sidecar":
            sidecar.write_text("{}", encoding="utf-8")
        if failure != "annotation_sidecar":
            annotation.write_text("{}", encoding="utf-8")
        self.plan = RecordingArtifactPlan(primary, sidecar, annotation, ())
        self.recorder = SimpleNamespace(
            started_monotonic_seconds=None if failure == "not_started" else 1.0
        )

    def finalise_for_session(self, session_id: str) -> tuple[str, None, bool]:
        """Report a stopped capture without inference."""
        del session_id
        return "not_active", None, False

    def recording_for_session(self, session_id: str) -> tuple[object, RecordingArtifactPlan]:
        """Return the generated plan."""
        del session_id
        return self.recorder, self.plan


class BlockingFinalisationRuntime(FakeFirstCrackRuntime):
    """First-crack runtime double that exposes a deterministic teardown race."""

    def __init__(self) -> None:
        """Initialize stage-two rendezvous events."""
        super().__init__()
        self.finalise_started = Event()
        self.release_finalise = Event()

    def finalise_for_session(self, session_id: str) -> tuple[str, None, bool]:
        """Block after admission until the test applies an emergency stop."""
        assert session_id == self.active_session_id or self.active_session_id is None
        self.finalise_started.set()
        assert self.release_finalise.wait(timeout=1.0)
        return "not_active", None, False

    def recording_for_session(self, session_id: str) -> tuple[None, None]:
        """Return no recording plan for a disabled first-crack configuration."""
        del session_id
        return None, None
