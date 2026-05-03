"""Configuration loading for RoastPilot."""

from __future__ import annotations

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

    input_device: str | None = None
    sample_rate: int = 16_000


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
    config_path = _resolve_config_path(path, env)
    raw_config: Mapping[str, Any] = {}

    if config_path.exists():
        raw_config = _read_yaml_config(config_path)
    elif path is not None or "COFFEE_ROASTER_MCP_CONFIG" in env:
        raise ConfigError(f"Config file {config_path} does not exist.")

    source_path = config_path if config_path.exists() else None
    config = _config_from_mapping(raw_config, source_path=source_path)
    return _apply_env_overrides(config, env)


def _resolve_config_path(path: str | Path | None, environ: Mapping[str, str]) -> Path:
    if path is not None:
        return Path(path)

    env_path = environ.get("COFFEE_ROASTER_MCP_CONFIG")
    if env_path:
        return Path(env_path)

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
    transport_type = _normalized_token(_string(raw, "type", "stdio"))
    if transport_type != "stdio":
        raise ConfigError("transport.type must be 'stdio'.")
    return TransportConfig(type="stdio")


def _roaster_from_mapping(raw: Mapping[str, Any]) -> RoasterConfig:
    return RoasterConfig(
        driver=_string(raw, "driver", "mock"),
        port=_optional_string(raw, "port"),
        baudrate=_integer(raw, "baudrate", 115_200),
        temperature_unit=_temperature_unit(_string(raw, "temperature_unit", "celsius")),
        command_interval_seconds=_float(raw, "command_interval_seconds", 0.3),
    )


def _first_crack_from_mapping(raw: Mapping[str, Any]) -> FirstCrackConfig:
    local_model_dir = _optional_path(raw, "local_model_dir")
    return FirstCrackConfig(
        mode=_first_crack_mode(_string(raw, "mode", "disabled")),
        repo_id=_string(raw, "repo_id", "syamaner/coffee-first-crack-detection"),
        revision=_optional_string(raw, "revision"),
        precision=_model_precision(_string(raw, "precision", "int8")),
        local_model_dir=local_model_dir,
        onnx_threads=_integer(raw, "onnx_threads", 2),
        allow_manual_override=_boolean(raw, "allow_manual_override", True),
    )


def _audio_from_mapping(raw: Mapping[str, Any]) -> AudioConfig:
    return AudioConfig(
        input_device=_optional_string(raw, "input_device"),
        sample_rate=_integer(raw, "sample_rate", 16_000),
    )


def _logging_from_mapping(raw: Mapping[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        log_dir=_path(raw, "log_dir", Path("./logs")),
        sample_interval_seconds=_float(raw, "sample_interval_seconds", 1.0),
        export_formats=_export_formats(raw.get("export_formats", ("jsonl", "csv", "summary"))),
    )


def _session_from_mapping(raw: Mapping[str, Any]) -> SessionConfig:
    return SessionConfig(
        auto_t0_detection_enabled=_boolean(raw, "auto_t0_detection_enabled", False),
        ror_window_seconds=_integer(raw, "ror_window_seconds", 60),
        ror_min_sample_seconds=_integer(raw, "ror_min_sample_seconds", 10),
    )


def _apply_env_overrides(config: AppConfig, environ: Mapping[str, str]) -> AppConfig:
    roaster = config.roaster
    first_crack = config.first_crack
    audio = config.audio
    logging = config.logging

    if "COFFEE_ROASTER_DRIVER" in environ:
        roaster = replace(roaster, driver=environ["COFFEE_ROASTER_DRIVER"])
    if "COFFEE_ROASTER_PORT" in environ:
        roaster = replace(roaster, port=_none_if_empty(environ["COFFEE_ROASTER_PORT"]))
    if "COFFEE_ROASTER_TEMP_UNIT" in environ:
        roaster = replace(
            roaster,
            temperature_unit=_temperature_unit(environ["COFFEE_ROASTER_TEMP_UNIT"]),
        )

    if "COFFEE_FIRST_CRACK_MODE" in environ:
        first_crack = replace(
            first_crack,
            mode=_first_crack_mode(environ["COFFEE_FIRST_CRACK_MODE"]),
        )
    if "COFFEE_FIRST_CRACK_REPO_ID" in environ:
        first_crack = replace(first_crack, repo_id=environ["COFFEE_FIRST_CRACK_REPO_ID"])
    if "COFFEE_FIRST_CRACK_REVISION" in environ:
        first_crack = replace(
            first_crack,
            revision=_none_if_empty(environ["COFFEE_FIRST_CRACK_REVISION"]),
        )
    if "COFFEE_FIRST_CRACK_PRECISION" in environ:
        first_crack = replace(
            first_crack,
            precision=_model_precision(environ["COFFEE_FIRST_CRACK_PRECISION"]),
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

    if "COFFEE_AUDIO_INPUT_DEVICE" in environ:
        audio = replace(audio, input_device=_none_if_empty(environ["COFFEE_AUDIO_INPUT_DEVICE"]))

    if "COFFEE_ROAST_LOG_DIR" in environ:
        logging = replace(
            logging,
            log_dir=Path(_required_string(environ["COFFEE_ROAST_LOG_DIR"], "COFFEE_ROAST_LOG_DIR")),
        )

    return replace(
        config,
        roaster=roaster,
        first_crack=first_crack,
        audio=audio,
        logging=logging,
    )


def _string(raw: Mapping[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string.")
    return value


def _optional_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string or null.")
    return _none_if_empty(value)


def _path(raw: Mapping[str, Any], key: str, default: Path) -> Path:
    value = raw.get(key, default)
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise ConfigError(f"{key} must be a path string.")


def _optional_path(raw: Mapping[str, Any], key: str) -> Path | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return Path(value)
    raise ConfigError(f"{key} must be a path string or null.")


def _boolean(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{key} must be a boolean.")


def _integer(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    return _parse_integer(value, key)


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


def _float(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number.")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number.") from exc
    raise ConfigError(f"{key} must be a number.")


def _temperature_unit(value: str) -> TemperatureUnit:
    normalized = _normalized_token(value)
    if normalized not in {"celsius", "fahrenheit", "auto"}:
        raise ConfigError("temperature_unit must be one of: celsius, fahrenheit, auto.")
    return cast(TemperatureUnit, normalized)


def _first_crack_mode(value: str) -> FirstCrackMode:
    normalized = _normalized_token(value)
    if normalized not in {"disabled", "audio", "manual"}:
        raise ConfigError("first_crack.mode must be one of: disabled, audio, manual.")
    return cast(FirstCrackMode, normalized)


def _model_precision(value: str) -> ModelPrecision:
    normalized = _normalized_token(value)
    if normalized not in {"int8", "fp32"}:
        raise ConfigError("first_crack.precision must be one of: int8, fp32.")
    return cast(ModelPrecision, normalized)


def _export_formats(value: object) -> tuple[ExportFormat, ...]:
    if not isinstance(value, list | tuple):
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
