from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from coffee_roaster_mcp.first_crack_observation import (
    ClockMapping,
    Counts,
    IdentityPayload,
    IdentityRecord,
    Maybe,
    ModelIdentity,
    Record,
    Settings,
    Time,
    absent,
    present,
)


def test_clock_mapping_preserves_distinct_nonzero_origins() -> None:
    clock = ClockMapping(
        session_origin=100, session_utc=datetime(2026, 1, 1, tzinfo=UTC), recording_origin=102
    )
    mapped = clock.at(109)
    assert mapped.session_relative_seconds.value == 9
    assert mapped.recording_relative_seconds.value == 7
    assert mapped.utc.value == "2026-01-01T00:00:09+00:00"
    assert "100" not in mapped.model_dump_json()


@pytest.mark.parametrize(
    "timestamp", ["garbage", "2026-01-01T00:00:00", "2026-01-01T00:00:00+01:00"]
)
def test_time_rejects_non_utc_strings(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        Time(
            session_relative_seconds=present(0.0),
            recording_relative_seconds=absent("recorder_never_started"),
            utc=present(timestamp),
        )


@pytest.mark.parametrize(
    "values", [dict(value=None, null_reason=None), dict(value=1, null_reason="not_applicable")]
)
def test_maybe_is_exclusive(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Maybe[int].model_validate(values)


def test_missing_clock_never_becomes_zero() -> None:
    clock = ClockMapping(session_origin=100, session_utc=datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.at(100).recording_relative_seconds == absent("recorder_never_started")
    assert clock.at(100).session_relative_seconds == present(0.0)


@pytest.mark.parametrize(
    "values", [dict(observed_windows=-1), dict(eligible_windows=True), dict(unknown=1)]
)
def test_closed_counts_reject_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Counts.model_validate(values)


def test_tagged_identity_record_roundtrips_complete_payload() -> None:
    at = ClockMapping(session_origin=0, session_utc=datetime(2026, 1, 1, tzinfo=UTC)).at(2)
    identity = ModelIdentity(
        status="pending",
        repo_id="test/model",
        revision=absent("hub_default_revision"),
        precision="int8",
        onnx_model_filename="onnx/int8/model_quantized.onnx",
        feature_extractor_filename="onnx/int8/preprocessor_config.json",
        onnx_model_sha256=absent("load_not_completed"),
        feature_extractor_config_sha256=absent("load_not_completed"),
        failure=absent("not_applicable"),
    )
    settings = Settings(
        confidence_threshold=0.6,
        min_positive_windows=1,
        confirmation_window_seconds=20,
        window_seconds=1,
        effective_hop_seconds=1,
        sample_rate=16000,
    )
    row = IdentityRecord(
        session_id="session",
        generation=1,
        record_key="identity",
        accepted_at=at,
        payload=IdentityPayload(identity=identity, settings=settings, load_available_at=at),
    )
    assert TypeAdapter(Record).validate_json(row.model_dump_json()) == row  # pyright: ignore[reportArgumentType] - supported Pydantic Annotated schema
    wrong = row.model_dump(mode="json")
    wrong["record_type"] = "fc_observation_window"
    with pytest.raises(ValidationError):
        TypeAdapter(Record).validate_python(wrong)  # pyright: ignore[reportArgumentType] - supported Pydantic Annotated schema


def test_clock_reason_cannot_claim_no_confirmation() -> None:
    with pytest.raises(ValidationError, match="Null reason"):
        Time(
            session_relative_seconds=absent("no_confirmation"),
            recording_relative_seconds=absent("recorder_never_started"),
            utc=absent("clock_unavailable"),
        )


@pytest.mark.parametrize("code", ["/private/secret", "token=abcdef", "a" * 161])
def test_identity_failure_code_is_bounded_and_sanitised(code: str) -> None:
    with pytest.raises(ValidationError):
        ModelIdentity(
            status="failed",
            repo_id="test/model",
            revision=absent("hub_default_revision"),
            precision="int8",
            onnx_model_filename="model.onnx",
            feature_extractor_filename="preprocessor.json",
            onnx_model_sha256=absent("resolution_failed"),
            feature_extractor_config_sha256=absent("resolution_failed"),
            failure=present(code),
        )


def test_identity_digest_cannot_use_unrelated_null_reason() -> None:
    with pytest.raises(ValidationError, match="Null reason"):
        ModelIdentity(
            status="pending",
            repo_id="test/model",
            revision=absent("hub_default_revision"),
            precision="int8",
            onnx_model_filename="model.onnx",
            feature_extractor_filename="preprocessor.json",
            onnx_model_sha256=absent("no_confirmation"),
            feature_extractor_config_sha256=absent("load_not_completed"),
            failure=absent("not_applicable"),
        )


def test_clock_mapping_rejects_naive_or_non_utc_origin() -> None:
    from datetime import timedelta, timezone

    for instant in [
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    ]:
        with pytest.raises(ValidationError, match="explicit UTC"):
            ClockMapping(session_origin=0, session_utc=instant)


def test_not_inferred_tag_requires_truthful_nulls_and_ineligibility() -> None:
    from coffee_roaster_mcp.first_crack_observation import WindowPayload

    clock = ClockMapping(session_origin=0, session_utc=datetime(2026, 1, 1, tzinfo=UTC))
    missing = Time(
        session_relative_seconds=absent("not_applicable"),
        recording_relative_seconds=absent("not_applicable"),
        utc=absent("not_applicable"),
    )
    row = WindowPayload(
        sequence=1,
        probability=absent("not_applicable"),
        positive_window_count=0,
        window_status="not_inferred",
        window_start=clock.at(1),
        window_end=clock.at(2),
        decision_available_at=missing,
        inference_duration_ms=0,
        queue_depth=0,
        capture_dropped_windows=0,
        overflow_count_last_minute=0,
        eligibility="ineligible_pre_charge",
        availability_class="drop_not_observed",
        human_mark_seen_at_decision=False,
    )
    assert row.window_status == "not_inferred"
    for changes in [
        dict(probability=present(0.1)),
        dict(probability=absent("inference_failed")),
        dict(eligibility="eligible"),
        dict(decision_available_at=clock.at(3)),
        dict(inference_duration_ms=1),
        dict(human_mark_seen_at_decision=True),
    ]:
        with pytest.raises(ValidationError, match="not_inferred"):
            WindowPayload.model_validate({**row.model_dump(), **changes})


def test_domain_and_observer_modules_cannot_import_control_capabilities() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "coffee_roaster_mcp"
    for filename, forbidden in [
        (
            "first_crack_observation.py",
            {
                "os",
                "pathlib",
                "threading",
                "time",
                "subprocess",
                "socket",
                "httpx",
                "requests",
                "coffee_roaster_mcp.config",
                "coffee_roaster_mcp.audio",
                "coffee_roaster_mcp.artifacts",
                "coffee_roaster_mcp.detector",
                "coffee_roaster_mcp.session",
                "coffee_roaster_mcp.drivers",
                "coffee_roaster_mcp.mcp_server",
            },
        ),
        (
            "first_crack_observer.py",
            {
                "coffee_roaster_mcp.session",
                "coffee_roaster_mcp.drivers",
                "coffee_roaster_mcp.controls",
                "coffee_roaster_mcp.mcp_server",
            },
        ),
    ]:
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(module in forbidden for module in modules)


def test_observer_schema_rejects_canonical_detected_status() -> None:
    from coffee_roaster_mcp.config import AppConfig
    from coffee_roaster_mcp.first_crack_observation import ObservationSnapshot
    from coffee_roaster_mcp.first_crack_observer import configured_identity, observation_settings

    config = AppConfig()
    with pytest.raises(ValidationError):
        ObservationSnapshot.model_validate(
            dict(
                observer_state="detected",
                identity=configured_identity(config.first_crack),
                settings=observation_settings(config),
                first_confirmation=absent("no_confirmation"),
                frozen_summary=absent(),
                durability="pending",
                worker_reaped=False,
            )
        )
