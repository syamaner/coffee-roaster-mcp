# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event, get_ident

import pytest

from coffee_roaster_mcp.artifacts import ResolvedArtifact, ResolvedDetectorArtifacts
from coffee_roaster_mcp.audio import AudioCaptureSnapshot, AudioWindow
from coffee_roaster_mcp.config import AppConfig, FirstCrackConfig, RecordingConfig
from coffee_roaster_mcp.detector import (
    FirstCrackDetectorOutput,
)
from coffee_roaster_mcp.first_crack_observation import (
    ConfirmationRecord,
    Record,
    WindowRecord,
)
from coffee_roaster_mcp.first_crack_observer import (
    FirstCrackObserver,
    JsonlObservationSink,
    ObservationSink,
    load_observation_model,
)


class Capture:
    def __init__(self) -> None:
        self.windows: list[AudioWindow] = []
        self.stopped = False
        self.overflow = 0
        self.dropped = 0

    def snapshot(self) -> AudioCaptureSnapshot:
        return AudioCaptureSnapshot(
            not self.stopped,
            len(self.windows),
            len(self.windows),
            self.dropped,
            None,
            total_overflow_count=self.overflow,
        )

    def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
        result = tuple(self.windows[:max_windows])
        del self.windows[: len(result)]
        return result

    def stop(self, *, timeout_seconds: float = 1) -> AudioCaptureSnapshot:
        self.stopped = True
        return self.snapshot()

    @property
    def shutdown_confirmed(self) -> bool:
        return self.stopped


class Sink:
    def __init__(self) -> None:
        self.rows: list[Record] = []
        self.appended = Event()
        self.confirmed = Event()

    def append(self, record: Record) -> None:
        self.rows.append(record)
        if isinstance(record, ConfirmationRecord):
            self.confirmed.set()
        self.appended.set()

    def finalise(self, summary: Record) -> None:
        self.append(summary)


class Backend:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.thread_id: int | None = None

    def detect(self, window: AudioWindow) -> FirstCrackDetectorOutput:
        self.thread_id = get_ident()
        self.entered.set()
        assert self.release.wait(5)
        return FirstCrackDetectorOutput(confirmed=True, confidence=0.95)


def window(sequence: int = 1, start: float = 5.0) -> AudioWindow:
    return AudioWindow(sequence, None, 16000, start, 1.0, (0.0,) * 16)


def setup_observer(
    tmp_path: Path, *, now: list[float] | None = None, sink: Sink | None = None
) -> tuple[FirstCrackObserver, Capture, Sink, Backend]:
    config = AppConfig(
        first_crack=FirstCrackConfig(mode="audio", event_authority="manual"),
        recording=RecordingConfig(enabled=True, autocapture=True),
    )
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"synthetic model")
    prep = tmp_path / "preprocessor.json"
    prep.write_bytes(b"{}")
    artifacts = ResolvedDetectorArtifacts(
        ResolvedArtifact("test", "test-rev", "onnx/int8/model_quantized.onnx", onnx),
        ResolvedArtifact("test", "test-rev", "onnx/int8/preprocessor_config.json", prep),
    )
    backend = Backend()

    def loader(fc: FirstCrackConfig):
        return load_observation_model(
            fc, resolver=lambda _: artifacts, backend_factory=lambda _config, _artifacts: backend
        )

    capture = Capture()
    sink = sink or Sink()
    clock = now or [10.0]
    observer = FirstCrackObserver(
        config=config,
        session_id=tmp_path.name,
        generation=1,
        session_origin=0,
        session_utc=datetime(2026, 1, 1, tzinfo=UTC),
        recording_origin=2,
        pipeline=capture,
        loader=loader,
        sink_factory=lambda _: sink,
        log_dir=tmp_path,
        monotonic_now=lambda: clock[0],
    )
    return observer, capture, sink, backend


def test_confirmation_is_diagnostic_durable_and_clock_mapped(tmp_path: Path) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window())
    assert observer.start()
    assert backend.entered.wait(2)
    assert sink.confirmed.wait(2)
    confirmation = next(row for row in sink.rows if isinstance(row, ConfirmationRecord))
    assert confirmation.payload.decision_available_at.session_relative_seconds.value == 10
    assert confirmation.payload.model_onset.recording_relative_seconds.value == 3
    assert backend.thread_id != get_ident()
    observer.update_boundaries(charge_at=1, human_at=11)
    observer.close(cutoff=12, reason="beans_dropped")
    assert observer.reaped.wait(2)
    snapshot = observer.snapshot()
    assert snapshot.frozen_summary.value is not None
    assert snapshot.frozen_summary.value.order == "model_before_human"
    assert snapshot.durability == "persisted"
    import hashlib

    assert (
        snapshot.identity.onnx_model_sha256.value == hashlib.sha256(b"synthetic model").hexdigest()
    )
    assert (
        snapshot.identity.feature_extractor_config_sha256.value == hashlib.sha256(b"{}").hexdigest()
    )
    assert snapshot.first_confirmation.value is not None
    assert snapshot.first_confirmation.value.human_mark_at_acceptance.value is None


def test_deadline_is_enforced_at_acceptance_without_supervisor_poll(tmp_path: Path) -> None:
    now = [21.0]
    observer, capture, sink, _ = setup_observer(tmp_path, now=now)
    observer.charge_at = 1
    observer.cutoff = 15
    observer.deadline = 20
    observer._inflight = 1
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(window())
    observer._observe(sink, window(), output, 19.0, 1.0, capture.snapshot())
    assert observer.confirmation is None
    assert observer.summary is not None
    assert observer.summary.coverage_complete is False
    assert observer.summary.counts.undecided_eligible_windows == 1
    assert not sink.rows


def test_human_seen_at_decision_uses_timestamp_not_later_admission(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    observer.charge_at = 1
    observer.human_at = 10
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(window())
    observer._observe(sink, window(), output, 9.0, 1.0, capture.snapshot())
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert not row.payload.human_mark_seen_at_decision
    assert observer.confirmation is not None
    assert observer.confirmation.human_mark_at_acceptance.value is not None


@pytest.mark.parametrize("overflow,dropped", [(1, 0), (0, 1), (0, 0)])
def test_capture_loss_never_yields_clean_negative(
    tmp_path: Path, overflow: int, dropped: int
) -> None:
    observer, capture, _, _ = setup_observer(tmp_path)
    capture.overflow = overflow
    capture.dropped = dropped
    observer.note_capture(capture.snapshot())
    observer.identity = observer.loader(observer.config.first_crack).identity
    observer.counts = observer.counts.model_copy(
        update=dict(observed_windows=1, eligible_windows=1)
    )
    observer._drain_complete = True
    observer._eligible_inferred = 1
    with observer.lock:
        observer._freeze_locked()
    assert observer.summary is not None
    assert observer.summary.coverage_complete is (not bool(overflow or dropped))
    assert observer.summary.outcome == ("incomplete" if overflow or dropped else "no_confirmation")


def test_hung_loader_freezes_without_join_and_blocks_new_worker(tmp_path: Path) -> None:
    now = [10.0]
    observer, capture, _, _ = setup_observer(tmp_path, now=now)
    entered = Event()
    release = Event()
    original = observer.loader

    def blocked(config: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(config)

    observer.loader = blocked
    assert observer.start()
    assert entered.wait(2)
    observer.close(cutoff=12, reason="beans_dropped")
    now[0] = 16
    observer.check_deadline()
    assert observer.snapshot().frozen_summary.value is not None
    assert observer.snapshot().durability == "pending"
    assert not observer.reaped.is_set()
    other_path = tmp_path / "next"
    other_path.mkdir()
    other, _, _, _ = setup_observer(other_path)
    assert not other.start()
    release.set()
    assert observer.reaped.wait(2)
    assert capture.stopped


def test_blocked_confirmation_sink_does_not_block_human_or_freeze(tmp_path: Path) -> None:
    entered = Event()
    release = Event()

    class BlockedSink(Sink):
        def append(self, record: Record) -> None:
            if isinstance(record, ConfirmationRecord):
                entered.set()
                assert release.wait(5)
            super().append(record)

    sink = BlockedSink()
    now = [10.0]
    observer, capture, _, _ = setup_observer(tmp_path, now=now, sink=sink)
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window())
    assert observer.start()
    assert entered.wait(2)
    observer.update_boundaries(charge_at=1, human_at=11)
    observer.close(cutoff=12, reason="beans_dropped")
    now[0] = 16
    observer.check_deadline()
    assert observer.snapshot().frozen_summary.value is not None
    assert observer.snapshot().durability == "pending"
    release.set()
    assert observer.reaped.wait(2)
    assert sum(isinstance(row, ConfirmationRecord) for row in sink.rows) == 1


def test_jsonl_sink_roundtrips_and_fsyncs(tmp_path: Path) -> None:
    observer, _, _, _ = setup_observer(tmp_path)
    with observer.lock:
        observer._freeze_locked()
        from coffee_roaster_mcp.first_crack_observation import SummaryRecord

        assert observer.summary is not None
        row = SummaryRecord(**observer._common_locked(register=False), payload=observer.summary)
    path = tmp_path / "diagnostics" / "first_crack_observation.jsonl"
    sink: ObservationSink = JsonlObservationSink(path)
    sink.finalise(row)
    assert path.read_text().count("\n") == 1
    assert '"record_type":"fc_observation_summary"' in path.read_text()


def test_new_generation_rejects_blocked_prior_inference(tmp_path: Path) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)
    backend.release.clear()
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window())
    assert observer.start()
    assert backend.entered.wait(2)
    observer.invalidate()
    assert observer.summary is not None
    frozen = observer.summary.model_dump_json()
    assert observer.summary.counts.stale_results_discarded == 0
    assert observer.summary.counts.late_results_discarded_before_freeze == 0
    from coffee_roaster_mcp.first_crack_observer import coordinator

    late_before = coordinator().late_results_after_freeze
    backend.release.set()
    assert observer.reaped.wait(2)
    assert observer.confirmation is None
    assert not any(isinstance(row, ConfirmationRecord) for row in sink.rows)
    assert observer.summary is not None and not observer.summary.coverage_complete
    # Invalidation freezes under the acceptance lock; a returned inference is
    # post-freeze, so only the process metric changes, never frozen counts.
    assert observer.summary.model_dump_json() == frozen
    assert coordinator().late_results_after_freeze == late_before + 1
    assert not any(isinstance(row, WindowRecord) for row in sink.rows)


def test_post_drop_window_cannot_add_confirmation(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    observer.charge_at = 1
    observer.cutoff = 6
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(window())
    observer._observe(sink, window(), output, 7.0, 1.0, capture.snapshot())
    assert observer.confirmation is None
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.eligibility == "ineligible_post_drop"


def test_ticket_cleanup_is_identity_scoped_and_keeps_earliest_cutoff(tmp_path: Path) -> None:
    """A resolved or foreign ticket cannot freeze another pending dispatch."""
    observer, _, _, _ = setup_observer(tmp_path)
    earlier, later, foreign = object(), object(), object()
    observer.open_boundary(earlier, reason="beans_dropped")
    observer.open_boundary(later, reason="emergency_stop")
    observer.resolve_boundary(later, cutoff=8)

    observer.freeze_pending_boundary(later, reason="later_cleanup")
    observer.freeze_pending_boundary(foreign, reason="foreign_cleanup")

    assert observer.summary is None
    assert earlier in observer._pending_boundaries
    assert observer.cutoff is None
    observer.resolve_boundary(earlier, cutoff=6)
    assert observer.cutoff == 6


def test_invalidation_with_pending_ticket_never_invents_public_cutoff(tmp_path: Path) -> None:
    """Lifecycle invalidation freezes unresolved work as unknown evidence."""
    now = [10.0]
    observer, _, _, _ = setup_observer(tmp_path, now=now)
    ticket = object()
    observer.open_boundary(ticket, reason="beans_dropped")
    observer.invalidate()

    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert observer.summary.counts.undecided_eligible_windows == 0


def test_lifecycle_close_freezes_only_proven_owned_work_without_a_cutoff(tmp_path: Path) -> None:
    """A lifecycle clock proves progress, never a synthetic drop boundary."""
    now = [10.0]
    observer, capture, sink, backend = setup_observer(tmp_path, now=now)
    backend.release.clear()
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.extend(
        [
            window(sequence=1, start=2),  # In flight after strict admission.
            window(sequence=2, start=4),  # Owned and strictly before progress.
            window(sequence=3, start=9),  # Tied with progress: unknown.
            window(sequence=4, start=10),  # Future: unknown.
        ]
    )
    assert observer.start()
    try:
        assert backend.entered.wait(2)
        observer.close(cutoff=None, reason="session_finalisation")
        assert observer.summary is not None
        assert observer.summary.drop_cutoff.value is None
        assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
        assert observer.summary.counts.undecided_eligible_windows == 2
        assert not observer.summary.coverage_complete
        frozen = observer.summary.model_dump_json()
        backend.release.set()
        observer.changed.set()
        assert observer.reaped.wait(2)
        assert observer.summary.model_dump_json() == frozen
    finally:
        backend.release.set()
        observer.changed.set()
        assert observer.reaped.wait(2)
    assert not any(isinstance(row, WindowRecord) for row in sink.rows)


def test_lifecycle_close_retains_confirmation_without_creating_postdrop_rows(
    tmp_path: Path,
) -> None:
    """A lifecycle freeze retains accepted confirmation but invents no drop evidence."""
    observer, capture, sink, backend = setup_observer(tmp_path)
    observer.update_boundaries(charge_at=1, human_at=9)
    capture.windows.append(window(start=2))
    assert observer.start()
    try:
        assert backend.entered.wait(2)
        assert sink.confirmed.wait(2)
        observer.close(cutoff=None, reason="process_shutdown")
        assert observer.reaped.wait(2)
    finally:
        observer.changed.set()
        assert observer.reaped.wait(2)
    assert observer.summary is not None
    assert observer.summary.outcome == "confirmed"
    assert observer.summary.first_confirmation_key.value is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    rows = [row.payload for row in sink.rows if isinstance(row, WindowRecord)]
    assert len(rows) == 1
    assert rows[0].eligibility == "eligible"
    assert rows[0].availability_class == "drop_not_observed"


def test_resolved_boundary_cannot_replace_earlier_complete_cutoff(tmp_path: Path) -> None:
    """A later accepted event folds into, rather than replaces, prior evidence."""
    observer, _, _, _ = setup_observer(tmp_path)
    observer.cutoff = 6
    ticket = object()
    observer.open_boundary(ticket, reason="emergency_stop")
    observer.resolve_boundary(ticket, cutoff=8)

    assert observer.cutoff == 6
    observer.invalidate()
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is not None
    assert observer.summary.drop_cutoff.value.session_relative_seconds.value == 6
    frozen = observer.summary.model_dump_json()
    observer.resolve_boundary(ticket, cutoff=4)
    observer.freeze_pending_boundary(ticket, reason="stale_cleanup")
    assert observer.summary.model_dump_json() == frozen


def test_mapped_clock_equality_holds_window_without_raw_clock_admission(tmp_path: Path) -> None:
    """The strict witness uses the session mapping, not a larger raw clock."""
    origin = 5.948083016636624
    raw_now = 15.538684000000002
    observer, capture, sink, _ = setup_observer(tmp_path, now=[raw_now])
    observer.clock = observer.clock.model_copy(update={"session_origin": origin})
    observer.charge_at = 1
    equal_window = window(start=14.538684)
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(
        equal_window
    )

    observer._observe(sink, equal_window, output, raw_now, 0, capture.snapshot())

    assert observer._mapped_now_locked() == 15.538684
    assert observer._held_observation is not None
    assert observer.confirmation is None
    assert not sink.rows


def test_precharge_positive_window_cannot_confirm(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    observer.charge_at = 6
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(
        window(), earliest_eligible_monotonic_seconds=6
    )
    observer._observe(sink, window(), output, 7.0, 1.0, capture.snapshot())
    assert observer.confirmation is None
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.eligibility == "ineligible_pre_charge"


def test_worker_flushes_capture_before_final_eligible_drain(tmp_path: Path) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)
    processed = Event()
    original_append = sink.append

    def append(record: Record) -> None:
        original_append(record)
        if isinstance(record, WindowRecord):
            processed.set()

    sink.append = append

    def detect(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(
            confirmed=window.sequence_number == 2,
            confidence=0.95 if window.sequence_number == 2 else 0.1,
        )

    backend.detect = detect
    original_stop = capture.stop

    def stop(*, timeout_seconds: float = 1.0) -> AudioCaptureSnapshot:
        capture.windows.append(window(sequence=2, start=5))
        return original_stop(timeout_seconds=timeout_seconds)

    capture.stop = stop
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window(sequence=1, start=3))
    assert observer.start()
    assert processed.wait(2)
    observer.close(cutoff=8, reason="beans_dropped")
    assert observer.reaped.wait(2)
    assert observer.summary is not None and observer.summary.outcome == "confirmed"
    assert observer.summary.counts.eligible_windows == 2
    assert not capture.windows


def test_delayed_recording_origin_is_latched_without_rewriting_prior_null(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observation import ClockMapping, IdentityRecord

    observer, capture, sink, _ = setup_observer(tmp_path)
    origin: list[float | None] = [None]
    observer.clock = ClockMapping(
        session_origin=0, session_utc=datetime(2026, 1, 1, tzinfo=UTC), recording_pending=True
    )
    observer._recording_origin_reader = lambda: origin[0]
    assert observer.start()
    assert sink.appended.wait(2)
    identity = next(row for row in sink.rows if isinstance(row, IdentityRecord))
    assert (
        identity.payload.load_available_at.recording_relative_seconds.null_reason
        == "not_yet_observed"
    )
    origin[0] = 2.0
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window())
    observer.changed.set()
    assert sink.confirmed.wait(2)
    assert (
        observer.confirmation is not None
        and observer.confirmation.model_onset.recording_relative_seconds.value == 3
    )
    origin[0] = 999.0
    observer.close(cutoff=12, reason="beans_dropped")
    assert observer.reaped.wait(2)
    assert observer.clock.recording_origin == 2
    assert identity.payload.load_available_at.recording_relative_seconds.value is None


def test_capture_drop_count_is_lifetime_max_across_stale_inference_snapshot(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    observer.charge_at = 1
    stale = capture.snapshot()
    capture.dropped = 7
    observer.note_capture(capture.snapshot())
    loaded = observer.loader(observer.config.first_crack)
    output = loaded.adapter.process_window_observed(window())
    observer._observe(sink, window(), output, 10.0, 1.0, stale)
    assert observer.counts.capture_dropped_windows == 7
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.capture_dropped_windows == 7
    with observer.lock:
        observer._freeze_locked()
    assert observer.summary is not None and observer.summary.counts.capture_dropped_windows == 7


@pytest.mark.parametrize(
    "start,charge,cutoff", [(0.0, 1.0, 12.0), (5.0, 1.0, 6.0), (5.5, 1.0, 6.0), (7.0, 1.0, 6.0)]
)
def test_actual_drain_records_not_inferred_without_backend(
    tmp_path: Path, start: float, charge: float, cutoff: float
) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)
    observer.update_boundaries(charge_at=charge, human_at=None)
    observer.close(cutoff=cutoff, reason="beans_dropped")
    capture.windows.append(window(start=start))
    assert observer.start()
    assert observer.reaped.wait(2)
    assert not backend.entered.is_set()
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.window_status == "not_inferred"
    assert row.payload.probability.null_reason == "not_applicable"
    assert row.payload.decision_available_at.utc.null_reason == "not_applicable"
    assert row.payload.inference_duration_ms == 0
    assert not row.payload.human_mark_seen_at_decision
    assert observer.summary is not None
    assert (
        observer.summary.counts.observed_windows == 1
        and observer.summary.counts.eligible_windows == 0
    )
    assert observer.summary.outcome == "unavailable"
    assert not observer.summary.coverage_complete
    # With no decision on this tag, the class describes ROW acceptance vs cutoff.
    assert row.payload.availability_class == ("after_drop" if cutoff <= 10 else "before_drop")


def test_eligible_negative_with_ineligible_tail_has_complete_eligible_coverage(
    tmp_path: Path,
) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)

    def negative(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(confirmed=False, confidence=0.1)

    backend.detect = negative
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8, reason="beans_dropped")
    capture.windows.extend([window(sequence=1, start=3), window(sequence=2, start=8)])
    assert observer.start()
    assert observer.reaped.wait(2)
    assert observer.summary is not None and observer.summary.outcome == "no_confirmation"
    assert observer.summary.coverage_complete
    assert (
        observer.summary.counts.observed_windows == 2
        and observer.summary.counts.eligible_windows == 1
    )
    from coffee_roaster_mcp.first_crack_observation import IdentityRecord

    assert any(isinstance(row, IdentityRecord) for row in sink.rows)


def test_actual_inference_error_emits_diagnostic_error_not_clean_miss(tmp_path: Path) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)

    def fail(window: AudioWindow) -> FirstCrackDetectorOutput:
        raise RuntimeError("private path must not escape")

    backend.detect = fail
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8, reason="beans_dropped")
    capture.windows.append(window())
    assert observer.start()
    assert observer.reaped.wait(2)
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert (
        row.payload.window_status == "error"
        and row.payload.probability.null_reason == "inference_failed"
    )
    assert observer.summary is not None and observer.summary.outcome != "no_confirmation"
    assert observer.summary.max_probability.null_reason == "inference_failed"
    assert not observer.summary.coverage_complete
    assert "private path" not in "".join(row.model_dump_json() for row in sink.rows)


def test_duplicate_ordinary_windows_do_not_increment_confirmation_counter(tmp_path: Path) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)
    calls: list[int] = []

    def negative(window: AudioWindow) -> FirstCrackDetectorOutput:
        calls.append(window.sequence_number)
        return FirstCrackDetectorOutput(confirmed=False, confidence=0.1)

    backend.detect = negative
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8, reason="beans_dropped")
    capture.windows.extend([window(), window()])
    assert observer.start()
    assert observer.reaped.wait(2)
    assert observer.summary is not None
    assert observer.summary.counts.observed_windows == 1
    assert observer.summary.counts.duplicate_confirmations_rejected == 0
    assert observer.summary.counts.eligible_windows == 1
    # Ordinary replay is idempotent, not stale-generation or late evidence.
    assert observer.summary.counts.stale_results_discarded == 0
    assert observer.summary.counts.late_results_discarded_before_freeze == 0
    assert calls == [1]
    assert sum(isinstance(row, WindowRecord) for row in sink.rows) == 1


def test_duplicate_confirmations_retain_first_idempotency_key(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8, reason="beans_dropped")
    capture.windows.extend([window(), window(), window(sequence=2, start=6)])
    assert observer.start()
    assert observer.reaped.wait(2)
    confirmations = [row for row in sink.rows if isinstance(row, ConfirmationRecord)]
    assert len(confirmations) == 1 and confirmations[0].payload.confirming_window_sequence == 1
    assert (
        observer.summary is not None
        and observer.summary.counts.duplicate_confirmations_rejected == 2
    )


def test_default_model_loading_resolves_hashes_then_constructs(tmp_path: Path) -> None:
    observer, _, _, backend = setup_observer(tmp_path)
    loaded = observer.loader(observer.config.first_crack)
    artifacts = loaded.adapter.artifacts
    steps: list[str] = []

    def resolve(_: FirstCrackConfig):
        steps.append("resolve")
        return artifacts

    def digest(path: Path) -> str:
        steps.append("hash:" + path.name)
        return "a" * 64

    def construct(_: FirstCrackConfig, _artifacts: ResolvedDetectorArtifacts):
        steps.append("construct")
        return backend

    loaded = load_observation_model(
        observer.config.first_crack, resolver=resolve, hasher=digest, backend_factory=construct
    )
    assert steps == ["resolve", "hash:model.onnx", "hash:preprocessor.json", "construct"]
    assert loaded.identity.onnx_model_sha256.value == "a" * 64


@pytest.mark.parametrize("stage", ["resolution", "hash", "backend"])
def test_model_failure_identity_is_durable_even_without_confirmation(
    tmp_path: Path, stage: str
) -> None:
    from coffee_roaster_mcp.first_crack_observation import IdentityRecord
    from coffee_roaster_mcp.first_crack_observer import ObservationModel

    now = [10.0]
    observer, _, sink, backend = setup_observer(tmp_path, now=now)
    artifacts = observer.loader(observer.config.first_crack).adapter.artifacts

    def resolve(_: FirstCrackConfig) -> ResolvedDetectorArtifacts:
        if stage == "resolution":
            raise RuntimeError("private token")
        return artifacts

    def digest(_: Path) -> str:
        if stage == "hash":
            raise OSError("private token")
        return "a" * 64

    def construct(_: FirstCrackConfig, _artifacts: ResolvedDetectorArtifacts):
        if stage == "backend":
            raise RuntimeError("private token")
        return backend

    def loader(fc: FirstCrackConfig) -> ObservationModel:
        return load_observation_model(
            fc, resolver=resolve, hasher=digest, backend_factory=construct
        )

    observer.loader = loader
    assert observer.start()
    assert sink.appended.wait(2)
    row = next(row for row in sink.rows if isinstance(row, IdentityRecord))
    assert row.payload.identity.status == "failed"
    if stage == "backend":
        assert row.payload.identity.onnx_model_sha256.value == "a" * 64
    else:
        assert row.payload.identity.onnx_model_sha256.null_reason == (
            "hash_failed" if stage == "hash" else "resolution_failed"
        )
    observer.close(cutoff=12, reason="beans_dropped")
    now[0] = 16
    observer.check_deadline()
    assert observer.reaped.wait(2)
    assert observer.summary is not None and observer.summary.outcome == "unavailable"
    assert observer.summary.max_probability.null_reason == "model_never_loaded"
    assert "private token" not in "".join(row.model_dump_json() for row in sink.rows)


@pytest.mark.parametrize("fail_at", ["append", "summary"])
def test_sink_failures_never_report_persisted(tmp_path: Path, fail_at: str) -> None:

    class FailureSink(Sink):
        def append(self, record: Record) -> None:
            if fail_at == "append":
                raise OSError("private path")
            super().append(record)

        def finalise(self, summary: Record) -> None:
            if fail_at == "summary":
                raise OSError("private path")
            super().finalise(summary)

    observer, _, _, _ = setup_observer(tmp_path, sink=FailureSink())
    observer.close(cutoff=12, reason="beans_dropped")
    assert observer.start()
    assert observer.reaped.wait(2)
    assert observer.snapshot().durability == "failed"


def test_shutdown_stops_capture_without_waiting_for_hung_loader(tmp_path: Path) -> None:
    observer, capture, _, _ = setup_observer(tmp_path)
    entered = Event()
    release = Event()
    original = observer.loader

    def loader(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original(fc)

    observer.loader = loader
    assert observer.start()
    assert entered.wait(2)
    observer.shutdown_capture(timeout=0.1)
    assert capture.stopped and capture.shutdown_confirmed
    assert not observer.reaped.is_set()
    assert observer.summary is not None and not observer.summary.coverage_complete
    release.set()
    assert observer.reaped.wait(2)


def test_bad_generation_does_not_kill_shared_deadline_supervisor(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observer import coordinator

    observer, _, _, _ = setup_observer(tmp_path)
    entered = Event()
    release = Event()
    failed = Event()
    original_loader = observer.loader

    def blocked(fc: FirstCrackConfig):
        entered.set()
        assert release.wait(5)
        return original_loader(fc)

    observer.loader = blocked
    original_failed = observer.supervision_failed

    def on_failed() -> None:
        original_failed()
        failed.set()

    observer.supervision_failed = on_failed
    assert observer.start()
    assert entered.wait(2)

    def invalid_clock() -> float:
        raise ValueError("invalid injected clock")

    observer.close(cutoff=12, reason="beans_dropped")
    observer.now = invalid_clock
    coordinator().wake.set()
    assert failed.wait(2)
    assert observer.snapshot().observer_state == "incomplete"
    assert observer.snapshot().durability == "failed"
    release.set()
    assert observer.reaped.wait(2)
    next_path = tmp_path / "next-valid"
    next_path.mkdir()
    now = [10.0]
    other, _, _, _ = setup_observer(next_path, now=now)
    entered2 = Event()
    release2 = Event()
    freeze = Event()
    original2 = other.loader

    def block_next(fc: FirstCrackConfig):
        entered2.set()
        assert release2.wait(5)
        return original2(fc)

    other.loader = block_next
    original_check = other.check_deadline

    def check() -> None:
        original_check()
        if other.summary is not None:
            freeze.set()

    other.check_deadline = check
    assert other.start()
    assert entered2.wait(2)
    other.close(cutoff=12, reason="beans_dropped")
    now[0] = 16
    coordinator().wake.set()
    assert freeze.wait(2)  # The same supervisor survived the prior exception.
    release2.set()
    assert other.reaped.wait(2)


def test_model_human_order_tie_uses_decision_not_acoustic_onset(tmp_path: Path) -> None:
    observer, capture, sink, _ = setup_observer(tmp_path)
    loaded = observer.loader(observer.config.first_crack)
    observer.identity = loaded.identity
    observer.charge_at = 1
    observer.human_at = 10
    output = loaded.adapter.process_window_observed(window())
    observer._observe(sink, window(), output, 10.0, 1.0, capture.snapshot())
    with observer.lock:
        observer._freeze_locked()
    assert observer.summary is not None and observer.summary.order == "same_instant"
    assert (
        observer.confirmation is not None
        and observer.confirmation.model_onset.session_relative_seconds.value != 10
    )


def test_deadline_counts_all_owned_eligible_buffered_work(tmp_path: Path) -> None:
    now = [10.0]
    observer, capture, _, backend = setup_observer(tmp_path, now=now)
    backend.release.clear()
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.extend(
        [window(sequence=1, start=3), window(sequence=2, start=5), window(sequence=3, start=20)]
    )
    assert observer.start()
    assert backend.entered.wait(2)
    observer.close(cutoff=8, reason="beans_dropped")
    now[0] = 16
    observer.check_deadline()
    assert observer.summary is not None
    assert observer.summary.counts.undecided_eligible_windows == 2
    assert not observer.summary.coverage_complete
    backend.release.set()
    assert observer.reaped.wait(2)


def test_emergency_prefix_does_not_claim_complete_normal_negative(tmp_path: Path) -> None:
    observer, capture, _sink, backend = setup_observer(tmp_path)

    def negative(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(confirmed=False, confidence=0.1)

    backend.detect = negative
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8, reason="emergency_stop")
    capture.windows.append(window())
    assert observer.start()
    assert observer.reaped.wait(2)
    assert observer.summary is not None and observer.summary.outcome == "incomplete"
    assert not observer.summary.coverage_complete


def test_failed_load_persists_one_human_mark_before_closing(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observation import HumanRecord, IdentityRecord

    identity_written = Event()
    human_written = Event()

    class EvidenceSink(Sink):
        def append(self, record: Record) -> None:
            super().append(record)
            if isinstance(record, IdentityRecord):
                identity_written.set()
            if isinstance(record, HumanRecord):
                human_written.set()

    now = [10.0]
    observer, _, sink, _ = setup_observer(tmp_path, now=now, sink=EvidenceSink())

    def fail(_: FirstCrackConfig):
        raise RuntimeError("synthetic load failure")

    observer.loader = fail
    assert observer.start()
    try:
        assert identity_written.wait(2)
        observer.update_boundaries(charge_at=1, human_at=9)
        assert human_written.wait(2)
        observer.update_boundaries(charge_at=1, human_at=10)
        observer.close(cutoff=12, reason="beans_dropped")
    finally:
        observer.close(cutoff=12, reason="beans_dropped")
        now[0] = 16
        observer.check_deadline()
        assert observer.reaped.wait(2)
    rows = [row for row in sink.rows if isinstance(row, HumanRecord)]
    assert len(rows) == 1
    assert observer.summary is not None
    assert rows[0].payload.human_mark == observer.summary.human_mark.value
    assert rows[0].payload.human_mark.session_relative_seconds.value == 9
    assert observer.summary.identity.status == "failed"
    assert observer.summary.identity.onnx_model_sha256.value is None
    assert observer.summary.outcome == "unavailable"
    assert observer.summary.first_confirmation_key.value is None
    assert not any(isinstance(row, ConfirmationRecord) for row in sink.rows)


def test_delayed_capture_loss_cannot_rewrite_frozen_prefix(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observation import SummaryRecord

    entered, release = Event(), Event()
    now = [10.0]
    observer, capture, sink, _ = setup_observer(tmp_path, now=now)
    original_stop = capture.stop

    def blocked_stop(*, timeout_seconds: float = 1) -> AudioCaptureSnapshot:
        entered.set()
        assert release.wait(5)
        capture.dropped = 7
        capture.overflow = 2
        return original_stop(timeout_seconds=timeout_seconds)

    capture.stop = blocked_stop
    observer.close(cutoff=12, reason="beans_dropped")
    assert observer.start()
    try:
        assert entered.wait(2)
        now[0] = 16
        observer.check_deadline()
        assert observer.summary is not None
        frozen = observer.summary.model_dump_json()
        assert not observer.summary.coverage_complete
        assert observer.summary.outcome != "no_confirmation"
        assert observer.summary.counts.capture_dropped_windows == 0
        assert observer.summary.counts.stale_results_discarded == 0
        assert observer.summary.counts.late_results_discarded_before_freeze == 0
    finally:
        release.set()
        assert observer.reaped.wait(2)
    assert capture.dropped == 7 and capture.overflow == 2
    assert observer.summary.model_dump_json() == frozen
    persisted = [row for row in sink.rows if isinstance(row, SummaryRecord)]
    assert len(persisted) == 1
    assert persisted[0].payload.model_dump_json() == frozen
    assert observer.snapshot().durability == "persisted"


def test_slow_capture_release_retains_same_worker_and_frozen_prefix(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observation import SummaryRecord
    from coffee_roaster_mcp.first_crack_observer import coordinator

    released, persisted, checked_after_persist = Event(), Event(), Event()

    class SlowCapture(Capture):
        stop_calls = 0

        def stop(self, *, timeout_seconds: float = 1) -> AudioCaptureSnapshot:
            self.stop_calls += 1
            return super().stop(timeout_seconds=timeout_seconds)

        @property
        def shutdown_confirmed(self) -> bool:
            if persisted.is_set():
                checked_after_persist.set()
            return released.is_set()

    class EvidenceSink(Sink):
        def finalise(self, summary: Record) -> None:
            super().finalise(summary)
            persisted.set()

    observer, _, sink, _ = setup_observer(tmp_path, sink=EvidenceSink())
    capture = SlowCapture()
    observer.pipeline = capture
    observer.close(cutoff=12, reason="beans_dropped")
    assert observer.start()
    worker = observer._thread
    try:
        assert checked_after_persist.wait(2)
        assert observer.worker_running and not observer.reaped.is_set()
        assert observer.summary is not None
        frozen = observer.summary.model_dump_json()
        assert not observer.summary.coverage_complete
        assert observer.summary.outcome != "no_confirmation"
        assert observer.summary.max_probability.null_reason == "not_applicable"
        assert not coordinator().reserve(object())
        assert capture.stop_calls == 1
    finally:
        released.set()
        observer.changed.set()
        assert observer.reaped.wait(2)
    assert observer._thread is worker
    assert capture.stop_calls == 1
    assert observer.summary.model_dump_json() == frozen
    summaries = [row for row in sink.rows if isinstance(row, SummaryRecord)]
    assert len(summaries) == 1 and summaries[0].payload.model_dump_json() == frozen
    token = object()
    assert coordinator().reserve(token)
    coordinator().release(token)


@pytest.mark.parametrize(
    "scenario",
    [
        "cleanup_release",
        "cleanup_release_recorder_first",
        "cleanup_stop_error",
        "cleanup_close_error",
        "observer_stop_error",
    ],
)
def test_release_ownership_exception_and_cleanup_controls(tmp_path: Path, scenario: str) -> None:
    """Permanent failed owners stay isolated in their own synthetic process."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = """
import sys,threading
from pathlib import Path
from test_first_crack_observer import Capture,setup_observer
from coffee_roaster_mcp.first_crack_observer import CaptureCleanup,coordinator
scenario = sys.argv[1]
stop_returned = threading.Event()
class Pipeline(Capture):
    calls = 0
    released = threading.Event()
    checked = threading.Event()
    def stop(self, *, timeout_seconds=1):
        self.calls += 1
        self.stopped = True
        stop_returned.set()
        if scenario in {'cleanup_stop_error','observer_stop_error'}:
            raise RuntimeError('synthetic stop failure')
        return self.snapshot()
    @property
    def shutdown_confirmed(self):
        self.checked.set()
        return self.released.is_set()
pipeline = Pipeline()
class Recorder:
    calls = 0
    closed = threading.Event()
    released = threading.Event()
    checked = threading.Event()
    def close(self):
        self.calls += 1
        self.closed.set()
        if scenario == 'cleanup_close_error':
            raise RuntimeError('synthetic recorder failure')
    @property
    def shutdown_confirmed(self):
        self.checked.set()
        return self.released.is_set()
recorder = Recorder()
if scenario == 'observer_stop_error':
    now = [10.0]
    observer,_,_,_ = setup_observer(Path.cwd(),now=now)
    observer.pipeline = pipeline
    observer.close(cutoff=12,reason='beans_dropped')
    assert observer.start()
    assert stop_returned.wait(2)
    now[0] = 16
    observer.check_deadline()
    observer._thread.join(2)  # Test only; production requests never join.
    assert not observer.worker_running
    pipeline.released.set()
    assert observer._stop_capture() is False
    assert not observer._capture_stop_returned_normally
    assert not observer.reaped.is_set()
    assert pipeline.calls == 1
    assert not coordinator().reserve(object())
    assert observer.summary is not None and not observer.summary.coverage_complete
else:
    token = object()
    assert coordinator().reserve(token)
    owner = CaptureCleanup(pipeline,recorder,0)
    owner.start(token,None)
    assert stop_returned.wait(2)
    if scenario in {'cleanup_release','cleanup_release_recorder_first'}:
        assert recorder.closed.wait(2)
        assert not owner.finished.is_set() and not owner.reaped.is_set()
        assert not coordinator().reserve(object())
        if scenario == 'cleanup_release':
            pipeline.released.set()
            assert recorder.checked.wait(2)
            assert not owner.reaped.is_set()  # Recorder remains independently owned.
            recorder.released.set()
        else:
            recorder.released.set()
            pipeline.checked.clear()
            assert pipeline.checked.wait(2)
            assert not owner.reaped.is_set()  # Capture remains independently owned.
            pipeline.released.set()
        assert owner.reaped.wait(2) and owner.finished.is_set()
        assert pipeline.calls == recorder.calls == 1
        next_token = object()
        assert coordinator().reserve(next_token)
        coordinator().release(next_token)
    else:
        assert owner.finished.wait(2)
        pipeline.released.set()
        recorder.released.set()
        assert not owner.reaped.is_set()
        assert not coordinator().reserve(object())
        assert pipeline.calls == 1
        assert recorder.calls == (0 if scenario == 'cleanup_stop_error' else 1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, scenario],
        cwd=tmp_path,
        env={"PYTHONPATH": str(root / "src") + ":" + str(root / "tests")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_lifespan_stop_owner_cannot_block_frozen_summary_persistence(tmp_path: Path) -> None:
    import threading

    loaded, release_loader, stop_entered, release_stop, persisted = (Event() for _ in range(5))

    class BlockingCapture(Capture):
        calls = 0

        def stop(self, *, timeout_seconds: float = 1) -> AudioCaptureSnapshot:
            self.calls += 1
            stop_entered.set()
            assert release_stop.wait(5)
            return super().stop(timeout_seconds=timeout_seconds)

    class EvidenceSink(Sink):
        def finalise(self, summary: Record) -> None:
            super().finalise(summary)
            persisted.set()

    observer, _, sink, _ = setup_observer(tmp_path, sink=EvidenceSink())
    capture = BlockingCapture()
    observer.pipeline = capture
    observer.stop_timeout = 0.01
    original = observer.loader

    def blocked(fc: FirstCrackConfig):
        loaded.set()
        assert release_loader.wait(5)
        return original(fc)

    observer.loader = blocked
    assert observer.start()
    assert loaded.wait(2)
    owner = threading.Thread(target=lambda: observer.shutdown_capture(timeout=0.01))
    owner.start()
    try:
        assert stop_entered.wait(2)
        release_loader.set()
        assert persisted.wait(2), "non-owner stop latch held final diagnostic persistence"
        assert observer.worker_running and not observer.reaped.is_set()
        assert not observer._capture_stop_finished.is_set()
        assert observer.summary is not None and not observer.summary.coverage_complete
        frozen = observer.summary.model_dump_json()
        assert capture.calls == 1
    finally:
        release_loader.set()
        release_stop.set()
        owner.join(2)  # Test-owned lifespan thread, never a production request join.
        assert not owner.is_alive()
        observer.changed.set()
        assert observer.reaped.wait(2)
    assert observer.summary.model_dump_json() == frozen
    assert capture.calls == 1
    assert len([row for row in sink.rows if row.record_type == "fc_observation_summary"]) == 1


def test_supervision_failure_closes_acceptance_without_fabricating_summary(tmp_path: Path) -> None:
    from coffee_roaster_mcp.first_crack_observer import coordinator

    observer, capture, sink, _ = setup_observer(tmp_path)
    output = observer.loader(observer.config.first_crack).adapter.process_window_observed(window())
    observer.supervision_failed()
    before = coordinator().late_results_after_freeze
    observer._observe(sink, window(), output, 10, 1, capture.snapshot())
    assert not observer.valid and observer.summary is None
    assert observer.snapshot().durability == "failed"
    assert observer.confirmation is None and not sink.rows
    assert observer.counts.observed_windows == 0
    assert observer.counts.stale_results_discarded == 0
    assert observer.counts.late_results_discarded_before_freeze == 0
    assert coordinator().late_results_after_freeze == before + 1


@pytest.mark.parametrize("scores", [(None,), (0.2, None, 0.7)])
def test_valid_scoreless_negatives_are_inferred_without_fabricated_probability(
    tmp_path: Path, scores: tuple[float | None, ...]
) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)

    def negative(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(
            confirmed=False, confidence=scores[window.sequence_number - 1]
        )

    backend.detect = negative
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=9, reason="beans_dropped")
    capture.windows.extend(window(sequence=i + 1, start=2 + i) for i in range(len(scores)))
    assert observer.start() and observer.reaped.wait(2)
    rows = [row.payload for row in sink.rows if isinstance(row, WindowRecord)]
    assert len(rows) == len(scores)
    for row, score in zip(rows, scores, strict=True):
        assert row.window_status == "negative" and row.eligibility == "eligible"
        assert row.probability.value == score
        assert row.probability.null_reason == ("not_applicable" if score is None else None)
        assert row.decision_available_at.session_relative_seconds.value == 10
    assert observer.summary is not None and observer.summary.coverage_complete
    assert observer.summary.outcome == "no_confirmation"
    assert observer._eligible_inferred == len(scores)
    assert not observer._inference_failed
    expected = max((score for score in scores if score is not None), default=None)
    assert observer.summary.max_probability.value == expected
    assert observer.summary.max_probability.null_reason == (
        "not_applicable" if expected is None else None
    )
    assert not any(isinstance(row, ConfirmationRecord) for row in sink.rows)


@pytest.mark.parametrize("minimum", [1, 2])
def test_scoreless_positive_is_not_a_confirmation_or_clean_negative(
    tmp_path: Path, minimum: int
) -> None:
    from dataclasses import replace

    from coffee_roaster_mcp.first_crack_observer import observation_settings

    observer, capture, sink, backend = setup_observer(tmp_path)
    observer.config = replace(
        observer.config,
        first_crack=replace(observer.config.first_crack, min_positive_windows=minimum),
    )
    observer.settings = observation_settings(observer.config)

    def positive(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(confirmed=True, confidence=None)

    backend.detect = positive
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=9, reason="beans_dropped")
    capture.windows.append(window())
    assert observer.start() and observer.reaped.wait(2)
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.window_status == "error"
    assert row.payload.probability.value is None
    assert row.payload.probability.null_reason == "not_applicable"
    assert observer.confirmation is None
    assert observer.summary is not None and not observer.summary.coverage_complete
    assert observer.summary.outcome != "no_confirmation"
    assert observer.summary.reason.value == "confirmation_probability_unavailable"
    assert observer.summary.max_probability.null_reason == "not_applicable"
    assert not observer._inference_failed


@pytest.mark.parametrize("probability", [float("nan"), -0.1, 1.1])
def test_invalid_probability_remains_a_real_inference_error(
    tmp_path: Path, probability: float
) -> None:
    observer, capture, sink, backend = setup_observer(tmp_path)

    def invalid(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(confirmed=False, confidence=probability)

    backend.detect = invalid
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=9, reason="beans_dropped")
    capture.windows.append(window())
    assert observer.start() and observer.reaped.wait(2)
    row = next(row for row in sink.rows if isinstance(row, WindowRecord))
    assert row.payload.window_status == "error"
    assert row.payload.probability.null_reason == "inference_failed"
    assert observer._inference_failed and observer.confirmation is None
    assert observer.summary is not None and not observer.summary.coverage_complete
    assert observer.summary.max_probability.null_reason == "inference_failed"


def test_process_error_then_normal_close_retains_window_evidence_and_incomplete_outcome(
    tmp_path: Path,
) -> None:
    processed = Event()

    class EvidenceSink(Sink):
        def append(self, record: Record) -> None:
            super().append(record)
            if sum(isinstance(row, WindowRecord) for row in self.rows) == 2:
                processed.set()

    observer, capture, sink, backend = setup_observer(tmp_path, sink=EvidenceSink())

    def mixed(window: AudioWindow) -> FirstCrackDetectorOutput:
        return FirstCrackDetectorOutput(
            confirmed=window.sequence_number == 2,
            confidence=0.3 if window.sequence_number == 1 else None,
        )

    backend.detect = mixed
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.extend([window(sequence=1, start=2), window(sequence=2, start=3)])
    assert observer.start()
    try:
        assert processed.wait(2)
        assert observer.summary is None
        observer.close(cutoff=9, reason="beans_dropped")
    finally:
        observer.close(cutoff=9, reason="beans_dropped")
        assert observer.reaped.wait(2)
    rows = [row.payload for row in sink.rows if isinstance(row, WindowRecord)]
    assert [row.window_status for row in rows] == ["negative", "error"]
    assert rows[0].probability.value == 0.3
    assert rows[1].probability.null_reason == "not_applicable"
    assert observer.summary is not None
    assert observer.summary.outcome == "incomplete" and not observer.summary.coverage_complete
    assert observer.summary.max_probability.value == 0.3
    assert observer.summary.counts.eligible_windows == 2
    assert observer.confirmation is None and not observer._inference_failed
    # One free-text summary reason is not an exhaustive failure inventory or
    # a promised priority order. Per-window evidence carries the unusable output.


def test_pending_ticket_fences_drain_returning_from_pre_ticket_snapshot(tmp_path: Path) -> None:
    """A ticket opened during snapshot prevents a subsequent fresh drain claim."""

    snapshot_entered = Event()
    release_snapshot = Event()
    drain_called = Event()
    worker_held_pending = Event()

    class SnapshotBlockedCapture(Capture):
        def snapshot(self) -> AudioCaptureSnapshot:
            snapshot_entered.set()
            assert release_snapshot.wait(5)
            return super().snapshot()

        def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
            drain_called.set()
            return super().drain_windows(max_windows=max_windows)

    observer, _, _, _ = setup_observer(tmp_path)
    capture = SnapshotBlockedCapture()
    capture.windows.append(window())
    observer.pipeline = capture
    original_wait = observer.changed.wait

    def acknowledge_pending_hold(timeout: float | None = None) -> bool:
        if release_snapshot.is_set():
            worker_held_pending.set()
        return original_wait(timeout)

    observer.changed.wait = acknowledge_pending_hold
    ticket = object()
    assert observer.start()
    try:
        assert snapshot_entered.wait(2)
        observer.open_boundary(ticket, reason="beans_dropped")
        release_snapshot.set()
        assert worker_held_pending.wait(2)
        assert not drain_called.is_set()
        assert capture.windows and not capture.stopped
        observer.freeze_pending_boundary(ticket, reason="test_cleanup")
    finally:
        release_snapshot.set()
        observer.freeze_pending_boundary(ticket, reason="test_cleanup")
        assert observer.reaped.wait(2)


def test_running_worker_later_ticket_does_not_consume_earlier_pending_window(
    tmp_path: Path,
) -> None:
    """A later resolution leaves earlier pending work owned and post-drop ineligible."""

    now = [104.0]
    observer, capture, sink, _ = setup_observer(tmp_path, now=now)
    pending_hold = Event()
    window_recorded = Event()
    original_wait = observer.changed.wait
    original_append = sink.append

    def acknowledge_pending_hold(timeout: float | None = None) -> bool:
        pending_hold.set()
        return original_wait(timeout)

    def append(record: Record) -> None:
        original_append(record)
        if isinstance(record, WindowRecord):
            window_recorded.set()

    observer.changed.wait = acknowledge_pending_hold
    sink.append = append
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window(start=105.0))
    earlier, later = object(), object()
    observer.open_boundary(earlier, reason="beans_dropped")
    now[0] = 107.0
    observer.open_boundary(later, reason="emergency_stop")
    assert observer.start()
    try:
        observer.resolve_boundary(later, cutoff=108.0)
        assert pending_hold.wait(2)
        assert capture.windows
        now[0] = 108.0
        observer.resolve_boundary(earlier, cutoff=105.0)
        assert window_recorded.wait(2)
        row = next(row for row in sink.rows if isinstance(row, WindowRecord))
        assert row.payload.eligibility == "ineligible_post_drop"
        assert observer.confirmation is None and observer.cutoff == 105.0
    finally:
        observer.close(cutoff=105.0, reason="beans_dropped")
        assert observer.reaped.wait(2)
    assert observer.summary is not None and observer.summary.counts.eligible_windows == 0


def test_running_worker_held_inference_retains_original_availability(tmp_path: Path) -> None:
    """A result returned during a pending boundary keeps its original decision time."""

    now = [7.0]
    observer, capture, sink, backend = setup_observer(tmp_path, now=now)
    held = Event()
    original_wait = observer.changed.wait

    def acknowledge_held_result(timeout: float | None = None) -> bool:
        if backend.release.is_set():
            held.set()
        return original_wait(timeout)

    observer.changed.wait = acknowledge_held_result
    backend.release.clear()
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window(start=5.0))
    ticket = object()
    assert observer.start()
    try:
        assert backend.entered.wait(2)
        observer.open_boundary(ticket, reason="beans_dropped")
        backend.release.set()
        assert held.wait(2)
        assert observer.confirmation is None
        now[0] = 8.0
        observer.resolve_boundary(ticket, cutoff=7.5)
        assert sink.confirmed.wait(2)
        assert observer.confirmation is not None
        assert observer.confirmation.decision_available_at.session_relative_seconds.value == 7.0
        row = next(row for row in sink.rows if isinstance(row, WindowRecord))
        assert row.payload.availability_class == "before_drop"
    finally:
        backend.release.set()
        observer.close(cutoff=7.5, reason="beans_dropped")
        assert observer.reaped.wait(2)


def test_running_worker_mapped_clock_tie_holds_until_strictly_advanced(tmp_path: Path) -> None:
    """The worker rejects the mapped equality tie before accepting a later raw sample."""

    origin = 5.948083016636624
    raw_now = 15.538684000000002
    now = [raw_now]
    observer, capture, sink, _ = setup_observer(tmp_path, now=now)
    observer.clock = observer.clock.model_copy(update={"session_origin": origin})
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window(start=14.538684))
    held = Event()
    original_wait = observer.changed.wait

    def acknowledge_tie_hold(timeout: float | None = None) -> bool:
        held.set()
        return original_wait(timeout)

    observer.changed.wait = acknowledge_tie_hold
    assert observer.start()
    try:
        assert held.wait(2)
        assert observer.confirmation is None and not sink.confirmed.is_set()
        now[0] = 15.538685
        observer.changed.set()
        assert sink.confirmed.wait(2)
        assert observer.confirmation is not None
        assert (
            observer.confirmation.decision_available_at.session_relative_seconds.value
            == 15.538685 - origin
        )
    finally:
        observer.close(cutoff=16.0, reason="beans_dropped")
        assert observer.reaped.wait(2)


def test_frozen_post_drain_window_cannot_claim_fresh_inference(tmp_path: Path) -> None:
    """A drain returned after deadline freeze cannot begin a new backend call."""

    drain_entered = Event()
    release_drain = Event()

    class DrainBlockedCapture(Capture):
        def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
            drain_entered.set()
            assert release_drain.wait(5)
            return super().drain_windows(max_windows=max_windows)

    now = [10.0]
    observer, _, _, backend = setup_observer(tmp_path, now=now)
    capture = DrainBlockedCapture()
    capture.windows.append(window(start=5.0))
    observer.pipeline = capture
    backend.release.clear()
    observer.update_boundaries(charge_at=1, human_at=None)
    observer.close(cutoff=8.0, reason="beans_dropped")
    assert observer.start()
    try:
        assert drain_entered.wait(2)
        now[0] = 15.0
        observer.check_deadline()
        assert observer.summary is not None
        release_drain.set()
        assert observer.reaped.wait(2)
        assert not backend.entered.is_set()
    finally:
        release_drain.set()
        backend.release.set()
        assert observer.reaped.wait(2)


def test_pending_closing_drain_masks_cutoff_and_clean_coverage(tmp_path: Path) -> None:
    """A pending ticket during the claimed closing drain preserves unknown evidence."""

    first_window_recorded = Event()
    drain_entered = Event()
    release_drain = Event()

    class ClosingDrainCapture(Capture):
        def drain_windows(self, *, max_windows: int | None = None) -> tuple[AudioWindow, ...]:
            if self.stopped:
                drain_entered.set()
                assert release_drain.wait(5)
            return super().drain_windows(max_windows=max_windows)

    class NegativeSink(Sink):
        def append(self, record: Record) -> None:
            super().append(record)
            if isinstance(record, WindowRecord):
                first_window_recorded.set()

    sink = NegativeSink()
    observer, _, _, backend = setup_observer(tmp_path, sink=sink)
    capture = ClosingDrainCapture()
    observer.pipeline = capture
    backend.detect = lambda window: FirstCrackDetectorOutput(confirmed=False, confidence=0.1)
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window(start=5.0))
    assert observer.start()
    try:
        assert first_window_recorded.wait(2)
        observer.close(cutoff=8.0, reason="beans_dropped")
        assert drain_entered.wait(2)
        ticket = object()
        observer.open_boundary(ticket, reason="emergency_stop")
        release_drain.set()
        assert observer.reaped.wait(2)
    finally:
        release_drain.set()
        assert observer.reaped.wait(2)
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert observer.summary.outcome == "incomplete"
    assert observer.summary.counts.eligible_windows == 1
    assert observer.summary.counts.undecided_eligible_windows == 0


@pytest.mark.parametrize(
    ("reason", "confirmed", "coverage_complete", "outcome"),
    [
        ("beans_dropped", False, True, "no_confirmation"),
        ("emergency_stop", False, False, "incomplete"),
        ("autonomous_telemetry_sampler_failure", False, False, "incomplete"),
        ("beans_dropped", True, True, "confirmed"),
        ("emergency_stop", True, False, "confirmed"),
        ("autonomous_telemetry_sampler_failure", True, False, "confirmed"),
    ],
)
def test_first_boundary_reason_preserves_negative_prefix_coverage(
    tmp_path: Path, reason: str, confirmed: bool, coverage_complete: bool, outcome: str
) -> None:
    """Each first boundary preserves a processed prefix without masking fault coverage."""

    now = [10.0]
    observer, capture, sink, backend = setup_observer(tmp_path, now=now)
    window_recorded = Event()
    original_append = sink.append

    def append(record: Record) -> None:
        original_append(record)
        if isinstance(record, WindowRecord):
            window_recorded.set()

    sink.append = append
    backend.detect = lambda window: FirstCrackDetectorOutput(
        confirmed=confirmed, confidence=0.95 if confirmed else 0.1
    )
    observer.update_boundaries(charge_at=1, human_at=None)
    capture.windows.append(window())
    assert observer.start()
    try:
        assert window_recorded.wait(2)
        assert (sink.confirmed.is_set()) is confirmed
        ticket = object()
        observer.open_boundary(ticket, reason=reason)
        observer.resolve_boundary(ticket, cutoff=8.0)
        assert observer.reaped.wait(2)
    finally:
        observer.changed.set()
        assert observer.reaped.wait(2)

    assert observer.summary is not None
    assert observer.summary.counts.eligible_windows == 1
    assert observer.summary.coverage_complete is coverage_complete
    assert observer.summary.outcome == outcome
    if confirmed:
        assert observer.confirmation is not None
        assert sink.confirmed.is_set()


def test_pending_boundary_before_stop_claim_defers_normal_capture_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticket opened before the normal stop claim defers the one real stop."""

    stop_entered = Event()
    release_stop_entry = Event()
    pending_hold = Event()

    class CountedCapture(Capture):
        def __init__(self) -> None:
            super().__init__()
            self.stop_calls = 0

        def stop(self, *, timeout_seconds: float = 1) -> AudioCaptureSnapshot:
            self.stop_calls += 1
            return super().stop(timeout_seconds=timeout_seconds)

    observer, _, _, _ = setup_observer(tmp_path)
    capture = CountedCapture()
    observer.pipeline = capture
    original_stop = observer._stop_capture  # pyright: ignore[reportPrivateUsage]
    original_wait = observer.changed.wait

    def pause_before_stop_claim(*args: object, **kwargs: object) -> bool:
        stop_entered.set()
        assert release_stop_entry.wait(5)
        return original_stop(*args, **kwargs)

    def acknowledge_pending_hold(timeout: float | None = None) -> bool:
        if observer._pending_boundaries:  # pyright: ignore[reportPrivateUsage]
            pending_hold.set()
        return original_wait(timeout)

    monkeypatch.setattr(observer, "_stop_capture", pause_before_stop_claim)
    monkeypatch.setattr(observer.changed, "wait", acknowledge_pending_hold)
    observer.close(cutoff=8.0, reason="beans_dropped")
    assert observer.start()
    ticket = object()
    try:
        assert stop_entered.wait(2)
        observer.open_boundary(ticket, reason="emergency_stop")
        release_stop_entry.set()
        assert pending_hold.wait(2)
        assert capture.stop_calls == 0 and not capture.stopped
        observer.freeze_pending_boundary(ticket, reason="test_cleanup")
        observer.changed.set()
        assert observer.reaped.wait(2)
    finally:
        release_stop_entry.set()
        observer.freeze_pending_boundary(ticket, reason="test_cleanup")
        observer.changed.set()
        assert observer.reaped.wait(2)

    assert capture.stop_calls == 1 and capture.stopped


def test_pending_freeze_hides_provisional_cutoff_and_clean_coverage(tmp_path: Path) -> None:
    """A resolved later scalar remains non-public while another ticket is owned."""

    observer, _, _, _ = setup_observer(tmp_path)
    earlier, later = object(), object()
    observer.open_boundary(earlier, reason="beans_dropped")
    observer.open_boundary(later, reason="emergency_stop")
    observer.resolve_boundary(later, cutoff=8.0)
    observer.identity = observer.loader(observer.config.first_crack).identity
    observer._drain_complete = True
    observer._eligible_inferred = 1
    observer.freeze_pending_boundary(earlier, reason="test_cleanup")
    assert observer.summary is not None
    assert observer.summary.drop_cutoff.value is None
    assert observer.summary.drop_cutoff.null_reason == "not_yet_observed"
    assert not observer.summary.coverage_complete
    assert observer.summary.counts.undecided_eligible_windows == 0
