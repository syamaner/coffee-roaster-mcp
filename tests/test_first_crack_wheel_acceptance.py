"""Test pure clean-wheel first-crack acceptance helpers."""

from __future__ import annotations

import importlib.util
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
