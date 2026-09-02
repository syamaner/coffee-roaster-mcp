"""Test pure clean-wheel first-crack acceptance helpers."""

from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

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
