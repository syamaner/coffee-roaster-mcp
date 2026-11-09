"""Bounded observation infrastructure; never owns canonical state or a driver."""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from coffee_roaster_mcp.artifacts import (
    ResolvedDetectorArtifacts,
    resolve_first_crack_detector_artifacts,
)
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow, RoastRecorder
from coffee_roaster_mcp.config import AppConfig, FirstCrackConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorAdapter,
    FirstCrackDetectorBackend,
    FirstCrackWindowObservation,
    build_first_crack_detector_adapter,
    build_released_onnx_first_crack_detector_backend,
)
from coffee_roaster_mcp.first_crack_observation import (
    ClockMapping,
    ConfirmationPayload,
    ConfirmationRecord,
    Counts,
    HumanPayload,
    HumanRecord,
    IdentityPayload,
    IdentityRecord,
    ModelIdentity,
    ObservationSnapshot,
    ObserverState,
    ReceiptPayload,
    ReceiptRecord,
    Record,
    Settings,
    SummaryPayload,
    SummaryRecord,
    Time,
    WindowPayload,
    WindowRecord,
    absent,
    present,
)


class ObservationSink(Protocol):
    """A successful append includes flush/fsync; no canonical lock is supplied."""

    def append(self, record: Record) -> None: ...
    def finalise(self, summary: SummaryRecord) -> None: ...


class JsonlObservationSink:
    """Independent diagnostic file; only the observation worker writes it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, record: Record) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(record.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def finalise(self, summary: SummaryRecord) -> None:
        self.append(summary)


class ObservationCapture(Protocol):
    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]: ...
    def snapshot(self) -> AudioCaptureSnapshot: ...
    def stop(self, *, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot: ...
    @property
    def shutdown_confirmed(self) -> bool: ...


class _Owner(Protocol):
    reaped: threading.Event

    def check_deadline(self) -> None: ...
    def supervision_failed(self) -> None: ...


class _Coordinator:
    """One lifetime observation slot and one I/O-free deadline supervisor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: _Owner | None = None
        self._reservation: object | None = None
        self.late_results_after_freeze = 0
        self.wake = threading.Event()
        threading.Thread(
            target=self._supervise, name="fc-observation-supervisor", daemon=True
        ).start()

    def note_late_result(self) -> None:
        with self._lock:
            self.late_results_after_freeze += 1

    def reserve(self, token: object) -> bool:
        with self._lock:
            if self._reservation is not None or (
                self._owner is not None and not self._owner.reaped.is_set()
            ):
                return False
            self._reservation = token
            return True

    def release(self, token: object) -> None:
        with self._lock:
            if self._reservation is token:
                self._reservation = None

    def claim(self, owner: _Owner, token: object | None = None) -> bool:
        with self._lock:
            if self._owner is not None and not self._owner.reaped.is_set():
                return False
            if self._reservation is not None and self._reservation is not token:
                return False
            self._reservation = None
            self._owner = owner
            return True

    def handoff_cleanup(self, cleanup: _Owner, token: object, previous: _Owner | None) -> bool:
        with self._lock:
            reservation_owned = self._reservation is token and (
                self._owner is None or self._owner.reaped.is_set()
            )
            failed_start_owned = self._owner is previous and previous is not None
            if not reservation_owned and not failed_start_owned:
                return False
            self._reservation = None
            self._owner = cleanup
            return True

    def _supervise(self) -> None:
        while True:
            self.wake.wait(0.05)
            self.wake.clear()
            with self._lock:
                owner = self._owner
            if owner is not None:
                try:
                    owner.check_deadline()
                except Exception:  # noqa: BLE001 - one generation cannot kill supervisor.
                    # No supervisor I/O; even broken failure reporting cannot
                    # kill the shared supervisor. The owner stays unreaped.
                    with suppress(Exception):
                        owner.supervision_failed()


_coordinator_instance: _Coordinator | None = None
_COORDINATOR_LOCK = threading.Lock()


def coordinator() -> _Coordinator:
    """Lazily construct the process-wide supervisor only for the new opt-in."""
    global _coordinator_instance
    with _COORDINATOR_LOCK:
        if _coordinator_instance is None:
            _coordinator_instance = _Coordinator()
        return _coordinator_instance


class CaptureCleanup:
    """The same reserved worker slot owns cleanup after partial setup failure."""

    def __init__(
        self, pipeline: ObservationCapture | None, recorder: RoastRecorder | None, timeout: float
    ) -> None:
        self.pipeline = pipeline
        self.recorder = recorder
        self.timeout = timeout
        self.reaped = threading.Event()
        self.finished = threading.Event()

    def check_deadline(self) -> None:
        # No I/O or capture call on the shared supervisor. An unreleased
        # resource keeps the slot owned even after a failed cleanup attempt.
        return

    def supervision_failed(self) -> None:
        # Cleanup is already non-clean until release; retain that ownership.
        return

    def start(self, token: object, previous: _Owner | None) -> None:
        if not coordinator().handoff_cleanup(self, token, previous):
            raise RuntimeError("Cleanup does not own the capture reservation.")
        threading.Thread(target=self._run, name="fc-observation-cleanup", daemon=True).start()

    def _run(self) -> None:
        released = False
        try:
            if self.pipeline is not None:
                self.pipeline.stop(timeout_seconds=self.timeout)
            if self.recorder is not None:
                self.recorder.close()
            # Keep this same owned worker until normal stop/close has actually
            # released both resources. Never retry either operation or ask the
            # supervisor/request paths to query capture lifecycle state.
            while not (
                (self.pipeline is None or self.pipeline.shutdown_confirmed)
                and (self.recorder is None or self.recorder.shutdown_confirmed)
            ):
                self.finished.wait(0.05)
            released = True
        except Exception:  # noqa: BLE001 - retain ownership on cleanup failure.
            released = False
        finally:
            self.finished.set()
            if released:
                self.reaped.set()


def configured_identity(config: FirstCrackConfig) -> ModelIdentity:
    """Configured provenance remains available even when the loader hangs."""
    prefix = "onnx/int8" if config.precision == "int8" else "onnx/fp32"
    model_filename = "model_quantized.onnx" if config.precision == "int8" else "model.onnx"
    return ModelIdentity(
        status="pending",
        repo_id=config.repo_id,
        revision=absent("hub_default_revision")
        if config.revision is None
        else present(config.revision),
        precision=config.precision,
        onnx_model_filename=f"{prefix}/{model_filename}",
        feature_extractor_filename=f"{prefix}/preprocessor_config.json",
        onnx_model_sha256=absent("load_not_completed"),
        feature_extractor_config_sha256=absent("load_not_completed"),
        failure=absent("not_applicable"),
    )


def observation_settings(config: AppConfig) -> Settings:
    return Settings(
        confidence_threshold=config.first_crack.confidence_threshold,
        min_positive_windows=config.first_crack.min_positive_windows,
        confirmation_window_seconds=config.first_crack.confirmation_window_seconds,
        window_seconds=config.audio.window_seconds,
        effective_hop_seconds=config.audio.hop_seconds
        if config.audio.hop_seconds is not None
        else config.audio.window_seconds * (1 - config.audio.overlap),
        sample_rate=config.audio.sample_rate,
    )


@dataclass(frozen=True)
class ObservationModel:
    adapter: FirstCrackDetectorAdapter
    identity: ModelIdentity


class ObservationModelLoadError(RuntimeError):
    def __init__(self, identity: ModelIdentity) -> None:
        super().__init__("Observation model unavailable.")
        self.identity = identity


def hash_artifact(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_observation_model(
    config: FirstCrackConfig,
    *,
    resolver: Callable[
        [FirstCrackConfig], ResolvedDetectorArtifacts
    ] = resolve_first_crack_detector_artifacts,
    backend_factory: Callable[
        [FirstCrackConfig, ResolvedDetectorArtifacts], FirstCrackDetectorBackend
    ] = build_released_onnx_first_crack_detector_backend,
    hasher: Callable[[Path], str] = hash_artifact,
) -> ObservationModel:
    """Resolve, hash, then construct. Called only by the observation worker."""
    identity = configured_identity(config)
    try:
        artifacts = resolver(config)
        if (artifacts.onnx_model.repo_id, artifacts.onnx_model.revision) != (
            artifacts.feature_extractor_config.repo_id,
            artifacts.feature_extractor_config.revision,
        ):
            raise ValueError("Artifact identity mismatch.")
    except Exception as exc:
        raise ObservationModelLoadError(
            identity.model_copy(
                update=dict(
                    status="failed",
                    failure=present("artifact_resolution_failed"),
                    onnx_model_sha256=absent("resolution_failed"),
                    feature_extractor_config_sha256=absent("resolution_failed"),
                )
            )
        ) from exc
    identity = identity.model_copy(
        update=dict(
            repo_id=artifacts.onnx_model.repo_id,
            revision=absent("hub_default_revision")
            if artifacts.onnx_model.revision is None
            else present(artifacts.onnx_model.revision),
            onnx_model_filename=artifacts.onnx_model.filename,
            feature_extractor_filename=artifacts.feature_extractor_config.filename,
        )
    )
    try:
        identity = identity.model_copy(
            update=dict(onnx_model_sha256=present(hasher(artifacts.onnx_model.local_path)))
        )
        identity = identity.model_copy(
            update=dict(
                feature_extractor_config_sha256=present(
                    hasher(artifacts.feature_extractor_config.local_path)
                )
            )
        )
    except Exception as exc:
        identity = identity.model_copy(
            update=dict(
                status="failed",
                failure=present("artifact_hash_failed"),
                onnx_model_sha256=identity.onnx_model_sha256
                if identity.onnx_model_sha256.value is not None
                else absent("hash_failed"),
                feature_extractor_config_sha256=absent("hash_failed"),
            )
        )
        raise ObservationModelLoadError(identity) from exc
    try:
        backend = backend_factory(config, artifacts)
        adapter = build_first_crack_detector_adapter(config, artifacts, backend)
    except Exception as exc:
        raise ObservationModelLoadError(
            identity.model_copy(
                update=dict(status="failed", failure=present("model_backend_failed"))
            )
        ) from exc
    return ObservationModel(adapter, identity.model_copy(update=dict(status="resolved")))


class _Common(TypedDict):
    session_id: str
    generation: int
    record_key: str
    accepted_at: Time


class FirstCrackObserver:
    """Generation-owned inference and persistence, without a SessionStore handle."""

    def __init__(
        self,
        *,
        config: AppConfig,
        session_id: str,
        generation: int,
        session_origin: float,
        session_utc: datetime,
        recording_origin: float | None,
        pipeline: ObservationCapture,
        loader: Callable[[FirstCrackConfig], ObservationModel],
        sink_factory: Callable[[Path], ObservationSink],
        log_dir: Path,
        monotonic_now: Callable[[], float] = time.monotonic,
        stop_timeout_seconds: float = 1.0,
        recording_origin_reader: Callable[[], float | None] | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.generation = generation
        self.clock = ClockMapping(
            session_origin=session_origin,
            session_utc=session_utc,
            recording_origin=recording_origin,
            recording_pending=recording_origin is None and recording_origin_reader is not None,
        )
        self.pipeline = pipeline
        self._recording_origin_reader = recording_origin_reader
        self._capture_stop_attempted = False
        self._capture_stop_returned_normally = False
        self._capture_stop_lock = threading.Lock()
        self._capture_stop_finished = threading.Event()
        self.loader = loader
        self.sink_factory = sink_factory
        self.path = log_dir / "first_crack_observation.jsonl"
        self.now = monotonic_now
        self.stop_timeout = stop_timeout_seconds
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.reaped = threading.Event()
        self.identity = configured_identity(config.first_crack)
        self.settings = observation_settings(config)
        self.counts = Counts()
        self.state: ObserverState = "loading"
        self.durability: Literal["pending", "persisted", "failed"] = "pending"
        self.confirmation: ConfirmationPayload | None = None
        self.summary: SummaryPayload | None = None
        self.human_at: float | None = None
        self.charge_at: float | None = None
        self.cutoff: float | None = None
        self._provisional_cutoff: float | None = None
        self._pending_boundaries: set[object] = set()
        self._held_observation: (
            tuple[
                AudioWindow, FirstCrackWindowObservation, float, float, AudioCaptureSnapshot, bool
            ]
            | None
        ) = None
        self.deadline: float | None = None
        self.reason: str | None = None
        self.failed = False
        self._supervision_failed = False
        self.valid = True
        self.max_probability: float | None = None
        self._sequence = 0
        self._registered: set[str] = set()
        self._drain_complete = False
        self._coverage_loss = False
        self._seen_windows: set[int] = set()
        self._human_written = False
        self._inflight = 0
        self._owned_windows: list[AudioWindow] = []
        self._eligible_inferred = 0
        self._inference_failed = False
        self._thread: threading.Thread | None = None

    def start(self, reservation: object | None = None) -> bool:
        if not coordinator().claim(self, reservation):
            with self.lock:
                self.state = "not_admitted"
                self.reason = "previous_observer_not_reaped"
                self.reaped.set()
            return False
        self._thread = threading.Thread(target=self._run, name="fc-observation-worker", daemon=True)
        self._thread.start()
        return True

    @property
    def worker_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _refresh_recording_origin(self) -> None:
        # The provider is a recorder read on the observation worker, never a
        # request/supervisor callback; previous records stay immutable.
        if self.clock.recording_origin is not None or self._recording_origin_reader is None:
            return
        origin = self._recording_origin_reader()
        if origin is not None and math.isfinite(origin):
            with self.lock:
                if self.summary is None and self.clock.recording_origin is None:
                    self.clock = self.clock.model_copy(
                        update=dict(recording_origin=origin, recording_pending=False)
                    )

    def _stop_capture_owned(self, timeout: float) -> None:
        capture = self.pipeline.stop(timeout_seconds=timeout)
        released = self.pipeline.shutdown_confirmed
        self.note_capture(capture)
        if not released:
            with self.lock:
                self._coverage_loss = True
                self.failed = True
                self.reason = "capture_stop_unconfirmed"

    def _stop_capture(
        self, *, timeout: float | None = None, require_frozen_boundary: bool = False
    ) -> bool:
        # Claim only under the short guard; no lock is held across capture I/O.
        # A lifespan caller may boundedly observe an existing stop owner, never
        # join/wait for model work or run a second concurrent pipeline.stop.
        # Lock order is observer state, then capture-stop ownership.  The
        # owner guard is released before any capture call or owner wait.
        with self.lock:
            if require_frozen_boundary and self.summary is None and self._pending_boundaries:
                return False
            with self._capture_stop_lock:
                claimed = not self._capture_stop_attempted
                if claimed:
                    self._capture_stop_attempted = True
        if not claimed:
            return (
                self._capture_stop_finished.wait(
                    timeout=self.stop_timeout if timeout is None else max(0.0, timeout)
                )
                and self._capture_stop_returned_normally
            )
        try:
            self._stop_capture_owned(self.stop_timeout if timeout is None else max(0.0, timeout))
            self._capture_stop_returned_normally = True
            return True
        finally:
            self._capture_stop_finished.set()

    def shutdown_capture(self, *, timeout: float) -> None:
        """Lifespan-only assurance: no wait/join on model load or inference."""
        self.close(cutoff=None, reason="process_shutdown")
        with self.lock:
            self.deadline = self.now()
            try:
                self._freeze_locked()
            except Exception:
                self._supervision_failed = True
                self.failed = True
                self.valid = False
                self.durability = "failed"
                self.state = "incomplete"
                self.reason = "deadline_supervisor_failed"
        try:
            self._stop_capture(timeout=timeout)
        except Exception:
            with self.lock:
                self.failed = True
                self.state = "incomplete"
                self.reason = "capture_stop_failed"
        self.changed.set()

    def update_boundaries(self, *, charge_at: float | None, human_at: float | None) -> None:
        with self.lock:
            if not self.valid or self.summary is not None:
                return
            if self.charge_at is None:
                self.charge_at = charge_at
            if self.human_at is None:
                self.human_at = human_at
        self.changed.set()

    def close(self, *, cutoff: float | None, reason: str) -> None:
        """Freeze a lifecycle close without treating its clock as an event."""
        with self.lock:
            if self._pending_boundaries:
                self.failed = True
                self.reason = "boundary_not_observed"
                self.deadline = self.now()
                self._freeze_locked()
                self.changed.set()
                coordinator().wake.set()
                return
            if cutoff is None:
                # A lifecycle transition proves that no more observation work
                # may be accepted.  It does not prove that beans were dropped
                # (or that a fault/e-stop event was accepted), so do not turn
                # the current clock into a public cutoff.
                self.failed = True
                self.reason = (
                    reason if re.fullmatch(r"[a-z][a-z0-9_]{0,159}", reason) else "observer_stop"
                )
                self.deadline = self.now()
                self._freeze_locked()
                self.changed.set()
                coordinator().wake.set()
                return
            if self.deadline is None:
                self.cutoff = cutoff
                self.deadline = (
                    self.now() + self.config.first_crack.observation_closing_timeout_seconds
                )
                self.state = "closing"
                if reason != "beans_dropped":
                    self.failed = True
                self.reason = (
                    reason if re.fullmatch(r"[a-z][a-z0-9_]{0,159}", reason) else "observer_stop"
                )
        self.changed.set()
        coordinator().wake.set()

    def open_boundary(self, ticket: object, *, reason: str) -> None:
        """Fence acceptance after a driver return and before canonical commit."""
        with self.lock:
            if not self.valid or self.summary is not None:
                return
            self._pending_boundaries.add(ticket)
            if self.deadline is None:
                self.deadline = (
                    self.now() + self.config.first_crack.observation_closing_timeout_seconds
                )
                self.state = "closing"
                if reason != "beans_dropped":
                    self.failed = True
                self.reason = reason
        self.changed.set()
        coordinator().wake.set()

    def resolve_boundary(self, ticket: object, *, cutoff: float) -> None:
        """Resolve only the matching dispatch ticket with its accepted event time."""
        with self.lock:
            if not self.valid or self.summary is not None or ticket not in self._pending_boundaries:
                return
            self._pending_boundaries.remove(ticket)
            self._provisional_cutoff = (
                cutoff
                if self._provisional_cutoff is None
                else min(self._provisional_cutoff, cutoff)
            )
            if not self._pending_boundaries:
                accepted = (self.cutoff, self._provisional_cutoff)
                self.cutoff = min(value for value in accepted if value is not None)
        self.changed.set()
        coordinator().wake.set()

    def freeze_pending_boundary(self, ticket: object, *, reason: str) -> None:
        """Freeze incomplete observation without inventing a missing cutoff."""
        with self.lock:
            if self.summary is None and ticket in self._pending_boundaries:
                self.failed = True
                self.reason = reason
                self.deadline = self.now()
                self._freeze_locked()
        self.changed.set()
        coordinator().wake.set()

    def _mapped_now_locked(self) -> float:
        return self.clock.session_origin + max(0.0, self.now() - self.clock.session_origin)

    def _freeze_if_due_locked(self) -> None:
        if self._supervision_failed:
            return
        if self.deadline is not None and self.now() >= self.deadline and self.summary is None:
            self._freeze_locked()

    def supervision_failed(self) -> None:
        with self.lock:
            self._supervision_failed = True
            self.valid = False
            self.failed = True
            self.durability = "failed"
            self.state = "incomplete"
            self.reason = "deadline_supervisor_failed"
        self.changed.set()

    def invalidate(self) -> None:
        """A new session cannot admit a result from the old generation."""
        with self.lock:
            if self.summary is None:
                self.valid = False
                self.deadline = self.now()
                self.reason = "session_changed"
                self._freeze_locked()
        self.changed.set()

    def check_deadline(self) -> None:
        """No I/O, inference, capture or driver call; safe with hung append."""
        with self.lock:
            before = self.summary
            self._freeze_if_due_locked()
            frozen_now = before is None and self.summary is not None
        if frozen_now:
            self.changed.set()

    def note_capture(self, capture: AudioCaptureSnapshot) -> None:
        """Latch observations obtained outside our lock, never call capture here."""
        with self.lock:
            if self.summary is not None:
                return
            self.counts = self.counts.model_copy(
                update=dict(
                    capture_dropped_windows=max(
                        self.counts.capture_dropped_windows, capture.dropped_window_count
                    )
                )
            )
            if (
                capture.total_overflow_count
                or capture.overflow_count_last_minute
                or capture.estimated_lost_audio_ms_last_minute
                or capture.latest_error is not None
            ):
                self._coverage_loss = True

    def snapshot(self) -> ObservationSnapshot:
        with self.lock:
            return ObservationSnapshot(
                observer_state=self.state,
                identity=self.identity,
                settings=self.settings,
                first_confirmation=absent("no_confirmation")
                if self.confirmation is None
                else present(self.confirmation),
                frozen_summary=absent() if self.summary is None else present(self.summary),
                durability=self.durability,
                worker_reaped=self.reaped.is_set(),
            )

    def _count(self, **changes: int) -> None:
        self.counts = self.counts.model_copy(
            update={name: getattr(self.counts, name) + value for name, value in changes.items()}
        )

    def _common_locked(self, *, register: bool = True) -> _Common:
        self._sequence += 1
        key = f"{self.session_id}:{self.generation}:record:{self._sequence}"
        if register:
            self._registered.add(key)
            self._count(persistence_pending_records=1)
        return _Common(
            session_id=self.session_id,
            generation=self.generation,
            record_key=key,
            accepted_at=self.clock.at(self.now()),
        )

    def _append(self, sink: ObservationSink, record: Record) -> bool:
        # Acceptance is registered before releasing the lock for I/O. The
        # supervisor can freeze meanwhile; completion changes durability only.
        with self.lock:
            self._freeze_if_due_locked()
            if record.record_key not in self._registered:
                return False
        try:
            sink.append(record)
            completed = self.now()
        except Exception:  # noqa: BLE001 - external sink errors are diagnostic.
            with self.lock:
                self._registered.discard(record.record_key)
                self._count(persistence_pending_records=-1, persistence_failed_records=1)
                self.durability = "failed"
                self.failed = True
            return False
        with self.lock:
            self._registered.discard(record.record_key)
            self._count(persistence_pending_records=-1)
            receipt = ReceiptRecord(
                **self._common_locked(register=False),
                payload=ReceiptPayload(
                    persisted_record_key=record.record_key,
                    append_completed_at=self.clock.at(completed),
                ),
            )
        try:
            sink.append(receipt)
        except Exception:  # noqa: BLE001 - receipt is not recursively receipted.
            with self.lock:
                self._count(persistence_failed_records=1)
                self.durability = "failed"
                self.failed = True
        return True

    def _run(self) -> None:
        sink: ObservationSink | None = None
        try:
            sink = self.sink_factory(self.path)
            try:
                loaded = self.loader(self.config.first_crack)
                self._refresh_recording_origin()
                adapter = loaded.adapter
                identity = loaded.identity
            except Exception as exc:  # noqa: BLE001 - sanitised diagnostic, no secrets.
                identity = (
                    exc.identity
                    if isinstance(exc, ObservationModelLoadError)
                    else self.identity.model_copy(
                        update=dict(
                            status="failed",
                            failure=present("model_loader_failed"),
                            onnx_model_sha256=absent("resolution_failed"),
                            feature_extractor_config_sha256=absent("resolution_failed"),
                        )
                    )
                )
                with self.lock:
                    self._freeze_if_due_locked()
                    if self.summary is not None or self._supervision_failed:
                        return
                    self.identity = identity
                    self.failed = True
                    self.state = "unavailable"
                    record = IdentityRecord(
                        **self._common_locked(),
                        payload=IdentityPayload(
                            identity=self.identity,
                            settings=self.settings,
                            load_available_at=self.clock.at(self.now()),
                        ),
                    )
                self._append(sink, record)
                self._wait_until_close(sink)
                return
            with self.lock:
                self._freeze_if_due_locked()
                if self.summary is not None or self._supervision_failed:
                    return
                self.identity = identity
                self.state = "observing" if self.deadline is None else "closing"
                record = IdentityRecord(
                    **self._common_locked(),
                    payload=IdentityPayload(
                        identity=identity,
                        settings=self.settings,
                        load_available_at=self.clock.at(self.now()),
                    ),
                )
            self._append(sink, record)
            while True:
                self.check_deadline()
                with self.lock:
                    if self.summary is not None or self._supervision_failed:
                        break
                    charge = self.charge_at
                    closing = self.deadline is not None and not self._pending_boundaries
                self._refresh_recording_origin()
                self._write_human(sink)
                with self.lock:
                    held = None if self._pending_boundaries else self._held_observation
                    if held is not None:
                        self._held_observation = None
                if held is not None:
                    self._observe(sink, *held[:-1], inference_failed=held[-1])
                    self.changed.wait(0.05)
                    self.changed.clear()
                    continue
                if closing:
                    stopped_normally = self._stop_capture(require_frozen_boundary=True)
                    self.check_deadline()
                    with self.lock:
                        if self.summary is not None:
                            break
                    if not stopped_normally:
                        # Another owner may still be stopping capture. Do not
                        # drain or declare completion ahead of its flush.
                        self.changed.wait(0.05)
                        self.changed.clear()
                        continue
                with self.lock:
                    pending_boundary = bool(self._pending_boundaries)
                    needs_batch = not self._owned_windows
                if pending_boundary:
                    self.changed.wait(0.05)
                    self.changed.clear()
                    continue
                capture = self.pipeline.snapshot()
                self.note_capture(capture)
                if capture.latest_error is not None:
                    with self.lock:
                        self.failed = True
                        self.reason = "capture_failed"
                    self.close(cutoff=self.now(), reason="capture_failed")
                if needs_batch:
                    with self.lock:
                        self._freeze_if_due_locked()
                        needs_batch = (
                            self.valid
                            and self.summary is None
                            and not self._supervision_failed
                            and not self._pending_boundaries
                            and not self._owned_windows
                        )
                    if not needs_batch:
                        self.changed.wait(0.05)
                        self.changed.clear()
                        continue
                    windows = self.pipeline.drain_windows(
                        max_windows=max(1, capture.queued_window_count)
                    )
                    with self.lock:
                        self._owned_windows.extend(windows)
                cutoff: float | None = None
                end = 0.0
                deferred = False
                with self.lock:
                    # Capture calls above are external I/O.  Their return does
                    # not preserve a prior work claim: a close, invalidation or
                    # deadline may have frozen admission while they ran.
                    self._freeze_if_due_locked()
                    if not self.valid or self.summary is not None or self._supervision_failed:
                        window = None
                    else:
                        window = self._owned_windows.pop(0) if self._owned_windows else None
                    if window is None:
                        pass
                    elif window.sequence_number in self._seen_windows:
                        if (
                            self.confirmation is not None
                            and self.confirmation.confirming_window_sequence
                            == window.sequence_number
                        ):
                            self._count(duplicate_confirmations_rejected=1)
                        continue
                    elif self._pending_boundaries:
                        self._owned_windows.insert(0, window)
                        window = None
                        deferred = True
                    else:
                        self._seen_windows.add(window.sequence_number)
                        cutoff = self.cutoff
                        charge = self.charge_at
                        # AudioWindow timestamps are capture times, not delivery times.
                        end = window.started_at_monotonic_seconds + window.duration_seconds
                        strict_witness = cutoff is not None or end < self._mapped_now_locked()
                        if not strict_witness:
                            self._owned_windows.insert(0, window)
                            self._seen_windows.discard(window.sequence_number)
                            window = None
                        else:
                            self._inflight = int(
                                charge is not None
                                and window.started_at_monotonic_seconds >= charge
                                and (cutoff is None or end < cutoff)
                            )
                if window is None:
                    if deferred:
                        self.changed.wait(0.05)
                        self.changed.clear()
                        continue
                    if closing:
                        with self.lock:
                            if self.summary is None:
                                self._drain_complete = True
                                self._freeze_locked()
                        break
                    self.changed.wait(0.05)
                    self.changed.clear()
                    continue
                if (
                    charge is None
                    or window.started_at_monotonic_seconds < charge
                    or (cutoff is not None and end >= cutoff)
                ):
                    self._record_skipped(sink, window, capture)
                    continue
                began = self.now()
                try:
                    observation = adapter.process_window_observed(
                        window, earliest_eligible_monotonic_seconds=charge
                    )
                    available = self.now()  # MUST precede lock/sink admission.
                except Exception:  # noqa: BLE001 - model failure has no authority.
                    available = self.now()
                    self._observe(
                        sink,
                        window,
                        FirstCrackWindowObservation(
                            window.sequence_number, None, 0, False, "listening", None
                        ),
                        available,
                        max(0.0, (available - began) * 1000),
                        capture,
                        inference_failed=True,
                    )
                    continue
                self._observe(
                    sink,
                    window,
                    observation,
                    available,
                    max(0.0, (available - began) * 1000),
                    capture,
                )
        except Exception:  # noqa: BLE001 - observer infrastructure is isolated.
            with self.lock:
                self.failed = True
                self.durability = "failed"
                self.reason = "observer_failed"
            self._wait_until_close()
        finally:
            stop_returned_normally = False
            try:
                stop_returned_normally = self._stop_capture()
            except Exception:  # noqa: BLE001 - report incomplete capture honestly.
                with self.lock:
                    self.failed = True
                    self.reason = "capture_stop_failed"
            record = None
            with self.lock:
                if self.summary is None and not self._supervision_failed:
                    try:
                        self._freeze_locked()
                    except Exception:
                        self._supervision_failed = True
                        self.failed = True
                        self.durability = "failed"
                        self.reason = "deadline_supervisor_failed"
                if self.summary is not None:
                    record = SummaryRecord(
                        **self._common_locked(register=False), payload=self.summary
                    )
            if sink is not None and record is not None:
                try:
                    sink.finalise(record)
                    with self.lock:
                        if self.durability != "failed":
                            self.durability = "persisted"
                except Exception:  # noqa: BLE001 - memory is not durable evidence.
                    with self.lock:
                        self.durability = "failed"
            released = False
            if stop_returned_normally:
                try:
                    released = self.pipeline.shutdown_confirmed
                except Exception:  # noqa: BLE001 - unknown release retains ownership.
                    stop_returned_normally = False
            with self.lock:
                self.state = "incomplete" if self._supervision_failed or not released else "stopped"
            # The diagnostic prefix is already final. A slow capture release
            # retains this same worker/slot with bounded per-iteration work,
            # never another stop, drain, inference or persistence operation.
            while not released:
                if self._capture_stop_finished.is_set():
                    if not self._capture_stop_returned_normally:
                        break  # A completed exception must never look clean.
                    try:
                        released = self.pipeline.shutdown_confirmed
                    except Exception:  # noqa: BLE001 - unknown release retains ownership.
                        break
                if not released:
                    self.changed.wait(0.05)
                    self.changed.clear()
            if released:
                self.reaped.set()

    def _wait_until_close(self, sink: ObservationSink | None = None) -> None:
        while True:
            if sink is not None:
                self._refresh_recording_origin()
                self._write_human(sink)
            self.check_deadline()
            with self.lock:
                if self.summary is not None or self._supervision_failed:
                    return
            self.changed.wait(0.05)
            self.changed.clear()

    def _write_human(self, sink: ObservationSink) -> None:
        with self.lock:
            self._freeze_if_due_locked()
            if self.human_at is None or self._human_written or self.summary is not None:
                return
            self._human_written = True
            record = HumanRecord(
                **self._common_locked(),
                payload=HumanPayload(
                    human_mark=self.clock.at(self.human_at),
                    first_confirmation_key_seen_at_acceptance=absent("no_confirmation")
                    if self.confirmation is None
                    else present(self.confirmation.first_confirmation_key),
                ),
            )
        self._append(sink, record)

    def _record_skipped(
        self, sink: ObservationSink, window: AudioWindow, capture: AudioCaptureSnapshot
    ) -> None:
        self.note_capture(capture)
        with self.lock:
            self._freeze_if_due_locked()
            if self.summary is not None or not self.valid:
                return
            self._inflight = 0
            end = window.started_at_monotonic_seconds + window.duration_seconds
            eligibility = (
                "ineligible_post_drop"
                if self.cutoff is not None and end >= self.cutoff
                else "ineligible_pre_charge"
            )
            self._count(observed_windows=1)
            now = self.now()
            missing_time = Time(
                session_relative_seconds=absent("not_applicable"),
                recording_relative_seconds=absent("not_applicable"),
                utc=absent("not_applicable"),
            )
            # On not_inferred only, availability_class describes this row
            # acceptance vs cutoff, never a model decision (which is absent).
            record = WindowRecord(
                **self._common_locked(),
                payload=WindowPayload(
                    sequence=window.sequence_number,
                    probability=absent("not_applicable"),
                    positive_window_count=0,
                    window_status="not_inferred",
                    window_start=self.clock.at(window.started_at_monotonic_seconds),
                    window_end=self.clock.at(end),
                    decision_available_at=missing_time,
                    inference_duration_ms=0,
                    queue_depth=capture.queued_window_count,
                    capture_dropped_windows=self.counts.capture_dropped_windows,
                    overflow_count_last_minute=capture.overflow_count_last_minute,
                    eligibility=eligibility,
                    availability_class="drop_not_observed"
                    if self.cutoff is None
                    else "after_drop"
                    if now >= self.cutoff
                    else "before_drop",
                    human_mark_seen_at_decision=False,
                ),
            )
        self._append(sink, record)

    def _observe(
        self,
        sink: ObservationSink,
        window: AudioWindow,
        observation: FirstCrackWindowObservation,
        available: float,
        duration: float,
        capture: AudioCaptureSnapshot,
        *,
        inference_failed: bool = False,
    ) -> None:
        self.note_capture(capture)
        with self.lock:
            self._freeze_if_due_locked()
            if self.summary is not None or not self.valid:
                coordinator().note_late_result()
                return
            end = window.started_at_monotonic_seconds + window.duration_seconds
            if self._pending_boundaries or (
                self.cutoff is None and end >= self._mapped_now_locked()
            ):
                self._held_observation = (
                    window,
                    observation,
                    available,
                    duration,
                    capture,
                    inference_failed,
                )
                return
            self._inflight = 0
            if self.cutoff is not None and end >= self.cutoff:
                eligibility = "ineligible_post_drop"
            elif self.charge_at is None or window.started_at_monotonic_seconds < self.charge_at:
                eligibility = "ineligible_pre_charge"
            else:
                eligibility = "eligible"
            self._count(observed_windows=1, eligible_windows=int(eligibility == "eligible"))
            confidence = observation.confidence
            unscored_positive = confidence is None and (
                observation.confirmed
                or observation.fc_status in {"candidate", "confirmed"}
                or observation.event is not None
            )
            if inference_failed:
                self._inference_failed = True
                self.failed = True
                self.reason = "inference_failed"
            elif unscored_positive:
                self.failed = True
                self.reason = "confirmation_probability_unavailable"
            if eligibility == "eligible" and not inference_failed and not unscored_positive:
                self._eligible_inferred += 1
            if confidence is not None:
                self.max_probability = max(confidence, self.max_probability or 0.0)
            confirmation_record = None
            event = observation.event
            if event is not None and confidence is None:
                self.failed = True
                self.reason = "confirmation_probability_unavailable"
            if event is not None and confidence is not None and eligibility == "eligible":
                if self.confirmation is None:
                    self.confirmation = ConfirmationPayload(
                        first_confirmation_key=f"{self.session_id}:{self.generation}:{window.sequence_number}",
                        confirming_window_sequence=window.sequence_number,
                        model_onset=self.clock.at(event.detected_at_monotonic_seconds),
                        model_onset_inferred=event.detected_at_inferred,
                        confirming_window_end=self.clock.at(end),
                        decision_available_at=self.clock.at(available),
                        confirming_probability=confidence,
                        positive_window_count=observation.positive_window_count,
                        human_mark_at_acceptance=absent()
                        if self.human_at is None
                        else present(self.clock.at(self.human_at)),
                    )
                    self.state = "confirmed" if self.deadline is None else "closing"
                    confirmation_record = ConfirmationRecord(
                        **self._common_locked(), payload=self.confirmation
                    )
                else:
                    self._count(duplicate_confirmations_rejected=1)
            payload = WindowPayload(
                sequence=window.sequence_number,
                probability=absent("inference_failed" if inference_failed else "not_applicable")
                if confidence is None
                else present(confidence),
                positive_window_count=observation.positive_window_count,
                window_status="error"
                if inference_failed or unscored_positive
                else "confirmed"
                if observation.fc_status == "confirmed"
                else "candidate"
                if observation.fc_status == "candidate"
                else "negative",
                window_start=self.clock.at(window.started_at_monotonic_seconds),
                window_end=self.clock.at(end),
                decision_available_at=self.clock.at(available),
                inference_duration_ms=duration,
                queue_depth=capture.queued_window_count,
                capture_dropped_windows=self.counts.capture_dropped_windows,
                overflow_count_last_minute=capture.overflow_count_last_minute,
                eligibility=eligibility,
                availability_class="drop_not_observed"
                if self.cutoff is None
                else "after_drop"
                if available >= self.cutoff
                else "before_drop",
                human_mark_seen_at_decision=self.human_at is not None
                and self.human_at <= available,
            )
            record = WindowRecord(**self._common_locked(), payload=payload)
        if confirmation_record is not None:
            self._append(sink, confirmation_record)
        self._append(sink, record)

    def _freeze_locked(self) -> None:
        if self.summary is not None:
            return
        confirmation = self.confirmation
        coverage = (
            not self._pending_boundaries
            and not self.failed
            and not self._coverage_loss
            and self.identity.status == "resolved"
            and self._drain_complete
            and self._eligible_inferred > 0
            and self.counts.capture_dropped_windows == 0
            and self._inflight == 0
            and not self._owned_windows
        )
        # This is owned, known eligible work only. An undrained capture queue
        # has unknown eligibility and keeps coverage incomplete via drain_complete.
        # Without an accepted cutoff, the lifecycle clock is evidence only for
        # work whose end is strictly before the mapped session-progress witness;
        # it is never published as a drop cutoff.
        progress = self._mapped_now_locked()
        pending_eligible = {
            item.sequence_number
            for item in self._owned_windows
            if item.sequence_number not in self._seen_windows
            and self.charge_at is not None
            and item.started_at_monotonic_seconds >= self.charge_at
            and item.started_at_monotonic_seconds + item.duration_seconds
            < (self.cutoff if self.cutoff is not None else progress)
        }
        self.counts = self.counts.model_copy(
            update=dict(
                undecided_eligible_windows=0
                if self._pending_boundaries
                else self._inflight + len(pending_eligible)
            )
        )
        outcome = (
            "confirmed"
            if confirmation is not None
            else "unavailable"
            if not self._eligible_inferred
            else "no_confirmation"
            if coverage
            else "incomplete"
        )
        if confirmation is None:
            order = "both_absent" if self.human_at is None else "model_absent"
        elif self.human_at is None:
            order = "human_absent"
        else:
            model_time = confirmation.decision_available_at.session_relative_seconds.value
            if model_time is None:
                self.failed = True
                coverage = False
                order = "model_absent"
                delta = None
            else:
                delta = model_time - (self.human_at - self.clock.session_origin)
            order = (
                "model_absent"
                if delta is None
                else "same_instant"
                if delta == 0
                else "model_before_human"
                if delta < 0
                else "human_before_model"
            )
        self.summary = SummaryPayload(
            identity=self.identity,
            settings=self.settings,
            outcome=outcome,
            coverage_complete=coverage,
            counts=self.counts,
            first_confirmation_key=absent("no_confirmation")
            if confirmation is None
            else present(confirmation.first_confirmation_key),
            human_mark=absent() if self.human_at is None else present(self.clock.at(self.human_at)),
            model_onset=absent("no_confirmation")
            if confirmation is None
            else present(confirmation.model_onset),
            confirming_window_end=absent("no_confirmation")
            if confirmation is None
            else present(confirmation.confirming_window_end),
            decision_available_at=absent("no_confirmation")
            if confirmation is None
            else present(confirmation.decision_available_at),
            drop_cutoff=absent("not_yet_observed")
            if self._pending_boundaries
            else absent()
            if self.cutoff is None
            else present(self.clock.at(self.cutoff)),
            frozen_at=self.clock.at(self.now()),
            max_probability=absent(
                "load_not_completed"
                if self.identity.status == "pending"
                else "model_never_loaded"
                if self.identity.status == "failed"
                else "inference_failed"
                if self._inference_failed
                else "not_applicable"
            )
            if self.max_probability is None
            else present(self.max_probability),
            order=order,
            reason=absent("not_applicable") if self.reason is None else present(self.reason),
        )
