"""Bounded capture-evidence publication and read tests."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import coffee_roaster_mcp.capture_evidence as capture_evidence
from coffee_roaster_mcp.audio import (
    AdditionalRecordingDevice,
    AudioInput,
    EvidenceRoastRecorder,
    MultiDeviceRoastRecorder,
    RoastAudioRecorder,
)
from coffee_roaster_mcp.capture_evidence import CaptureEvidencePublisher
from coffee_roaster_mcp.mcp_server import (  # pyright: ignore[reportPrivateUsage]
    CaptureEvidenceInputResult,
    CaptureEvidenceResult,
    ServerContext,
    _read_capture_evidence,  # pyright: ignore[reportPrivateUsage]
)
from coffee_roaster_mcp.session import RoastSession, RoastSessionStore


def _context(store: RoastSessionStore, publisher: CaptureEvidencePublisher) -> ServerContext:
    """Build the minimal inert reader context without runtime dependencies."""
    return cast(
        ServerContext,
        SimpleNamespace(
            capture_evidence_configured=True,
            config=SimpleNamespace(recording=SimpleNamespace(devices=())),
            session_store=store,
            capture_evidence_publisher=publisher,
        ),
    )


def test_subject_sequence_advances_and_copy_retains_it() -> None:
    """Replacement ownership remains distinct even when test ids are reused."""
    ids = iter(("same", "same"))
    store = RoastSessionStore(session_id_factory=lambda: next(ids))

    first = store.start_session()
    first_subject = store.try_get_capture_subject()
    store.stop_session()
    second = store.start_session_snapshot()
    second_subject = store.try_get_capture_subject()

    assert first.id == second.id == "same"
    assert first_subject is not None and second_subject is not None
    assert first_subject.ownership_sequence == first.capture_ownership_sequence == 1
    assert second_subject.ownership_sequence == second.capture_ownership_sequence == 2


def test_capture_ownership_sequence_preserves_positional_and_equality_contract() -> None:
    """The internal ownership fence is neither positional nor value identity."""
    from datetime import UTC, datetime

    created = datetime(2026, 1, 1, tzinfo=UTC)
    original = RoastSession("session", created, 1.0, "roast")
    successor = RoastSession("session", created, 1.0, "roast", capture_ownership_sequence=9)

    assert original.purpose == "roast"
    assert original == successor


def test_reader_returns_observed_only_for_exact_current_subject() -> None:
    """A successful snapshot is bracketed by matching exact store ownership."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    publisher.open_input(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        reported_rate_hz=16_000.0,
        wav_header_rate_hz=16_000.0,
    )
    publisher.publish_progress(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=0,
        accepted_frames=0,
        flushed_frames=0,
    )

    observed = _read_capture_evidence(_context(store, publisher), session.id)
    wrong = _read_capture_evidence(_context(store, publisher), "other")

    assert observed.outcome == "observed"
    assert observed.session_id == session.id
    assert observed.inputs[0].input_frames == observed.inputs[0].accepted_frames == 0
    assert observed.inputs[0].identity == "unresolved"
    assert wrong.outcome == "refused"
    assert wrong.reason == "wrong_session"
    assert wrong.inputs == ()


def test_stale_owner_updates_cannot_replace_successor_projection() -> None:
    """Old recorder progress and close cannot alter a newer store owner."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="old", ownership_sequence=1, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    assert publisher.publish_setup(
        session_id="new", ownership_sequence=2, recorder_generation=1, requested_rates_hz=(16_000,)
    )
    publisher.open_input(
        session_id="old",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        reported_rate_hz=16_000.0,
    )
    publisher.close_input(
        session_id="old",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )

    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    assert snapshot.session_id == "new"
    assert snapshot.inputs[0].identity == "unopened"


def test_setup_failure_is_attributable_to_its_exact_current_owner() -> None:
    """Setup failure changes no ownership coordinate or prior successor state."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    publisher.publish_setup_failure(
        session_id="session", ownership_sequence=1, recorder_generation=1
    )
    snapshot = publisher.try_snapshot()

    assert snapshot is not False and snapshot is not None
    assert snapshot.setup_failed is True


def test_lost_setup_failure_is_publicly_unavailable_for_its_current_owner() -> None:
    """A contended disposition matches coordinates, not its new field value."""
    store = RoastSessionStore()
    session = store.start_session()
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id=session.id,
        ownership_sequence=session.capture_ownership_sequence,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher._lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    try:
        publisher.publish_setup_failure(
            session_id=session.id,
            ownership_sequence=session.capture_ownership_sequence,
            recorder_generation=1,
        )
    finally:
        publisher._lock.release()  # pyright: ignore[reportPrivateUsage]

    result = _read_capture_evidence(_context(store, publisher), session.id)

    assert result.outcome == "unavailable"
    assert result.reason == "publication_contended"


def test_absolute_progress_restores_lost_counts_before_flush() -> None:
    """A missed delta cannot make a later known flush exceed accepted frames."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher._lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    try:
        assert not publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=1_600,
            accepted_frames=1_600,
            flushed_frames=0,
        )
    finally:
        publisher._lock.release()  # pyright: ignore[reportPrivateUsage]
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=1_600,
        accepted_frames=1_600,
        flushed_frames=1_600,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].accepted_frames == snapshot.inputs[0].flushed_frames == 1_600


def test_absolute_progress_restores_a_lost_input_read() -> None:
    """A later absolute observation repairs an ordinary contended read update."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher._lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    try:
        assert not publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=800,
            accepted_frames=800,
            flushed_frames=0,
        )
    finally:
        publisher._lock.release()  # pyright: ignore[reportPrivateUsage]
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=1_600,
        accepted_frames=1_600,
        flushed_frames=0,
    )

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].input_frames == snapshot.inputs[0].accepted_frames == 1_600


def test_lost_current_terminal_update_latches_unavailability_without_stale_poisoning() -> None:
    """A lost current failure is safe, while stale failure cannot hide a successor."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="current",
        ownership_sequence=2,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="current",
        ownership_sequence=2,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher._lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    try:
        publisher.fail_input(
            session_id="current",
            ownership_sequence=2,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="write_failed",
        )
    finally:
        publisher._lock.release()  # pyright: ignore[reportPrivateUsage]
    assert publisher.try_snapshot() is False

    assert publisher.publish_setup(
        session_id="successor",
        ownership_sequence=3,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.session_id == "successor"


def test_stale_reopen_failure_does_not_latch_current_generation() -> None:
    """A stale terminal claim after a reopen remains unable to poison current evidence."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=2,
    )
    assert publisher._lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    try:
        publisher.fail_input(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            failure="read_failed",
        )
    finally:
        publisher._lock.release()  # pyright: ignore[reportPrivateUsage]
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].input_generation == 2
    assert snapshot.inputs[0].state == "capturing"


def test_confirmed_close_does_not_infer_flush_progress() -> None:
    """A terminal disposition cannot manufacture a writer flush count."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    assert publisher.publish_progress(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        input_frames=4,
        accepted_frames=4,
        flushed_frames=0,
    )
    publisher.close_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        confirmed=True,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].state == "closed"
    assert snapshot.inputs[0].accepted_frames == 4
    assert snapshot.inputs[0].flushed_frames == 0


def test_routine_concurrent_progress_does_not_latch_publication() -> None:
    """A normal concurrent producer and reader retain an observable projection."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
    )
    release = Event()
    finished = Event()

    def publish() -> None:
        release.wait(timeout=1)
        publisher.publish_progress(
            session_id="session",
            ownership_sequence=1,
            recorder_generation=1,
            slot=1,
            input_generation=1,
            input_frames=3,
            accepted_frames=3,
            flushed_frames=0,
        )
        finished.set()

    worker = Thread(target=publish)
    worker.start()
    release.set()
    assert finished.wait(timeout=1)
    worker.join(timeout=1)
    assert not worker.is_alive()
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].accepted_frames == 3


def test_single_recorder_publishes_actual_writer_receipts_and_close(tmp_path: Path) -> None:
    """The single-stream path reports only its writer's absolute receipts."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=200,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert (snapshot.inputs[0].input_frames, snapshot.inputs[0].accepted_frames) == (160, 160)
    assert snapshot.inputs[0].flushed_frames == 0

    evidence.write_samples([0.0] * 40)
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert snapshot.inputs[0].flushed_frames == 200

    evidence.close()
    evidence.write_samples([0.0])
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (200, 200, 200)
    assert (row.state, row.failure) == ("closed", None)


def test_single_recorder_final_flush_failure_publishes_unconfirmed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final single-stream flush failure cannot retain healthy evidence."""
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    recorder = RoastAudioRecorder(
        wav_path=tmp_path / "roast.wav",
        sidecar_path=tmp_path / "roast.json",
        sample_rate=16_000,
        session_id="session",
        flush_sample_threshold=200,
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000,),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(recorder._writer, "_flush_locked", fail_final_flush)  # pyright: ignore[reportPrivateUsage] - writer-boundary witness
    with pytest.raises(OSError, match="final flush"):
        evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    row = snapshot.inputs[0]
    assert (row.input_frames, row.accepted_frames, row.flushed_frames) == (160, 160, 0)
    assert (row.state, row.failure) == ("failed", "close_unconfirmed")


def test_multi_recorder_final_flush_failures_are_per_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detector and additional writers retain independent terminal receipts."""

    class OneChunkInput(AudioInput):
        def __init__(self) -> None:
            self.read_once = Event()
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self.read_once.set()
            return [0.0] * 40

        def close(self) -> None:
            return None

    additional_input = OneChunkInput()
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: additional_input,
        flush_sample_threshold=200,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    assert additional_input.read_once.wait(timeout=1)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(recorder._detector_writer, "_flush_locked", fail_final_flush)  # pyright: ignore[reportPrivateUsage] - detector writer-boundary witness
    with pytest.raises(OSError, match="final flush"):
        evidence.close()
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.state, detector.failure) == ("failed", "close_unconfirmed")
    # The raised detector close leaves additional finalisation to the existing
    # owner path; it is not falsely claimed as closed by this failure.
    assert additional.state == "capturing"


def test_additional_final_flush_failure_is_not_misreported_as_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An additional writer's final flush remains an unconfirmed close receipt."""

    class OneChunkInput(AudioInput):
        def __init__(self) -> None:
            self.read_once = Event()
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self.read_once.set()
            return [0.0] * 40

        def close(self) -> None:
            return None

    additional_input = OneChunkInput()
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: additional_input,
        flush_sample_threshold=200,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence.begin()
    evidence.observe_detector_input(backend_input_id=None, reported_rate_hz=None)
    evidence.write_samples([0.0] * 160)
    assert additional_input.read_once.wait(timeout=1)

    def fail_final_flush() -> None:
        raise OSError("synthetic final flush failure")

    monkeypatch.setattr(
        recorder._additional_streams[0].writer,  # pyright: ignore[reportPrivateUsage]
        "_flush_locked",
        fail_final_flush,
    )
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.state, detector.failure) == ("closed", None)
    assert (additional.input_frames, additional.accepted_frames, additional.flushed_frames) == (
        40,
        40,
        0,
    )
    assert (additional.state, additional.failure) == ("failed", "close_unconfirmed")


def test_token_collision_is_unresolved_but_same_backend_is_aliased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opaque collisions disclose neither backend identity nor a false distinction."""

    def identical_token(byte_count: int) -> str:
        """Return a forced opaque token for collision testing."""
        assert byte_count == 16
        return "a" * 32

    monkeypatch.setattr(capture_evidence.secrets, "token_hex", identical_token)
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=1,
    )
    assert publisher.open_input(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        backend_input_id=2,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert [(row.identity, row.opened_input_token) for row in snapshot.inputs] == [
        ("unresolved", None),
        ("unresolved", None),
    ]

    assert publisher.publish_setup(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    assert publisher.open_input(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        slot=1,
        input_generation=1,
        backend_input_id=7,
    )
    assert publisher.open_input(
        session_id="next",
        ownership_sequence=2,
        recorder_generation=1,
        slot=2,
        input_generation=1,
        backend_input_id=7,
    )
    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    assert [row.identity for row in snapshot.inputs] == ["aliased", "aliased"]
    assert snapshot.inputs[0].opened_input_token == snapshot.inputs[1].opened_input_token


def test_closed_result_grammar_rejects_crossed_outcomes_and_token_identity() -> None:
    """Public models reject crossed outcome/reason and token identity combinations."""
    with pytest.raises(ValidationError):
        CaptureEvidenceResult(
            capture_protocol="capture_evidence/v1",
            intent="current_session",
            outcome="refused",
            reason="store_contended",
        )
    with pytest.raises(ValidationError):
        CaptureEvidenceResult(
            capture_protocol="capture_evidence/v1",
            intent="current_session",
            outcome="unavailable",
            reason="capture_pending",
        )
    with pytest.raises(ValidationError):
        CaptureEvidenceInputResult(
            slot=1,
            role="detector_teed",
            input_generation=1,
            opened_input_token="a" * 32,
            identity="unresolved",
            requested_rate_hz=16_000,
            reported_rate_hz=None,
            wav_header_rate_hz=None,
            input_frames=0,
            accepted_frames=0,
            flushed_frames=0,
            state="capturing",
            failure=None,
        )


def test_multi_input_recorder_publishes_actual_aliases_and_boundaries(tmp_path: Path) -> None:
    """Independent reads publish their own identity and writer-boundary counts."""

    class FakeInput(AudioInput):
        def __init__(self, frames: int, first_read: Event) -> None:
            self._frames = frames
            self._first_read = first_read
            self._read = False

        def read_samples(self, sample_count: int) -> list[float]:
            del sample_count
            if self._read:
                return []
            self._read = True
            self._first_read.set()
            return [0.0] * self._frames

        def capture_evidence_identity(self) -> tuple[int, float]:
            return (7, 16_000.0)

        def close(self) -> None:
            return None

    first_read = Event()
    inputs = iter((FakeInput(4_000, first_read),))
    recorder = MultiDeviceRoastRecorder(
        detector_wav_path=tmp_path / "detector.wav",
        detector_device_label=None,
        sidecar_path=tmp_path / "recording.json",
        sample_rate=16_000,
        session_id="session",
        additional_devices=(
            AdditionalRecordingDevice("additional", tmp_path / "additional.wav", 16_000),
        ),
        additional_input_factory=lambda _device: next(inputs),
        flush_sample_threshold=1,
    )
    publisher = CaptureEvidencePublisher()
    assert publisher.publish_setup(
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )
    evidence = EvidenceRoastRecorder(
        recorder=recorder,
        publisher=publisher,
        session_id="session",
        ownership_sequence=1,
        recorder_generation=1,
        requested_rates_hz=(16_000, 16_000),
    )

    evidence.begin()
    evidence.observe_detector_input(backend_input_id=7, reported_rate_hz=16_000.0)
    evidence.write_samples([0.0] * 1_600)
    assert first_read.wait(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = publisher.try_snapshot()
        if (
            snapshot is not False
            and snapshot is not None
            and snapshot.inputs[1].input_frames == 4_000
        ):
            break
        time.sleep(0.001)
    else:
        pytest.fail("Timed out waiting for independently captured evidence.")
    evidence.close()

    snapshot = publisher.try_snapshot()
    assert snapshot is not False and snapshot is not None
    detector, additional = snapshot.inputs
    assert (detector.identity, additional.identity) == ("aliased", "aliased")
    assert detector.opened_input_token == additional.opened_input_token
    assert (detector.input_frames, additional.input_frames) == (1_600, 4_000)
    assert (detector.accepted_frames, additional.accepted_frames) == (1_600, 4_000)
    assert (detector.flushed_frames, additional.flushed_frames) == (1_600, 4_000)
