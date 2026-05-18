"""Configuration loading for RoastPilot."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

DEFAULT_CONFIG_FILENAME = "coffee-roaster-mcp.yaml"

TransportType = Literal["stdio"]
TemperatureUnit = Literal["celsius", "fahrenheit", "auto"]
FirstCrackMode = Literal["disabled", "audio", "manual"]
ModelPrecision = Literal["int8", "fp32"]
AudioSource = Literal["microphone", "wav"]
ExportFormat = Literal["jsonl", "csv", "summary"]


@dataclass(frozen=True)
class TransportConfig:
    """MCP transport configuration."""

    type: TransportType = "stdio"


@dataclass(frozen=True)
class RoasterConfig:
    """Roaster driver configuration."""

    driver: str = "mock"
    port: str | None = None
    baudrate: int = 115_200
    temperature_unit: TemperatureUnit = "celsius"
    command_interval_seconds: float = 0.3


@dataclass(frozen=True)
class FirstCrackConfig:
    """First-crack detector configuration."""

    mode: FirstCrackMode = "disabled"
    repo_id: str = "syamaner/coffee-first-crack-detection"
    revision: str | None = None
    precision: ModelPrecision = "int8"
    local_model_dir: Path | None = None
    onnx_threads: int = 2
    allow_manual_override: bool = True


@dataclass(frozen=True)
class AudioConfig:
    """Audio capture configuration."""

    source: AudioSource = "microphone"
    input_device: str | None = None
    sample_rate: int = 16_000
    wav_path: Path | None = None


@dataclass(frozen=True)
class LoggingConfig:
    """Roast logging configuration."""

    log_dir: Path = Path("./logs")
    sample_interval_seconds: float = 1.0
    export_formats: tuple[ExportFormat, ...] = ("jsonl", "csv", "summary")


@dataclass(frozen=True)
class SessionConfig:
    """Roast session metric configuration."""

    auto_t0_detection_enabled: bool = False
    auto_t0_drop_threshold_c: float = 25.0
    ror_window_seconds: int = 60
    ror_min_sample_seconds: int = 10


@dataclass(frozen=True)
class AppConfig:
    """Top-level RoastPilot configuration."""

    transport: TransportConfig = field(default_factory=TransportConfig)
    roaster: RoasterConfig = field(default_factory=RoasterConfig)
    first_crack: FirstCrackConfig = field(default_factory=FirstCrackConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    source_path: Path | None = None


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


def load_config(
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load RoastPilot configuration from defaults, YAML, and environment overrides."""
    env = os.environ if environ is None else environ
    env_config_path = (
        _none_if_empty(env["COFFEE_ROASTER_MCP_CONFIG"])
        if "COFFEE_ROASTER_MCP_CONFIG" in env
        else None
    )
    config_path = _resolve_config_path(path, env)
    config_path_exists = config_path.exists()
    raw_config: Mapping[str, Any] = {}

    if config_path_exists:
        raw_config = _read_yaml_config(config_path)
    elif path is not None or env_config_path is not None:
        raise ConfigError(f"Config file {config_path} does not exist.")

    source_path = config_path if config_path_exists else None
    config = _config_from_mapping(raw_config, source_path=source_path)
    return _apply_env_overrides(config, env)


def _resolve_config_path(path: str | Path | None, environ: Mapping[str, str]) -> Path:
    if path is not None:
        return Path(path)

    env_path = environ.get("COFFEE_ROASTER_MCP_CONFIG")
    if env_path is not None:
        normalized_env_path = _none_if_empty(env_path)
        if normalized_env_path is not None:
            return Path(normalized_env_path)

    return Path(DEFAULT_CONFIG_FILENAME)


def _read_yaml_config(path: Path) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "Loading coffee-roaster-mcp.yaml requires PyYAML. "
            "Install the package dependencies before using YAML configuration."
        ) from exc

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML config file {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"Config file {path} must contain a YAML mapping at the top level.")
    return cast(Mapping[str, Any], loaded)


def _config_from_mapping(raw_config: Mapping[str, Any], source_path: Path | None) -> AppConfig:
    return AppConfig(
        transport=_transport_from_mapping(_section(raw_config, "transport")),
        roaster=_roaster_from_mapping(_section(raw_config, "roaster")),
        first_crack=_first_crack_from_mapping(_section(raw_config, "first_crack")),
        audio=_audio_from_mapping(_section(raw_config, "audio")),
        logging=_logging_from_mapping(_section(raw_config, "logging")),
        session=_session_from_mapping(_section(raw_config, "session")),
        source_path=source_path,
    )


def _section(raw_config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw_config.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"Config section '{name}' must be a mapping.")
    return cast(Mapping[str, Any], value)


def _transport_from_mapping(raw: Mapping[str, Any]) -> TransportConfig:
    transport_type = _normalized_token(_string(raw, "type", "stdio", label="transport.type"))
    if transport_type != "stdio":
        raise ConfigError("transport.type must be 'stdio'.")
    return TransportConfig(type="stdio")


def _roaster_from_mapping(raw: Mapping[str, Any]) -> RoasterConfig:
    return RoasterConfig(
        driver=_string(raw, "driver", "mock", label="roaster.driver"),
        port=_optional_string(raw, "port", label="roaster.port"),
        baudrate=_integer(raw, "baudrate", 115_200, label="roaster.baudrate"),
        temperature_unit=_temperature_unit(
            _string(raw, "temperature_unit", "celsius", label="roaster.temperature_unit"),
            label="roaster.temperature_unit",
        ),
        command_interval_seconds=_float(
            raw,
            "command_interval_seconds",
            0.3,
            label="roaster.command_interval_seconds",
        ),
    )


def _first_crack_from_mapping(raw: Mapping[str, Any]) -> FirstCrackConfig:
    local_model_dir = _optional_path(raw, "local_model_dir", label="first_crack.local_model_dir")
    return FirstCrackConfig(
        mode=_first_crack_mode(
            _string(raw, "mode", "disabled", label="first_crack.mode"),
            label="first_crack.mode",
        ),
        repo_id=_string(
            raw,
            "repo_id",
            "syamaner/coffee-first-crack-detection",
            label="first_crack.repo_id",
        ),
        revision=_optional_string(raw, "revision", label="first_crack.revision"),
        precision=_model_precision(
            _string(raw, "precision", "int8", label="first_crack.precision"),
            label="first_crack.precision",
        ),
        local_model_dir=local_model_dir,
        onnx_threads=_integer(raw, "onnx_threads", 2, label="first_crack.onnx_threads"),
        allow_manual_override=_boolean(
            raw,
            "allow_manual_override",
            True,
            label="first_crack.allow_manual_override",
        ),
    )


def _audio_from_mapping(raw: Mapping[str, Any]) -> AudioConfig:
    return AudioConfig(
        source=_audio_source(
            _string(raw, "source", "microphone", label="audio.source"),
            label="audio.source",
        ),
        input_device=_optional_string(raw, "input_device", label="audio.input_device"),
        sample_rate=_integer(raw, "sample_rate", 16_000, label="audio.sample_rate"),
        wav_path=_optional_path(raw, "wav_path", label="audio.wav_path"),
    )


def _logging_from_mapping(raw: Mapping[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        log_dir=_path(raw, "log_dir", Path("./logs"), label="logging.log_dir"),
        sample_interval_seconds=_float(
            raw,
            "sample_interval_seconds",
            1.0,
            label="logging.sample_interval_seconds",
        ),
        export_formats=_export_formats(raw.get("export_formats", ("jsonl", "csv", "summary"))),
    )


def _session_from_mapping(raw: Mapping[str, Any]) -> SessionConfig:
    return SessionConfig(
        auto_t0_detection_enabled=_boolean(
            raw,
            "auto_t0_detection_enabled",
            False,
            label="session.auto_t0_detection_enabled",
        ),
        auto_t0_drop_threshold_c=_positive_float(
            raw,
            "auto_t0_drop_threshold_c",
            25.0,
            label="session.auto_t0_drop_threshold_c",
        ),
        ror_window_seconds=_integer(
            raw,
            "ror_window_seconds",
            60,
            label="session.ror_window_seconds",
        ),
        ror_min_sample_seconds=_integer(
            raw,
            "ror_min_sample_seconds",
            10,
            label="session.ror_min_sample_seconds",
        ),
    )


def _apply_env_overrides(config: AppConfig, environ: Mapping[str, str]) -> AppConfig:
    roaster = config.roaster
    first_crack = config.first_crack
    audio = config.audio
    logging = config.logging

    if "COFFEE_ROASTER_DRIVER" in environ:
        roaster = replace(
            roaster,
            driver=_required_string(environ["COFFEE_ROASTER_DRIVER"], "COFFEE_ROASTER_DRIVER"),
        )
    if "COFFEE_ROASTER_PORT" in environ:
        roaster = replace(roaster, port=_none_if_empty(environ["COFFEE_ROASTER_PORT"]))
    if "COFFEE_ROASTER_TEMP_UNIT" in environ:
        roaster = replace(
            roaster,
            temperature_unit=_temperature_unit(
                environ["COFFEE_ROASTER_TEMP_UNIT"],
                label="COFFEE_ROASTER_TEMP_UNIT",
            ),
        )

    if "COFFEE_FIRST_CRACK_MODE" in environ:
        first_crack = replace(
            first_crack,
            mode=_first_crack_mode(
                environ["COFFEE_FIRST_CRACK_MODE"],
                label="COFFEE_FIRST_CRACK_MODE",
            ),
        )
    if "COFFEE_FIRST_CRACK_REPO_ID" in environ:
        first_crack = replace(
            first_crack,
            repo_id=_required_string(
                environ["COFFEE_FIRST_CRACK_REPO_ID"],
                "COFFEE_FIRST_CRACK_REPO_ID",
            ),
        )
    if "COFFEE_FIRST_CRACK_REVISION" in environ:
        first_crack = replace(
            first_crack,
            revision=_none_if_empty(environ["COFFEE_FIRST_CRACK_REVISION"]),
        )
    if "COFFEE_FIRST_CRACK_PRECISION" in environ:
        first_crack = replace(
            first_crack,
            precision=_model_precision(
                environ["COFFEE_FIRST_CRACK_PRECISION"],
                label="COFFEE_FIRST_CRACK_PRECISION",
            ),
        )
    if "COFFEE_FIRST_CRACK_LOCAL_MODEL_DIR" in environ:
        local_dir = _none_if_empty(environ["COFFEE_FIRST_CRACK_LOCAL_MODEL_DIR"])
        first_crack = replace(
            first_crack,
            local_model_dir=Path(local_dir) if local_dir is not None else None,
        )
    if "COFFEE_FIRST_CRACK_ONNX_THREADS" in environ:
        first_crack = replace(
            first_crack,
            onnx_threads=_parse_integer(
                environ["COFFEE_FIRST_CRACK_ONNX_THREADS"],
                "COFFEE_FIRST_CRACK_ONNX_THREADS",
            ),
        )

    if "COFFEE_AUDIO_SOURCE" in environ:
        audio = replace(
            audio,
            source=_audio_source(environ["COFFEE_AUDIO_SOURCE"], label="COFFEE_AUDIO_SOURCE"),
        )
    if "COFFEE_AUDIO_INPUT_DEVICE" in environ:
        audio = replace(audio, input_device=_none_if_empty(environ["COFFEE_AUDIO_INPUT_DEVICE"]))
    if "COFFEE_AUDIO_SAMPLE_RATE" in environ:
        audio = replace(
            audio,
            sample_rate=_parse_integer(
                environ["COFFEE_AUDIO_SAMPLE_RATE"],
                "COFFEE_AUDIO_SAMPLE_RATE",
            ),
        )
    if "COFFEE_AUDIO_WAV_PATH" in environ:
        wav_path = _none_if_empty(environ["COFFEE_AUDIO_WAV_PATH"])
        audio = replace(audio, wav_path=Path(wav_path) if wav_path is not None else None)

    if "COFFEE_ROAST_LOG_DIR" in environ:
        logging = replace(
            logging,
            log_dir=Path(_required_string(environ["COFFEE_ROAST_LOG_DIR"], "COFFEE_ROAST_LOG_DIR")),
        )
    if "COFFEE_AUTO_T0_DROP_THRESHOLD_C" in environ:
        session = replace(
            config.session,
            auto_t0_drop_threshold_c=_parse_positive_float(
                environ["COFFEE_AUTO_T0_DROP_THRESHOLD_C"],
                "COFFEE_AUTO_T0_DROP_THRESHOLD_C",
            ),
        )
    else:
        session = config.session

    return replace(
        config,
        roaster=roaster,
        first_crack=first_crack,
        audio=audio,
        logging=logging,
        session=session,
    )


def _string(raw: Mapping[str, Any], key: str, default: str, label: str | None = None) -> str:
    error_key = label or key
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{error_key} must be a string.")
    return value


def _optional_string(raw: Mapping[str, Any], key: str, label: str | None = None) -> str | None:
    error_key = label or key
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{error_key} must be a string or null.")
    return _none_if_empty(value)


def _path(raw: Mapping[str, Any], key: str, default: Path, label: str | None = None) -> Path:
    error_key = label or key
    value = raw.get(key, default)
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise ConfigError(f"{error_key} must be a path string.")


def _optional_path(raw: Mapping[str, Any], key: str, label: str | None = None) -> Path | None:
    error_key = label or key
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return Path(value)
    raise ConfigError(f"{error_key} must be a path string or null.")


def _boolean(
    raw: Mapping[str, Any],
    key: str,
    default: bool,
    label: str | None = None,
) -> bool:
    error_key = label or key
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{error_key} must be a boolean.")


def _integer(raw: Mapping[str, Any], key: str, default: int, label: str | None = None) -> int:
    error_key = label or key
    value = raw.get(key, default)
    return _parse_integer(value, error_key)


def _parse_integer(value: object, key: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{key} must be an integer.") from exc
    raise ConfigError(f"{key} must be an integer.")


def _float(raw: Mapping[str, Any], key: str, default: float, label: str | None = None) -> float:
    error_key = label or key
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{error_key} must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{error_key} must be a number.") from exc
    raise ConfigError(f"{error_key} must be a number.")


def _positive_float(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    label: str | None = None,
) -> float:
    error_key = label or key
    return _parse_positive_float(raw.get(key, default), error_key)


def _parse_positive_float(value: object, key: str) -> float:
    parsed = _parse_float(value, key)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigError(f"{key} must be greater than 0.")
    return parsed


def _parse_float(value: object, key: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number.") from exc
    raise ConfigError(f"{key} must be a number.")


def _temperature_unit(value: str, label: str = "temperature_unit") -> TemperatureUnit:
    normalized = _normalized_token(value)
    if normalized not in {"celsius", "fahrenheit", "auto"}:
        raise ConfigError(f"{label} must be one of: celsius, fahrenheit, auto.")
    return cast(TemperatureUnit, normalized)


def _first_crack_mode(value: str, label: str = "first_crack.mode") -> FirstCrackMode:
    normalized = _normalized_token(value)
    if normalized not in {"disabled", "audio", "manual"}:
        raise ConfigError(f"{label} must be one of: disabled, audio, manual.")
    return cast(FirstCrackMode, normalized)


def _model_precision(value: str, label: str = "first_crack.precision") -> ModelPrecision:
    normalized = _normalized_token(value)
    if normalized not in {"int8", "fp32"}:
        raise ConfigError(f"{label} must be one of: int8, fp32.")
    return cast(ModelPrecision, normalized)


def _audio_source(value: str, label: str = "audio.source") -> AudioSource:
    normalized = _normalized_token(value)
    if normalized not in {"microphone", "wav"}:
        raise ConfigError(f"{label} must be one of: microphone, wav.")
    return cast(AudioSource, normalized)


def _export_formats(value: object) -> tuple[ExportFormat, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError("logging.export_formats must be a list or tuple.")

    formats: list[ExportFormat] = []
    raw_formats = cast(list[object] | tuple[object, ...], value)
    for item in raw_formats:
        if not isinstance(item, str):
            raise ConfigError("logging.export_formats values must be strings.")
        normalized = _normalized_token(item)
        if normalized not in {"jsonl", "csv", "summary"}:
            raise ConfigError("logging.export_formats values must be jsonl, csv, or summary.")
        formats.append(cast(ExportFormat, normalized))
    return tuple(formats)


def _none_if_empty(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


def _required_string(value: str, key: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ConfigError(f"{key} must not be empty.")
    return stripped


def _normalized_token(value: str) -> str:
    return value.strip().lower()
