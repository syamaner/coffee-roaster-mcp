# pyright: reportPrivateUsage=false
"""Test-only real stdio server with synthetic capture and out-of-band barriers."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from coffee_roaster_mcp import mcp_server
from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow, RoastRecorder
from coffee_roaster_mcp.config import AppConfig, AudioConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorOutput,
)
from coffee_roaster_mcp.first_crack_observation import Record, SummaryRecord
from coffee_roaster_mcp.first_crack_observer import (
    JsonlObservationSink,
    ObservationModel,
    load_observation_model,
)
from coffee_roaster_mcp.first_crack_runtime import FirstCrackSessionRuntime


def serve(config_path: Path, barriers: Path, blocked_stage: str) -> None:
    def barrier(stage: str) -> None:
        if stage != blocked_stage:
            return
        (barriers / f"{stage}-entered").write_text("entered", encoding="utf-8")
        until = time.monotonic() + 15
        while not (barriers / "release").exists():
            if time.monotonic() > until:
                raise TimeoutError("test barrier not released")
            threading.Event().wait(0.01)

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
