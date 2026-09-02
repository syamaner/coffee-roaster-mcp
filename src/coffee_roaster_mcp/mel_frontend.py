"""Torch-free NumPy/SciPy Kaldi-compatible mel frontend.

Adapted and modified from ``coffee-first-crack-detection`` commit
``749c9330b01c93fa153cca2e290ebfbd6c1d986c``,
``src/coffee_first_crack/mel_frontend.py`` (SHA-256
``4332e0e210e841af76b6f8692990f7576fac46d9a09882b35737102c8015d47a``),
under Apache-2.0. MCP modifications are package relocation, narrow defensive
validation and error handling, typing/docstring adjustments, and no detector
integration in this slice.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np
from scipy.signal import get_window  # type: ignore[reportMissingTypeStubs]

_FRAME_LENGTH = 400
_HOP_LENGTH = 160
_FFT_LENGTH = 512
_NUM_FREQ_BINS = _FFT_LENGTH // 2 + 1
_PREEMPHASIS = 0.97
_MEL_FLOOR = 1.192092955078125e-07
_MIN_FREQUENCY = 20.0
_DEFAULT_NUM_MEL_BINS = 128
_DEFAULT_SAMPLING_RATE = 16000
_DEFAULT_MAX_LENGTH = 1024
_FLOAT32_MAX = float(np.finfo(np.float32).max)
_FLOAT32_MIN_SUBNORMAL = float(np.nextafter(np.float32(0), np.float32(1)))
_NORMALISATION_REFERENCE_VALUES = (float(np.float32(math.log(_MEL_FLOOR))), 0.0)


class MelFrontendConfigError(ValueError):
    """Raised when consumed mel frontend configuration is invalid."""


def _hz_to_mel_kaldi(freq: float | np.ndarray) -> float | np.ndarray:
    """Convert Hz to mel using the Kaldi formula.

    Args:
        freq: Frequency value or values in Hz.

    Returns:
        The corresponding mel value or values.
    """
    return 1127.0 * np.log(1.0 + freq / 700.0)  # type: ignore[return-value]


def _build_kaldi_mel_filters(
    num_frequency_bins: int,
    num_mel_filters: int,
    min_frequency: float,
    max_frequency: float,
    sampling_rate: int,
) -> np.ndarray:
    """Build a Kaldi-compatible triangular mel filter bank.

    Args:
        num_frequency_bins: Number of FFT frequency bins.
        num_mel_filters: Number of mel filter channels.
        min_frequency: Lower filter-bank frequency in Hz.
        max_frequency: Upper filter-bank frequency in Hz.
        sampling_rate: Audio sample rate in Hz.

    Returns:
        A float32 array of shape ``(num_frequency_bins, num_mel_filters)``.
    """
    linear_frequencies = np.linspace(0, sampling_rate // 2, num_frequency_bins, dtype=np.float64)
    mel_min = _hz_to_mel_kaldi(min_frequency)
    mel_max = _hz_to_mel_kaldi(max_frequency)
    mel_points = np.linspace(mel_min, mel_max, num_mel_filters + 2, dtype=np.float64)
    bands = np.zeros((num_frequency_bins, num_mel_filters), dtype=np.float64)
    linear_frequencies_mel = _hz_to_mel_kaldi(linear_frequencies)

    for index in range(num_mel_filters):
        left = mel_points[index]
        center = mel_points[index + 1]
        right = mel_points[index + 2]
        rising = (linear_frequencies_mel - left) / (center - left)
        falling = (right - linear_frequencies_mel) / (right - center)
        bands[:, index] = np.maximum(0.0, np.minimum(rising, falling))

    return bands.astype(np.float32)


def _hann_window_symmetric(length: int) -> np.ndarray:
    """Return the symmetric Hann window used by AST's NumPy path.

    Args:
        length: Window length in samples.

    Returns:
        A float32 symmetric Hann window.
    """
    window: Any = get_window("hann", length, fftbins=False)  # type: ignore[reportUnknownVariableType]
    return np.asarray(window, dtype=np.float32)


def _validated_waveform(waveform: object) -> np.ndarray:
    """Validate and convert a single waveform without mutating its input.

    Args:
        waveform: A one-dimensional real numeric sequence or array.

    Returns:
        A C-contiguous float64 copy suitable for numerical processing.

    Raises:
        ValueError: If the waveform is not a finite, one-dimensional real numeric
            signal with at least one analysis frame.
    """
    try:
        array = np.asarray(waveform)
    except (TypeError, ValueError) as exc:
        raise ValueError("waveform must be a one-dimensional real numeric array") from exc
    if array.ndim != 1:
        raise ValueError("waveform must be one-dimensional")
    if array.size == 0:
        raise ValueError("waveform must not be empty")
    if array.dtype.kind not in "iuf":
        raise ValueError("waveform must contain real numeric values")
    if array.size < _FRAME_LENGTH:
        raise ValueError(f"waveform must contain at least {_FRAME_LENGTH} samples")
    converted = np.ascontiguousarray(array, dtype=np.float64)
    if not np.isfinite(converted).all():
        raise ValueError("waveform must contain only finite values")
    return converted


def _require_real(value: object, name: str) -> float:
    """Return a finite non-boolean real value or raise a configuration error.

    Args:
        value: Candidate JSON or constructor value.
        name: Field name used in the bounded error message.

    Returns:
        A finite float.

    Raises:
        MelFrontendConfigError: If the value is not a finite real number.
    """
    if isinstance(value, (bool, complex)) or not isinstance(value, (int, float)):
        raise MelFrontendConfigError(f"{name} must be a finite JSON number")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise MelFrontendConfigError(f"{name} must be finite") from exc
    if not math.isfinite(converted):
        raise MelFrontendConfigError(f"{name} must be finite")
    return converted


def _validate_normalisation_parameters(mean: float, std: float) -> None:
    """Validate normalisation parameters against known finite feature values.

    Args:
        mean: Global normalisation mean.
        std: Positive global normalisation standard deviation.

    Raises:
        MelFrontendConfigError: If normalisation cannot safely produce float32 output.
    """
    if abs(mean) > _FLOAT32_MAX or std > _FLOAT32_MAX or std < _FLOAT32_MIN_SUBNORMAL:
        raise MelFrontendConfigError("mean and std must be representable as finite float32")
    scale = std * 2.0
    for value in _NORMALISATION_REFERENCE_VALUES:
        normalised = (value - mean) / scale
        if not math.isfinite(normalised) or abs(normalised) > _FLOAT32_MAX:
            raise MelFrontendConfigError("mean and std must produce finite float32 normalisation")


def _require_fixed_integer(value: object, name: str, expected: int) -> int:
    """Validate an exactly fixed, non-boolean integer configuration value.

    Args:
        value: Candidate JSON or constructor value.
        name: Field name used in the bounded error message.
        expected: The only supported integer value.

    Returns:
        The expected value.

    Raises:
        MelFrontendConfigError: If the value is not the required integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MelFrontendConfigError(f"{name} must be integer {expected}")
    if int(value) != expected:
        raise MelFrontendConfigError(f"{name} must be {expected}")
    return expected


def extract_mel(
    waveform: object,
    mel_filters: np.ndarray,
    window: np.ndarray,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> np.ndarray:
    """Compute an unnormalised Kaldi-compatible log-mel spectrogram.

    Args:
        waveform: A single real numeric waveform.
        mel_filters: Kaldi mel filter matrix.
        window: Symmetric Hann window.
        max_length: Output frame count after padding or truncation.

    Returns:
        A float32 ``(max_length, num_mel_filters)`` log-mel array.
    """
    if type(max_length) is not int or max_length <= 0:
        raise ValueError("max_length must be a positive integer")
    samples = _validated_waveform(waveform)
    frame_count = 1 + (len(samples) - _FRAME_LENGTH) // _HOP_LENGTH
    frames = np.zeros((frame_count, _FRAME_LENGTH), dtype=np.float64)
    for index in range(frame_count):
        start = index * _HOP_LENGTH
        frames[index] = samples[start : start + _FRAME_LENGTH]

    frames -= frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= _PREEMPHASIS * frames[:, :-1]
    frames[:, 0] *= 1.0 - _PREEMPHASIS
    frames *= window[np.newaxis, :].astype(np.float64)
    fft_output = np.fft.rfft(frames, n=_FFT_LENGTH, axis=1)
    power = fft_output.real**2 + fft_output.imag**2
    mel_spectrum = np.dot(power, mel_filters.astype(np.float64))
    log_mel = np.log(np.maximum(mel_spectrum, _MEL_FLOOR)).astype(np.float32)

    difference = max_length - log_mel.shape[0]
    if difference > 0:
        log_mel = np.pad(log_mel, ((0, difference), (0, 0)), mode="constant")
    elif difference < 0:
        log_mel = log_mel[:max_length, :]
    return np.ascontiguousarray(log_mel, dtype=np.float32)


class MelFrontend:
    """NumPy/SciPy Kaldi-compatible frontend for AST-compatible ONNX input.

    Args:
        mean: Global normalisation mean.
        std: Global normalisation standard deviation.
        num_mel_bins: Required mel channel count, exactly 128.
        sampling_rate: Required sample rate, exactly 16000 Hz.
        max_length: Required output length, exactly 1024 frames.
        min_frequency: Required mel lower frequency, exactly 20 Hz.
    """

    def __init__(
        self,
        mean: object,
        std: object,
        num_mel_bins: object = _DEFAULT_NUM_MEL_BINS,
        sampling_rate: object = _DEFAULT_SAMPLING_RATE,
        max_length: object = _DEFAULT_MAX_LENGTH,
        min_frequency: object = _MIN_FREQUENCY,
    ) -> None:
        """Initialise the frontend with its fixed AST numerical contract."""
        self.mean = _require_real(mean, "mean")
        self.std = _require_real(std, "std")
        if self.std <= 0:
            raise MelFrontendConfigError("std must be strictly positive")
        _validate_normalisation_parameters(self.mean, self.std)
        self.num_mel_bins = _require_fixed_integer(
            num_mel_bins, "num_mel_bins", _DEFAULT_NUM_MEL_BINS
        )
        self.sampling_rate = _require_fixed_integer(
            sampling_rate, "sampling_rate", _DEFAULT_SAMPLING_RATE
        )
        self.max_length = _require_fixed_integer(max_length, "max_length", _DEFAULT_MAX_LENGTH)
        checked_min_frequency = _require_real(min_frequency, "min_frequency")
        if checked_min_frequency != _MIN_FREQUENCY:
            raise MelFrontendConfigError(f"min_frequency must be {_MIN_FREQUENCY}")
        if checked_min_frequency >= self.sampling_rate / 2:  # pragma: no cover - fixed inputs above
            raise MelFrontendConfigError("min_frequency must be below Nyquist")
        self._mel_filters = _build_kaldi_mel_filters(
            _NUM_FREQ_BINS,
            self.num_mel_bins,
            checked_min_frequency,
            self.sampling_rate // 2,
            self.sampling_rate,
        )
        self._window = _hann_window_symmetric(_FRAME_LENGTH)

    @classmethod
    def from_config(cls, config_dir: str | Path) -> MelFrontend:
        """Construct a frontend from ``preprocessor_config.json``.

        Args:
            config_dir: Directory containing the preprocessor configuration.

        Returns:
            A validated frontend instance.

        Raises:
            FileNotFoundError: If the configuration file is absent.
            MelFrontendConfigError: If consumed configuration content is invalid.
        """
        config_path = Path(config_dir) / "preprocessor_config.json"
        if not config_path.is_file():
            raise FileNotFoundError("preprocessor_config.json not found in supplied directory")
        try:
            with config_path.open(encoding="utf-8") as file_handle:
                loaded: object = json.load(file_handle)
        except UnicodeDecodeError as exc:
            raise MelFrontendConfigError("preprocessor_config.json contains invalid UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise MelFrontendConfigError("preprocessor_config.json contains invalid JSON") from exc
        except ValueError as exc:
            raise MelFrontendConfigError(
                "preprocessor_config.json contains invalid numeric content"
            ) from exc
        except OSError as exc:
            raise MelFrontendConfigError("preprocessor_config.json could not be read") from exc
        if not isinstance(loaded, dict):
            raise MelFrontendConfigError("preprocessor_config.json must contain a JSON object")
        if "mean" not in loaded:
            raise MelFrontendConfigError("preprocessor_config.json is missing mean")
        if "std" not in loaded:
            raise MelFrontendConfigError("preprocessor_config.json is missing std")
        config = cast(dict[str, object], loaded)
        if config.get("do_normalize", True) is not True:
            raise MelFrontendConfigError("do_normalize must be true when present")
        return cls(
            mean=config["mean"],
            std=config["std"],
            num_mel_bins=config.get("num_mel_bins", _DEFAULT_NUM_MEL_BINS),
            sampling_rate=config.get("sampling_rate", _DEFAULT_SAMPLING_RATE),
            max_length=config.get("max_length", _DEFAULT_MAX_LENGTH),
        )

    def extract(self, waveform: object) -> np.ndarray:
        """Extract normalised features from one waveform.

        Args:
            waveform: A finite one-dimensional real numeric audio signal.

        Returns:
            A C-contiguous float32 array of shape ``(1024, 128)``.
        """
        log_mel = extract_mel(waveform, self._mel_filters, self._window, self.max_length)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            normalised = (log_mel.astype(np.float64) - self.mean) / (self.std * 2)
        if not np.isfinite(normalised).all() or np.abs(normalised).max() > _FLOAT32_MAX:
            raise MelFrontendConfigError("normalised features must be finite float32 values")
        return np.ascontiguousarray(normalised, dtype=np.float32)

    def __call__(
        self,
        raw_speech: object,
        sampling_rate: int | None = None,
        return_tensors: str | None = None,
    ) -> dict[str, np.ndarray]:
        """Return AST-compatible batched ``input_values``.

        Args:
            raw_speech: Non-empty sequence of individual waveforms.
            sampling_rate: Optional sample rate which must be 16000 Hz.
            return_tensors: Accepted for AST API compatibility; output is NumPy.

        Returns:
            A mapping containing C-contiguous float32 ``input_values``.

        Raises:
            ValueError: If the batch is empty or the sample rate is incompatible.
        """
        del return_tensors
        if sampling_rate is not None and sampling_rate != self.sampling_rate:
            raise ValueError(f"sampling_rate must be {self.sampling_rate}")
        if not isinstance(raw_speech, Sequence) or isinstance(raw_speech, (str, bytes)):
            raise ValueError("raw_speech must be a non-empty batch of waveforms")
        if not raw_speech:
            raise ValueError("raw_speech must not be empty")
        batch = cast(Sequence[object], raw_speech)
        return {
            "input_values": np.ascontiguousarray(np.stack([self.extract(item) for item in batch]))
        }
