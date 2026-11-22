# pyright: reportPrivateUsage=false
"""Test-only real stdio server with synthetic capture and out-of-band barriers."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from coffee_roaster_mcp import mcp_server
from coffee_roaster_mcp import session as session_module
from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow, RoastRecorder
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorOutput,
)
from coffee_roaster_mcp.drivers import EmergencyStopResult
from coffee_roaster_mcp.first_crack_observation import Record, SummaryRecord
from coffee_roaster_mcp.first_crack_observer import (
    JsonlObservationSink,
    ObservationModel,
    load_observation_model,
)
from coffee_roaster_mcp.first_crack_runtime import FirstCrackSessionRuntime
from coffee_roaster_mcp.mcp_server import ServerContext


def serve(config_path: Path, barriers: Path, blocked_stage: str) -> None:
    if blocked_stage == "recovery-drop-fsync-enospc":
        original_write = session_module.os.write
        original_fsync = session_module.os.fsync
        original_drop_beans = mcp_server.MockRoasterDriver.drop_beans
        original_mock_emergency_stop = mcp_server.MockRoasterDriver.emergency_stop
        recovery_cooling_descriptor: int | None = None

        def record_recovery_drop_write(descriptor: int, frame: bytes) -> int:
            nonlocal recovery_cooling_descriptor
            count = original_write(descriptor, frame)
            if b'"kind":"cooling_started"' in frame:
                recovery_cooling_descriptor = descriptor
            return count

        def fail_recovery_drop_sync(descriptor: int) -> None:
            if descriptor == recovery_cooling_descriptor:
                raise OSError(28, "synthetic no space during derived cooling sync")
            original_fsync(descriptor)

        def count_recovery_drop(
            self: mcp_server.MockRoasterDriver,
            *,
            exact_drop: mcp_server.ExactDropArbitration | None = None,
        ) -> mcp_server.RoasterState:
            with (barriers / "recovery-drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("drop\n")
            return original_drop_beans(self, exact_drop=exact_drop)

        def count_emergency_stop(
            self: mcp_server.MockRoasterDriver, *, reason: str
        ) -> EmergencyStopResult:
            with (barriers / "recovery-drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("emergency\n")
            return original_mock_emergency_stop(self, reason=reason)

        session_module.os.write = record_recovery_drop_write
        session_module.os.fsync = fail_recovery_drop_sync
        mcp_server.MockRoasterDriver.drop_beans = count_recovery_drop
        mcp_server.MockRoasterDriver.emergency_stop = count_emergency_stop

    if blocked_stage in {"drop-capacity-one", "drop-capacity-discharge"}:
        # The ordinary manual charge consumes the sole event-lane slot.  A
        # returned Drop therefore cannot admit its coupled Drop/cooling rows.
        session_module._CANONICAL_EVENT_ROW_LIMIT = 1
        original_drop_beans = mcp_server.MockRoasterDriver.drop_beans

        def count_drop(
            self: mcp_server.MockRoasterDriver,
            *,
            exact_drop: mcp_server.ExactDropArbitration | None = None,
        ) -> mcp_server.RoasterState:
            with (barriers / "drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("drop\n")
            return original_drop_beans(self, exact_drop=exact_drop)

        mcp_server.MockRoasterDriver.drop_beans = count_drop
        original_set_heat = mcp_server.MockRoasterDriver.set_heat
        original_set_fan = mcp_server.MockRoasterDriver.set_fan

        def count_heat(
            self: mcp_server.MockRoasterDriver, *, heat_level_percent: int
        ) -> mcp_server.RoasterState:
            with (barriers / "drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("heat\n")
            return original_set_heat(self, heat_level_percent=heat_level_percent)

        def count_fan(
            self: mcp_server.MockRoasterDriver, *, fan_level_percent: int
        ) -> mcp_server.RoasterState:
            with (barriers / "drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("fan\n")
            return original_set_fan(self, fan_level_percent=fan_level_percent)

        mcp_server.MockRoasterDriver.set_heat = count_heat
        mcp_server.MockRoasterDriver.set_fan = count_fan

        if blocked_stage == "drop-capacity-discharge":
            original_capacity_emergency_stop = mcp_server.MockRoasterDriver.emergency_stop
            original_stop_cooling = mcp_server.MockRoasterDriver.stop_cooling

            def count_emergency_stop(
                self: mcp_server.MockRoasterDriver, *, reason: str
            ) -> EmergencyStopResult:
                with (barriers / "drop-driver-calls").open("a", encoding="utf-8") as handle:
                    handle.write("emergency\n")
                return original_capacity_emergency_stop(self, reason=reason)

            def count_stop_cooling(
                self: mcp_server.MockRoasterDriver,
            ) -> mcp_server.RoasterState:
                with (barriers / "drop-driver-calls").open("a", encoding="utf-8") as handle:
                    handle.write("stop_cooling\n")
                return original_stop_cooling(self)

            mcp_server.MockRoasterDriver.emergency_stop = count_emergency_stop
            mcp_server.MockRoasterDriver.stop_cooling = count_stop_cooling

    def barrier(stage: str) -> None:
        expected_stage = blocked_stage
        if blocked_stage == "first-crack-fault-fail":
            expected_stage = "first-crack"
        if stage != expected_stage:
            return
        (barriers / f"{stage}-entered").write_text("entered", encoding="utf-8")
        until = time.monotonic() + 15
        while not (barriers / "release").exists():
            if time.monotonic() > until:
                raise TimeoutError("test barrier not released")
            threading.Event().wait(0.01)

    if blocked_stage in {
        "charge",
        "first-crack",
        "fault",
        "first-crack-fault-fail",
        "recovery-drop",
        "exact-drop",
    }:
        original_append_verified = session_module._CanonicalGenerationWriter._append_verified
        held_kind = {
            "charge": b'"kind":"beans_added"',
            "first-crack": b'"kind":"first_crack_detected"',
            "fault": b'"kind":"fault"',
            "first-crack-fault-fail": b'"kind":"first_crack_detected"',
            "recovery-drop": b'"kind":"beans_dropped"',
            "exact-drop": b'"kind":"beans_dropped"',
        }[blocked_stage]

        def hold_canonical_row(
            self: session_module._CanonicalGenerationWriter, frame: bytes
        ) -> None:
            if held_kind in frame:
                stage = blocked_stage
                if blocked_stage == "first-crack-fault-fail":
                    stage = "first-crack"
                barrier(stage)
            if blocked_stage == "first-crack-fault-fail" and b'"kind":"fault"' in frame:
                raise OSError("synthetic queued fault append failure")
            original_append_verified(self, frame)

        original_emergency_dispatch = mcp_server.run_driver_emergency_stop

        def record_emergency_dispatch(
            server_context: ServerContext, *, reason: str
        ) -> dict[str, session_module.EventPayloadValue]:
            result = original_emergency_dispatch(server_context, reason=reason)
            (barriers / "emergency-entered").write_text("entered", encoding="utf-8")
            return result

        session_module._CanonicalGenerationWriter._append_verified = hold_canonical_row
        mcp_server.run_driver_emergency_stop = record_emergency_dispatch

        if blocked_stage == "recovery-drop":
            original_drop_beans = mcp_server.MockRoasterDriver.drop_beans

            def count_recovery_drop(
                self: mcp_server.MockRoasterDriver,
                *,
                exact_drop: mcp_server.ExactDropArbitration | None = None,
            ) -> mcp_server.RoasterState:
                with (barriers / "recovery-drop-driver-calls").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write("drop\n")
                return original_drop_beans(self, exact_drop=exact_drop)

            mcp_server.MockRoasterDriver.drop_beans = count_recovery_drop

        if blocked_stage == "exact-drop":
            original_drop_beans = mcp_server.MockRoasterDriver.drop_beans
            original_mock_emergency_stop = mcp_server.MockRoasterDriver.emergency_stop

            def count_exact_drop(
                self: mcp_server.MockRoasterDriver,
                *,
                exact_drop: mcp_server.ExactDropArbitration | None = None,
            ) -> mcp_server.RoasterState:
                with (barriers / "exact-drop-driver-calls").open("a", encoding="utf-8") as handle:
                    handle.write("drop\n")
                return original_drop_beans(self, exact_drop=exact_drop)

            def count_exact_emergency(
                self: mcp_server.MockRoasterDriver, *, reason: str
            ) -> EmergencyStopResult:
                with (barriers / "exact-drop-driver-calls").open("a", encoding="utf-8") as handle:
                    handle.write("emergency\n")
                return original_mock_emergency_stop(self, reason=reason)

            mcp_server.MockRoasterDriver.drop_beans = count_exact_drop
            mcp_server.MockRoasterDriver.emergency_stop = count_exact_emergency

    if blocked_stage == "ordinary-drop-driver":
        original_drop_beans = mcp_server.MockRoasterDriver.drop_beans
        drop_calls = 0

        def hold_first_ordinary_drop(
            self: mcp_server.MockRoasterDriver,
            *,
            exact_drop: mcp_server.ExactDropArbitration | None = None,
        ) -> mcp_server.RoasterState:
            nonlocal drop_calls
            drop_calls += 1
            with (barriers / "ordinary-drop-driver-calls").open("a", encoding="utf-8") as handle:
                handle.write("drop\n")
            if drop_calls == 1:
                (barriers / "ordinary-drop-driver-entered").write_text("entered", encoding="utf-8")
                until = time.monotonic() + 15
                while not (barriers / "release").exists():
                    if time.monotonic() > until:
                        raise TimeoutError("test barrier not released")
                    threading.Event().wait(0.01)
            return original_drop_beans(self, exact_drop=exact_drop)

        mcp_server.MockRoasterDriver.drop_beans = hold_first_ordinary_drop

    if blocked_stage == "lost-ordinary-return":
        original_drop_beans = mcp_server.MockRoasterDriver.drop_beans
        original_set_heat = mcp_server.MockRoasterDriver.set_heat
        original_set_fan = mcp_server.MockRoasterDriver.set_fan

        def count_heat(
            self: mcp_server.MockRoasterDriver, *, heat_level_percent: int
        ) -> mcp_server.RoasterState:
            with (barriers / "lost-ordinary-return-driver-calls").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("heat\n")
            return original_set_heat(self, heat_level_percent=heat_level_percent)

        def count_fan(
            self: mcp_server.MockRoasterDriver, *, fan_level_percent: int
        ) -> mcp_server.RoasterState:
            with (barriers / "lost-ordinary-return-driver-calls").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("fan\n")
            return original_set_fan(self, fan_level_percent=fan_level_percent)

        def lose_ordinary_return(
            self: mcp_server.MockRoasterDriver,
            *,
            exact_drop: mcp_server.ExactDropArbitration | None = None,
        ) -> mcp_server.RoasterState:
            entry = "exact" if exact_drop is not None else "ordinary"
            with (barriers / "lost-ordinary-return-driver-calls").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(f"{entry}\n")
            result = original_drop_beans(self, exact_drop=exact_drop)
            if exact_drop is None:
                raise OSError("synthetic lost ordinary Drop return")
            return result

        mcp_server.MockRoasterDriver.drop_beans = lose_ordinary_return
        mcp_server.MockRoasterDriver.set_heat = count_heat
        mcp_server.MockRoasterDriver.set_fan = count_fan

    class Capture:
        def __init__(self, recorder: RoastRecorder | None) -> None:
            self.recorder = recorder
            self.stopped = False
            self.emitted = False

        def start(self) -> AudioCaptureSnapshot:
            with (barriers / "capture-starts").open("a", encoding="utf-8") as handle:
                handle.write("start\n")
            if self.recorder is not None:
                self.recorder.begin()
                self.recorder.write_samples((0.0,) * 160)
            return self.snapshot()

        def snapshot(self) -> AudioCaptureSnapshot:
            if (barriers / "projection-counters").exists():
                return AudioCaptureSnapshot(
                    not self.stopped,
                    3,
                    13,
                    4,
                    None,
                    peak_dbfs=-3,
                    rms_dbfs=-8,
                    overflow_count_last_minute=2,
                    estimated_lost_audio_ms_last_minute=150,
                    total_overflow_count=9,
                    max_consecutive_overflow_count=2,
                )
            return AudioCaptureSnapshot(not self.stopped, 0, int(self.emitted), 0, None)

        def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
            if not self.stopped and self.recorder is not None:
                self.recorder.write_samples((0.0,) * 160)
                self.recorder.close()
            self.stopped = True
            (barriers / "capture-stopped").write_text("stopped", encoding="utf-8")
            return self.snapshot()

        def discard_pending_audio(self, *, timeout_seconds: float = 1.0) -> None:
            return

        def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
            if self.emitted or not (barriers / "charge-ready").exists():
                return ()
            self.emitted = True
            if self.recorder is not None:
                self.recorder.write_samples((0.0,) * 160)
            return (AudioWindow(1, None, 16000, time.monotonic(), 0.01, (0.0,) * 160),)

        @property
        def shutdown_confirmed(self) -> bool:
            return self.stopped

    class Backend:
        def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
            barrier("backend")
            return FirstCrackDetectorOutput(confirmed=True, confidence=0.95)

    class Sink(JsonlObservationSink):
        def append(self, record: Record) -> None:
            barrier("sink")
            super().append(record)

        def finalise(self, summary: SummaryRecord) -> None:
            super().finalise(summary)
            (barriers / "summary-persisted").write_text("persisted", encoding="utf-8")

    def loader(config: FirstCrackConfig) -> ObservationModel:
        with (barriers / "loader-starts").open("a", encoding="utf-8") as handle:
            handle.write("load\n")
        barrier("loader")
        model = barriers / "synthetic.onnx"
        model.write_bytes(b"synthetic")
        prep = barriers / "synthetic.json"
        prep.write_text("{}", encoding="utf-8")
        artifacts = ResolvedDetectorArtifacts(
            ResolvedArtifact(
                config.repo_id, config.revision, "onnx/int8/model_quantized.onnx", model
            ),
            ResolvedArtifact(
                config.repo_id, config.revision, "onnx/int8/preprocessor_config.json", prep
            ),
        )
        loaded = load_observation_model(
            config,
            resolver=lambda _: artifacts,
            backend_factory=lambda _config, _artifacts: Backend(),
        )

        (barriers / "loader-returned").write_text("returned", encoding="utf-8")
        return loaded

    def runtime_factory(
        config: AppConfig, *, session_is_retained: Callable[[str], bool] | None = None
    ) -> FirstCrackSessionRuntime:
        runtime: FirstCrackSessionRuntime

        def capture_factory(_: AudioConfig) -> Capture:
            return Capture(runtime._pending_recorder)

        runtime = FirstCrackSessionRuntime(
            config=config,
            audio_pipeline_factory=capture_factory,
            observation_model_loader=loader,
            observation_sink_factory=Sink,
            session_is_retained=session_is_retained,
        )
        return runtime

    assert hasattr(mcp_server, "build_first_crack_session_runtime"), (
        "Synthetic runtime builder seam is missing; refusing production composition."
    )
    mcp_server.build_first_crack_session_runtime = runtime_factory
    mcp_server.create_mcp_server(config_path=config_path).run(transport="stdio")


if __name__ == "__main__":
    serve(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
