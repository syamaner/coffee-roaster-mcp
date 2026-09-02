"""Test pure clean-wheel first-crack acceptance helpers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest


def _module() -> ModuleType:
    """Load the standalone script without running its command entry point."""
    path = Path(__file__).parents[1] / "scripts" / "acceptance_first_crack_wheel.py"
    spec = importlib.util.spec_from_file_location("wheel_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheel_metadata_rejects_removed_frontend_dependency(tmp_path: Path) -> None:
    """The pre-install wheel check rejects Transformers metadata."""
    module = _module()
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package-0.dist-info/METADATA", "Requires-Dist: transformers (>=4.40)\n")
    metadata = module._wheel_metadata(wheel)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="banned packages"):
        module._assert_no_banned_requirements(metadata.get_all("Requires-Dist"))  # type: ignore[attr-defined]


def test_wheel_metadata_rejects_banned_extra_requirement(tmp_path: Path) -> None:
    """The wheel audit rejects banned requirements even when an extra guards them."""
    module = _module()
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "package-0.dist-info/METADATA",
            'Requires-Dist: torchaudio; extra == "frontend"\n',
        )
    metadata = module._wheel_metadata(wheel)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="banned packages"):
        module._assert_no_banned_requirements(metadata.get_all("Requires-Dist"))  # type: ignore[attr-defined]


def test_installed_dependency_evidence_uses_active_state_not_optional_metadata() -> None:
    """An unrelated optional declaration does not make a banned package installed."""
    module = _module()

    evidence = module._installed_dependency_evidence(  # type: ignore[attr-defined]
        [],
        {"torch": False, "torchaudio": False, "transformers": False},
    )

    assert evidence == {
        "banned_distributions": [],
        "import_specs": {"torch": False, "torchaudio": False, "transformers": False},
    }


def test_sanitized_child_environment_strips_external_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Acceptance children use only the temporary Hugging Face cache and safe platform keys."""
    module = _module()
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "LANG": "en_GB.UTF-8",
            "PYTHONPATH": "/unsafe/pythonpath",
            "COFFEE_CONFIG": "/unsafe/config",
            "HF_ENDPOINT": "https://unsafe.example",
            "HF_HOME": "/unsafe/cache",
            "HTTPS_PROXY": "https://user:password@proxy.example",
        },
    )

    environment = module._sanitized_child_environment(tmp_path / "hf")  # type: ignore[attr-defined]

    assert environment == {
        "PATH": "/usr/bin",
        "LANG": "en_GB.UTF-8",
        "HF_HOME": str(tmp_path / "hf"),
        "HF_HUB_CACHE": str(tmp_path / "hf" / "hub"),
    }


def test_installed_replay_invocation_isolated_to_acceptance_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fresh-wheel replay child receives only the acceptance-owned environment."""
    module = _module()
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "HF_TOKEN": "secret",
            "HF_ENDPOINT": "unsafe",
            "PIP_INDEX_URL": "unsafe",
        },
    )
    cache = tmp_path / "acceptance" / "huggingface-cache"
    command, environment = module._installed_replay_invocation(  # type: ignore[attr-defined]
        tmp_path / "venv" / "bin" / "python", tmp_path / "model", tmp_path / "replay", cache
    )

    assert command[-2:] == ["--huggingface-cache", cache]
    assert environment == {
        "PATH": "/usr/bin",
        "HF_HOME": str(cache),
        "HF_HUB_CACHE": str(cache / "hub"),
    }
    assert Path(environment["HF_HOME"]).is_relative_to(tmp_path)
    assert Path(environment["HF_HUB_CACHE"]).is_relative_to(tmp_path)


def test_parent_run_without_environment_keeps_ambient_process_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Build/install-style parent commands do not gain a sanitised child environment."""
    module = _module()
    captured: dict[str, object] = {}

    def complete(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", complete)
    module._run(["parent-command"], tmp_path)  # type: ignore[attr-defined]

    assert captured["env"] is None


def test_run_reports_failed_command_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing acceptance subprocess retains its command, status, and output."""
    module = _module()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            7, ["tool", "argument"], "standard output", "standard error"
        )

    monkeypatch.setattr(module.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="exit status 7") as error:
        module._run(["tool", "argument"], tmp_path)  # type: ignore[attr-defined]

    assert "tool argument" in str(error.value)
    assert "standard output" in str(error.value)
    assert "standard error" in str(error.value)


def test_artifact_preflight_rejects_changed_size(tmp_path: Path) -> None:
    """Model verification rejects an unexpected artifact size before hashing."""
    module = _module()
    filename = next(iter(module.MODEL_ARTIFACTS))  # type: ignore[attr-defined]
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")

    with pytest.raises(RuntimeError, match="unexpected size"):
        module._verify_artifacts({filename: path})  # type: ignore[attr-defined]


def test_artifact_preflight_rejects_changed_hash(tmp_path: Path) -> None:
    """Model verification fails before a session may use changed bytes."""
    module = _module()
    paths: dict[str, Path] = {}
    for filename, (size, _digest) in module.MODEL_ARTIFACTS.items():  # type: ignore[attr-defined]
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        paths[filename] = path
    with pytest.raises(RuntimeError, match="SHA-256"):
        module._verify_artifacts(paths)  # type: ignore[attr-defined]


def test_export_containment_accepts_canonical_path_through_symlink_alias(tmp_path: Path) -> None:
    """Canonical exported paths remain inside a symlinked temporary work directory."""
    module = _module()
    canonical_work_dir = tmp_path / "canonical-work"
    canonical_work_dir.mkdir()
    alias = tmp_path / "work-alias"
    alias.symlink_to(canonical_work_dir, target_is_directory=True)
    jsonl = canonical_work_dir / "roast.jsonl"
    summary = canonical_work_dir / "summary.json"
    jsonl.write_text("", encoding="utf-8")
    summary.write_text('{"first_crack_model": {}}', encoding="utf-8")

    result = module._assert_export(  # type: ignore[attr-defined]
        {"jsonl_path": str(jsonl), "summary_path": str(summary)}, alias
    )

    assert result == {"first_crack_model": {}}


def test_export_containment_rejects_real_escape(tmp_path: Path) -> None:
    """Canonical containment still rejects an exported file outside the work directory."""
    module = _module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    jsonl = outside / "roast.jsonl"
    summary = outside / "summary.json"
    jsonl.write_text("", encoding="utf-8")
    summary.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="escaped"):
        module._assert_export(  # type: ignore[attr-defined]
            {"jsonl_path": str(jsonl), "summary_path": str(summary)}, work_dir
        )


def _confirmation_evidence(
    module: ModuleType,
    tmp_path: Path,
    rows: Sequence[Mapping[str, object]] | None = None,
    *,
    state_onset: object = 105.0,
    confidence: object = 0.6,
) -> dict[str, object]:
    """Write temporary JSONL and return the pure session-confirmation evidence."""
    jsonl_path = tmp_path / "roast.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(row) for row in rows or _confirmation_rows()), encoding="utf-8"
    )
    return module._confirmation_evidence(  # type: ignore[attr-defined]
        jsonl_path,
        "current",
        {"first_crack_monotonic_seconds": state_onset},
        {"first_crack_model": {"confidence": confidence}},
    )


def _confirmation_rows(
    *,
    beans_added: object = 100.0,
    exported_onset: object = 105.0,
    absolute_onset: object = 500.0,
    absolute_confirmation: object = 510.0,
    payload: Mapping[str, object] | None = None,
    confirming_confidence: object = 0.72,
    confirmed: object = True,
) -> list[dict[str, object]]:
    """Return valid session events whose confirmation is 15 seconds after beans added."""
    first_crack_payload = payload or {
        "detected_at_monotonic_seconds": absolute_onset,
        "confirmed_at_monotonic_seconds": absolute_confirmation,
        "confidence": 0.6,
    }
    return [
        {
            "session_id": "current",
            "type": "event",
            "kind": "beans_added",
            "monotonic_seconds": beans_added,
        },
        {
            "session_id": "current",
            "type": "event",
            "kind": "first_crack_detected",
            "monotonic_seconds": exported_onset,
            "payload": first_crack_payload,
        },
        {
            "session_id": "current",
            "type": "fc_window",
            "confirmed": False,
            "confidence": 0.31,
            "window_sequence_number": 1,
        },
        {
            "session_id": "current",
            "type": "fc_window",
            "confirmed": False,
            "confidence": 0.44,
            "window_sequence_number": 2,
        },
        {
            "session_id": "current",
            "type": "fc_window",
            "confirmed": confirmed,
            "confidence": confirming_confidence,
            "window_sequence_number": 3,
        },
    ]


def test_confirmation_evidence_uses_session_formula_and_filters_rows(tmp_path: Path) -> None:
    """Only current-session events contribute to the exported confirmation evidence."""
    module = _module()
    rows = [
        {
            "session_id": "current",
            "type": "telemetry",
            "kind": "beans_added",
            "monotonic_seconds": 1.0,
            "confirmed": True,
        },
        {
            "session_id": "other",
            "type": "event",
            "kind": "first_crack_detected",
            "monotonic_seconds": 1.0,
        },
        {
            "session_id": "other",
            "type": "fc_window",
            "confirmed": True,
            "confidence": 0.1,
            "window_sequence_number": 1,
        },
        *_confirmation_rows(),
    ]

    evidence = _confirmation_evidence(module, tmp_path, rows)

    assert evidence["confirmation"] == {
        "beans_added_monotonic_seconds": 100.0,
        "confirmation_session_monotonic_seconds": 115.0,
        "detected_at_monotonic_seconds": 500.0,
        "confirmed_at_monotonic_seconds": 510.0,
        "onset_to_confirmation_seconds": 10.0,
        "confirmation_seconds_after_beans_added": 15.0,
        "minimum_seconds_after_beans_added": 3.82710390663442,
        "maximum_seconds_after_beans_added": 21.0,
        "strict_maximum_seconds_after_beans_added": 20.017,
    }
    assert evidence["onset"] == {
        "beans_added_event_count": 1,
        "first_crack_detected_event_count": 1,
        "exported_monotonic_seconds": 105.0,
        "state_monotonic_seconds": 105.0,
        "onset_not_after_confirmation": True,
        "export_state_consistent": True,
        "onset_is_not_latency_gate": True,
    }
    assert evidence["confidence"] == {
        "confirming_confidence": 0.72,
        "confirming_confidence_source": "fc_window_confirmed_row",
        "confirming_window_sequence_number": 3,
        "current_session_fc_window_row_count": 3,
        "confirmed_fc_window_row_count": 1,
        "minimum": 0.6,
        "onset_candidate_payload_confidence": 0.6,
        "onset_candidate_summary_confidence": 0.6,
        "onset_candidate_confidence_is_diagnostic": True,
    }
    assert evidence["observed_onset_after_t0"] == 5.0


@pytest.mark.parametrize(
    ("absolute_confirmation", "expected_confirmation_after_t0"),
    [(503.8271039066344, 3.82710390663442), (520.0169, 20.0169)],
)
def test_confirmation_evidence_accepts_inclusive_minimum_and_strict_maximum_margin(
    tmp_path: Path, absolute_confirmation: float, expected_confirmation_after_t0: float
) -> None:
    """The accepted interval includes its minimum and excludes only the strict maximum."""
    module = _module()

    evidence = _confirmation_evidence(
        module,
        tmp_path,
        _confirmation_rows(exported_onset=100.0, absolute_confirmation=absolute_confirmation),
        state_onset=100.0,
    )

    confirmation = cast(dict[str, object], evidence["confirmation"])
    observed = confirmation["confirmation_seconds_after_beans_added"]
    assert isinstance(observed, float)
    if expected_confirmation_after_t0 == 3.82710390663442:
        assert observed == expected_confirmation_after_t0
    else:
        assert observed < 20.017


def test_confirmation_evidence_accepts_exact_minimum_confidence(tmp_path: Path) -> None:
    """The confirmation confidence gate accepts exactly 0.6."""
    evidence = _confirmation_evidence(
        _module(), tmp_path, _confirmation_rows(confirming_confidence=0.6), confidence=0.6
    )

    assert evidence["confidence"] == {
        "confirming_confidence": 0.6,
        "confirming_confidence_source": "fc_window_confirmed_row",
        "confirming_window_sequence_number": 3,
        "current_session_fc_window_row_count": 3,
        "confirmed_fc_window_row_count": 1,
        "minimum": 0.6,
        "onset_candidate_payload_confidence": 0.6,
        "onset_candidate_summary_confidence": 0.6,
        "onset_candidate_confidence_is_diagnostic": True,
    }


def test_confirmation_evidence_gates_confirming_window_not_onset_candidate(tmp_path: Path) -> None:
    """A low diagnostic onset candidate does not fail a passing confirming window."""
    rows = _confirmation_rows(
        payload={
            "detected_at_monotonic_seconds": 500.0,
            "confirmed_at_monotonic_seconds": 510.0,
            "confidence": 0.1,
        }
    )

    evidence = _confirmation_evidence(_module(), tmp_path, rows, confidence=0.1)

    confidence = cast(dict[str, object], evidence["confidence"])
    assert confidence["confirming_confidence"] == 0.72
    assert confidence["onset_candidate_payload_confidence"] == 0.1
    assert confidence["onset_candidate_summary_confidence"] == 0.1


def test_confirmation_evidence_rejects_low_confirming_window_despite_strong_onset(
    tmp_path: Path,
) -> None:
    """Only the confirmed first-crack window confidence is threshold-gated."""
    with pytest.raises(RuntimeError, match="confirming window confidence"):
        _confirmation_evidence(
            _module(),
            tmp_path,
            _confirmation_rows(
                payload={
                    "detected_at_monotonic_seconds": 500.0,
                    "confirmed_at_monotonic_seconds": 510.0,
                    "confidence": 0.9,
                },
                confirming_confidence=0.599,
            ),
            confidence=0.9,
        )


@pytest.mark.parametrize("confirmed", [None, 1, 0, "true"])
def test_confirmation_evidence_rejects_non_boolean_confirmed(
    tmp_path: Path, confirmed: object
) -> None:
    """Every current-session first-crack window must use a JSON boolean confirmed flag."""
    with pytest.raises(RuntimeError, match="line 5"):
        _confirmation_evidence(_module(), tmp_path, _confirmation_rows(confirmed=confirmed))


def test_confirmation_evidence_rejects_absent_or_duplicate_confirming_windows(
    tmp_path: Path,
) -> None:
    """The current session must have exactly one confirmed first-crack window."""
    rows = _confirmation_rows(confirmed=False)
    with pytest.raises(RuntimeError, match="exactly one confirmed"):
        _confirmation_evidence(_module(), tmp_path, rows)
    rows = _confirmation_rows()
    rows.append(dict(rows[-1]))
    with pytest.raises(RuntimeError, match="exactly one confirmed"):
        _confirmation_evidence(_module(), tmp_path, rows)


def test_confirmation_evidence_rejects_malformed_non_confirming_window(tmp_path: Path) -> None:
    """A malformed false window is not ignored while searching for the true window."""
    rows = _confirmation_rows()
    rows[2]["confirmed"] = "false"

    with pytest.raises(RuntimeError, match="line 3"):
        _confirmation_evidence(_module(), tmp_path, rows)


@pytest.mark.parametrize("value", [None, "0.6", True, float("nan"), float("inf")])
def test_confirmation_evidence_rejects_invalid_confirming_confidence(
    tmp_path: Path, value: object
) -> None:
    """Confirming-window confidence must be finite numeric data, not a stand-in."""
    with pytest.raises(RuntimeError, match="confirmed fc_window.confidence"):
        _confirmation_evidence(_module(), tmp_path, _confirmation_rows(confirming_confidence=value))


def test_confirmation_evidence_rejects_missing_confirming_window_fields(tmp_path: Path) -> None:
    """Required confirmation fields fail clearly when omitted from the window row."""
    rows = _confirmation_rows()
    del rows[-1]["confirmed"]
    with pytest.raises(RuntimeError, match="line 5"):
        _confirmation_evidence(_module(), tmp_path, rows)
    rows = _confirmation_rows()
    del rows[-1]["confidence"]
    with pytest.raises(RuntimeError, match="confirmed fc_window.confidence"):
        _confirmation_evidence(_module(), tmp_path, rows)


def test_confirmation_evidence_rejects_onset_candidate_confidence_mismatch(tmp_path: Path) -> None:
    """The two diagnostic onset-candidate confidence copies must exactly agree."""
    with pytest.raises(RuntimeError, match="payload and summary confidence differ"):
        _confirmation_evidence(_module(), tmp_path, confidence=0.7)


def test_confirmation_evidence_preserves_event_validation_precedence(tmp_path: Path) -> None:
    """An existing timing failure wins before malformed first-crack window data."""
    rows = _confirmation_rows(absolute_confirmation=499.0)
    rows[-1]["confirmed"] = "true"

    with pytest.raises(RuntimeError, match="onset is after confirmation"):
        _confirmation_evidence(_module(), tmp_path, rows)


def test_confirmation_evidence_treats_window_sequence_as_optional_diagnostic(
    tmp_path: Path,
) -> None:
    """Window sequence metadata is reported when present and never acceptance-gated."""
    rows = _confirmation_rows()
    del rows[-1]["window_sequence_number"]

    evidence = _confirmation_evidence(_module(), tmp_path, rows)

    confidence = cast(dict[str, object], evidence["confidence"])
    assert confidence["confirming_window_sequence_number"] is None


@pytest.mark.parametrize("kind", ["beans_added", "first_crack_detected"])
def test_confirmation_evidence_rejects_missing_or_duplicate_events(
    tmp_path: Path, kind: str
) -> None:
    """Each required event must occur exactly once for the current session."""
    module = _module()
    rows = _confirmation_rows()
    rows = [row for row in rows if row.get("kind") != kind]
    with pytest.raises(RuntimeError, match=kind):
        _confirmation_evidence(module, tmp_path, rows)
    rows = _confirmation_rows()
    rows.append(dict(next(row for row in rows if row.get("kind") == kind)))
    with pytest.raises(RuntimeError, match=kind):
        _confirmation_evidence(module, tmp_path, rows)


@pytest.mark.parametrize(
    "field", ["detected_at_monotonic_seconds", "confirmed_at_monotonic_seconds"]
)
def test_confirmation_evidence_rejects_missing_payload_stamps(tmp_path: Path, field: str) -> None:
    """Both absolute payload timestamps are mandatory."""
    module = _module()
    payload = {"detected_at_monotonic_seconds": 500.0, "confirmed_at_monotonic_seconds": 510.0}
    del payload[field]

    with pytest.raises(RuntimeError, match=field):
        _confirmation_evidence(module, tmp_path, _confirmation_rows(payload=payload))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number", True])
def test_confirmation_evidence_rejects_invalid_timestamps(tmp_path: Path, value: object) -> None:
    """All timestamp inputs must be finite numeric values."""
    with pytest.raises(RuntimeError, match="monotonic_seconds"):
        _confirmation_evidence(_module(), tmp_path, _confirmation_rows(beans_added=value))


@pytest.mark.parametrize(
    ("rows", "state_onset", "message"),
    [
        (_confirmation_rows(absolute_confirmation=499.0), 105.0, "onset is after confirmation"),
        (_confirmation_rows(beans_added=106.0), 105.0, "onset is before beans added"),
        (_confirmation_rows(), 106.0, "differs from final state"),
    ],
)
def test_confirmation_evidence_rejects_order_and_state_inconsistency(
    tmp_path: Path, rows: list[dict[str, object]], state_onset: object, message: str
) -> None:
    """Onset ordering and exact export/state equality are required."""
    with pytest.raises(RuntimeError, match=message):
        _confirmation_evidence(_module(), tmp_path, rows, state_onset=state_onset)


@pytest.mark.parametrize(
    ("absolute_confirmation", "message"),
    [
        (520.017, "strict maximum"),
        (503.8271039066343, "inclusive bounds"),
        (521.1, "inclusive bounds"),
    ],
)
def test_confirmation_evidence_rejects_confirmation_bounds(
    tmp_path: Path, absolute_confirmation: float, message: str
) -> None:
    """The confirmation gate rejects strict maximum and inclusive-bound failures."""
    with pytest.raises(RuntimeError, match=message):
        _confirmation_evidence(
            _module(),
            tmp_path,
            _confirmation_rows(exported_onset=100.0, absolute_confirmation=absolute_confirmation),
            state_onset=100.0,
        )


def test_confirmation_evidence_rejects_subthreshold_confidence(tmp_path: Path) -> None:
    """Confirmation confidence remains gated at 0.6."""
    with pytest.raises(RuntimeError, match="confidence"):
        _confirmation_evidence(_module(), tmp_path, confidence=0.599)


def test_confirmation_evidence_names_malformed_json_line(tmp_path: Path) -> None:
    """Malformed export JSON names its one-based source line."""
    module = _module()
    jsonl_path = tmp_path / "roast.jsonl"
    jsonl_path.write_text('{"session_id": "current"}\n{', encoding="utf-8")

    with pytest.raises(RuntimeError, match="line 2"):
        module._confirmation_evidence(  # type: ignore[attr-defined]
            jsonl_path,
            "current",
            {"first_crack_monotonic_seconds": 105.0},
            {"first_crack_model": {"confidence": 0.6}},
        )
