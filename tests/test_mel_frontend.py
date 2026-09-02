"""Behavioural tests for the standalone NumPy/SciPy mel frontend."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from coffee_roaster_mcp.mel_frontend import MelFrontend, MelFrontendConfigError, extract_mel

_FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "mel_frontend"
_MANIFEST_PATH = _FIXTURE_DIRECTORY / "analytic-10s.manifest.json"
_REFERENCE_SOURCE_SHA = "4332e0e210e841af76b6f8692990f7576fac46d9a09882b35737102c8015d47a"
_REFERENCE_TEST_SHA = "090fb759aa0b1f82e6761d21e04cee0cf0a6bcfea48abb6b80f779332573b516"
_REFERENCE_LICENSE_SHA = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
_EXPECTED_ATOL = 1e-4
_EXPECTED_DTYPE = np.dtype(np.float32)
_EXPECTED_SHAPE = (1024, 128)


def _manifest() -> dict[str, Any]:
    """Load the committed JSON fixture manifest.

    Returns:
        Fixture metadata.
    """
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _waveform_from_recipe() -> np.ndarray:
    """Regenerate the deterministic analytic fixture waveform.

    Returns:
        The 10-second float32 waveform described by the manifest.
    """
    sample_count = 160000
    sampling_rate = 16000
    time = np.arange(sample_count, dtype=np.float64) / sampling_rate
    envelope = 0.55 + 0.25 * np.sin(2.0 * np.pi * 0.31 * time)
    segment = np.where(time < 3.25, 1.0, np.where(time < 6.75, 0.62, 0.88))
    carrier = (
        0.19 * np.sin(2.0 * np.pi * 173.0 * time)
        + 0.11 * np.sin(2.0 * np.pi * 701.0 * time + 0.4)
        + 0.07 * np.sin(2.0 * np.pi * 2261.0 * time - 0.2)
    )
    return (segment * envelope * carrier + 0.0125).astype(np.float32)


def _frontend() -> MelFrontend:
    """Return the frontend configured for the committed golden fixture.

    Returns:
        A configured mel frontend.
    """
    return MelFrontend(mean=-4.2677393, std=4.5689974)


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write a temporary preprocessor configuration.

    Args:
        tmp_path: Temporary test directory.
        content: JSON content to write.

    Returns:
        The configuration directory.
    """
    config_path = tmp_path / "preprocessor_config.json"
    config_path.write_text(content, encoding="utf-8")
    return tmp_path


def test_golden_fixture_provenance_and_strict_parity() -> None:
    """The port must match the reference-generated fixture at strict tolerance."""
    manifest = _manifest()
    assert manifest["source"]["sha256"] == _REFERENCE_SOURCE_SHA
    assert manifest["reference_test"]["sha256"] == _REFERENCE_TEST_SHA
    assert manifest["license"]["sha256"] == _REFERENCE_LICENSE_SHA
    assert manifest["comparison"]["atol"] == _EXPECTED_ATOL
    assert manifest["comparison"]["rtol"] == 0
    assert manifest["comparison"]["max_abs_diff_operator"] == "<"
    expected_info = manifest["expected"]
    fixture_path = _FIXTURE_DIRECTORY / expected_info["file"]
    fixture_bytes = fixture_path.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == expected_info["sha256"]
    assert len(fixture_bytes) == expected_info["bytes"]
    with np.load(fixture_path, allow_pickle=False) as fixture:
        assert set(fixture.files) == {expected_info["key"]}
        expected = fixture[expected_info["key"]]
    assert expected_info["dtype"] == _EXPECTED_DTYPE.name
    assert tuple(expected_info["shape"]) == _EXPECTED_SHAPE
    assert expected.dtype == _EXPECTED_DTYPE
    assert expected.shape == _EXPECTED_SHAPE
    assert np.isfinite(expected).all()

    actual = _frontend().extract(_waveform_from_recipe())
    max_abs_diff = np.abs(actual.astype(np.float64) - expected.astype(np.float64)).max()
    assert max_abs_diff < _EXPECTED_ATOL, f"max_abs_diff={max_abs_diff:.9g}"


def test_recipe_and_extract_are_repeatably_bitwise_deterministic() -> None:
    """The analytic waveform and frontend output must be deterministic."""
    first_waveform = _waveform_from_recipe()
    second_waveform = _waveform_from_recipe()
    assert np.array_equal(first_waveform, second_waveform)
    frontend = _frontend()
    first = frontend.extract(first_waveform)
    second = frontend.extract(first_waveform)
    assert np.array_equal(first, second)
    assert first.dtype == np.float32
    assert first.flags.c_contiguous
    assert np.isfinite(first).all()


def test_padding_truncation_and_normalisation_semantics() -> None:
    """The frontend must normalise and retain reference padding/truncation rules."""
    frontend = _frontend()
    waveform = _waveform_from_recipe()
    raw = extract_mel(
        waveform,
        frontend._mel_filters,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
        frontend._window,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
    )
    actual = frontend.extract(waveform)
    np.testing.assert_array_equal(
        actual, ((raw - frontend.mean) / (frontend.std * 2)).astype(np.float32)
    )
    assert np.array_equal(raw[998:], np.zeros((26, 128), dtype=np.float32))
    long = np.tile(waveform, 2)
    long_raw = extract_mel(
        long,
        frontend._mel_filters,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
        frontend._window,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
    )
    untruncated = extract_mel(
        long,
        frontend._mel_filters,  # type: ignore[reportPrivateUsage]
        frontend._window,  # type: ignore[reportPrivateUsage]
        max_length=2000,  # noqa: SLF001
    )
    np.testing.assert_array_equal(long_raw, untruncated[:1024])
    exact = extract_mel(
        np.zeros(164080, dtype=np.float32),
        frontend._mel_filters,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
        frontend._window,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
    )
    assert exact.shape == (1024, 128)


def test_config_loads_required_values_and_ignores_unrelated_keys(tmp_path: Path) -> None:
    """Released-preprocessor extras must not affect the narrowly consumed values."""
    directory = _write_config(
        tmp_path,
        json.dumps(
            {
                "mean": -4.2677393,
                "std": 4.5689974,
                "num_mel_bins": 128,
                "sampling_rate": 16000,
                "max_length": 1024,
                "feature_extractor_type": "ASTFeatureExtractor",
                "do_normalize": True,
            }
        ),
    )
    frontend = MelFrontend.from_config(directory)
    assert (frontend.mean, frontend.std, frontend.num_mel_bins) == (-4.2677393, 4.5689974, 128)


def test_config_defaults_optional_consumed_values(tmp_path: Path) -> None:
    """Missing optional consumed values must retain the reference defaults."""
    frontend = MelFrontend.from_config(_write_config(tmp_path, '{"mean": -4.0, "std": 2.0}'))
    assert (frontend.num_mel_bins, frontend.sampling_rate, frontend.max_length) == (
        128,
        16000,
        1024,
    )


def test_config_missing_file_malformed_non_object_and_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, malformed, non-object, and unreadable configs fail clearly."""
    with pytest.raises(FileNotFoundError, match="preprocessor_config.json"):
        MelFrontend.from_config(tmp_path)
    with pytest.raises(MelFrontendConfigError, match="invalid JSON"):
        MelFrontend.from_config(_write_config(tmp_path, "{"))
    with pytest.raises(MelFrontendConfigError, match="JSON object"):
        MelFrontend.from_config(_write_config(tmp_path, "[]"))

    def raise_os_error(*args: object, **kwargs: object) -> Any:
        """Simulate a local read failure without changing filesystem permissions."""
        del args, kwargs
        raise OSError("denied")

    directory = _write_config(tmp_path, '{"mean": -4.0, "std": 2.0}')
    monkeypatch.setattr(Path, "open", raise_os_error)
    with pytest.raises(MelFrontendConfigError, match="could not be read"):
        MelFrontend.from_config(directory)


def test_config_invalid_utf8_is_a_bounded_configuration_error(tmp_path: Path) -> None:
    """Invalid UTF-8 in the configuration must not leak a decoding error."""
    (tmp_path / "preprocessor_config.json").write_bytes(b"\xff")
    with pytest.raises(MelFrontendConfigError, match="invalid UTF-8"):
        MelFrontend.from_config(tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"std": 1.0}', "missing mean"),
        ('{"mean": 1.0}', "missing std"),
        ('{"mean": true, "std": 1.0}', "mean"),
        ('{"mean": "1", "std": 1.0}', "mean"),
        ('{"mean": 1e999, "std": 1.0}', "mean"),
        ('{"mean": 1.0, "std": false}', "std"),
        ('{"mean": 1.0, "std": "1"}', "std"),
        ('{"mean": 1.0, "std": 0}', "strictly positive"),
        ('{"mean": 1.0, "std": -1}', "strictly positive"),
        ('{"mean": 1.0, "std": 1.0, "num_mel_bins": 127}', "num_mel_bins"),
        ('{"mean": 1.0, "std": 1.0, "num_mel_bins": true}', "num_mel_bins"),
        ('{"mean": 1.0, "std": 1.0, "sampling_rate": 8000}', "sampling_rate"),
        ('{"mean": 1.0, "std": 1.0, "sampling_rate": 16000.0}', "sampling_rate"),
        ('{"mean": 1.0, "std": 1.0, "max_length": 1023}', "max_length"),
    ],
)
def test_config_rejects_invalid_consumed_values(tmp_path: Path, content: str, message: str) -> None:
    """Only consumed values are validated, including strict fixed dimensions."""
    with pytest.raises(MelFrontendConfigError, match=message):
        MelFrontend.from_config(_write_config(tmp_path, content))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mean": float("nan"), "std": 1.0},
        {"mean": 1.0, "std": float("inf")},
        {"mean": True, "std": 1.0},
        {"mean": 1.0, "std": True},
        {"mean": 1 + 1j, "std": 1.0},
        {"mean": 1.0, "std": 1.0, "num_mel_bins": 127},
        {"mean": 1.0, "std": 1.0, "sampling_rate": 8000},
        {"mean": 1.0, "std": 1.0, "max_length": 1000},
        {"mean": 1.0, "std": 1.0, "min_frequency": 19.0},
        {"mean": 1e308, "std": 1.0},
        {"mean": 0.0, "std": 1e-308},
    ],
)
def test_constructor_rejects_invalid_contract_values(kwargs: dict[str, object]) -> None:
    """Direct construction enforces the same finite fixed configuration rules."""
    with pytest.raises(MelFrontendConfigError):
        MelFrontend(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"frame_length": 800}, "at least 800 samples"),
        ({"frame_length": 0}, "frame_length must be a positive integer"),
        ({"hop_length": 0}, "hop_length must be a positive integer"),
    ],
)
def test_extract_mel_rejects_invalid_analysis_dimensions(
    kwargs: dict[str, int], message: str
) -> None:
    """Analysis dimensions must fail before frame allocation or arithmetic."""
    frontend = _frontend()
    with pytest.raises(ValueError, match=message):
        extract_mel(
            np.zeros(400, dtype=np.float32),
            frontend._mel_filters,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
            frontend._window,  # noqa: SLF001  # type: ignore[reportPrivateUsage]
            **kwargs,
        )


@pytest.mark.parametrize(
    "waveform",
    [
        np.zeros((400, 1), dtype=np.float32),
        np.array([], dtype=np.float32),
        np.zeros(399, dtype=np.float32),
        np.array(["x"] * 400),
        np.array([1 + 1j] * 400),
        np.full(400, np.nan, dtype=np.float32),
        np.full(400, np.inf, dtype=np.float64),
    ],
)
def test_waveform_validation_rejects_invalid_inputs(waveform: np.ndarray) -> None:
    """Invalid ranks, dtypes, lengths, and values must fail before processing."""
    with pytest.raises(ValueError, match="waveform"):
        _frontend().extract(waveform)


def test_waveform_conversion_errors_are_value_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Array conversion failures must remain within the documented ValueError family."""
    frontend = _frontend()

    def raise_type_error(*args: object, **kwargs: object) -> Any:
        """Simulate an array conversion failure."""
        del args, kwargs
        raise TypeError("not numeric")

    monkeypatch.setattr(np, "asarray", raise_type_error)
    with pytest.raises(ValueError, match="real numeric"):
        frontend.extract(object())


def test_waveform_non_mutation_non_contiguity_and_numeric_sequences() -> None:
    """Input conversion accepts real sequences without mutating non-contiguous arrays."""
    waveform = _waveform_from_recipe()[::2]
    before = waveform.copy()
    assert not waveform.flags.c_contiguous
    result = _frontend().extract(waveform)
    assert np.array_equal(waveform, before)
    assert result.flags.c_contiguous
    assert _frontend().extract(_waveform_from_recipe()[:400].tolist()).shape == (1024, 128)


@pytest.mark.parametrize(
    "waveform",
    [
        np.zeros(400, dtype=np.float32),
        np.full(400, 0.3, dtype=np.float64),
        np.full(400, 1e-12, dtype=np.float32),
        np.resize(np.array([-1.0, 1.0], dtype=np.float32), 400),
    ],
)
def test_numerically_challenging_valid_inputs_stay_finite(waveform: np.ndarray) -> None:
    """Valid silence, DC, low-amplitude, and full-scale inputs remain finite."""
    result = _frontend().extract(waveform)
    assert result.shape == (1024, 128)
    assert result.dtype == np.float32
    assert np.isfinite(result).all()


def test_batch_contract_sampling_rate_and_empty_batch() -> None:
    """The AST-compatible batch API rejects empty batches and wrong rates."""
    frontend = _frontend()
    with pytest.raises(ValueError, match="raw_speech"):
        frontend([])
    with pytest.raises(ValueError, match="raw_speech"):
        frontend("not a batch")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sampling_rate"):
        frontend([np.zeros(400, dtype=np.float32)], sampling_rate=8000)
    output = frontend([np.zeros(400, dtype=np.float32)], sampling_rate=16000, return_tensors="np")
    assert output["input_values"].shape == (1, 1024, 128)
    assert output["input_values"].dtype == np.float32
    assert output["input_values"].flags.c_contiguous


def test_import_isolation_and_no_existing_production_import() -> None:
    """The standalone module must not pull Torch/Transformers or be integrated yet."""
    module_name = "coffee_roaster_mcp.mel_frontend"
    sys.modules.pop(module_name, None)
    sys.modules.pop("torch", None)
    sys.modules.pop("transformers", None)
    importlib.import_module(module_name)
    assert "torch" not in sys.modules
    assert "transformers" not in sys.modules

    source_directory = Path(__file__).parents[1] / "src" / "coffee_roaster_mcp"
    for source_path in source_directory.glob("*.py"):
        if source_path.name == "mel_frontend.py":
            continue
        parsed = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = [
            node for node in ast.walk(parsed) if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert all(
            not (
                isinstance(node, ast.ImportFrom)
                and node.module == "coffee_roaster_mcp.mel_frontend"
            )
            and not (
                isinstance(node, ast.Import)
                and any(alias.name == "coffee_roaster_mcp.mel_frontend" for alias in node.names)
            )
            for node in imports
        ), source_path.name
