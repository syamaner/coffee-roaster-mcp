from pathlib import Path

import pytest

from coffee_roaster_mcp.config import ConfigError, load_config


def test_default_config_allows_mock_run_without_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config(environ={})

    assert config.source_path is None
    assert config.transport.type == "stdio"
    assert config.roaster.driver == "mock"
    assert config.roaster.port is None
    assert config.roaster.baudrate == 115_200
    assert config.roaster.temperature_unit == "celsius"
    assert config.first_crack.mode == "disabled"
    assert config.first_crack.repo_id == "syamaner/coffee-first-crack-detection"
    assert config.first_crack.precision == "int8"
    assert config.logging.log_dir == Path("./logs")
    assert config.logging.export_formats == ("jsonl", "csv", "summary")
    assert config.session.auto_t0_detection_enabled is False


def test_missing_explicit_config_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(path=tmp_path / "missing.yaml")


def test_yaml_config_overrides_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        """
transport:
  type: STDIO
roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/cu.usbserial-1234
  baudrate: 57600
  temperature_unit: " auto "
  command_interval_seconds: 0.5
first_crack:
  mode: " audio "
  repo_id: syamaner/custom-first-crack
  revision: v0.1.0
  precision: " fp32 "
  local_model_dir: ./models/fp32
  onnx_threads: 4
  allow_manual_override: false
audio:
  input_device: roast-mic
  sample_rate: 48000
logging:
  log_dir: ./roast-logs
  sample_interval_seconds: 0.5
  export_formats:
    - jsonl
    - summary
session:
  auto_t0_detection_enabled: true
  ror_window_seconds: 45
  ror_min_sample_seconds: 12
""",
        encoding="utf-8",
    )

    config = load_config(config_path, environ={})

    assert config.source_path == config_path
    assert config.roaster.driver == "hottop_kn8828b_2k_plus"
    assert config.roaster.port == "/dev/cu.usbserial-1234"
    assert config.roaster.baudrate == 57_600
    assert config.roaster.temperature_unit == "auto"
    assert config.roaster.command_interval_seconds == 0.5
    assert config.first_crack.mode == "audio"
    assert config.first_crack.repo_id == "syamaner/custom-first-crack"
    assert config.first_crack.revision == "v0.1.0"
    assert config.first_crack.precision == "fp32"
    assert config.first_crack.local_model_dir == Path("./models/fp32")
    assert config.first_crack.onnx_threads == 4
    assert config.first_crack.allow_manual_override is False
    assert config.audio.input_device == "roast-mic"
    assert config.audio.sample_rate == 48_000
    assert config.logging.log_dir == Path("./roast-logs")
    assert config.logging.sample_interval_seconds == 0.5
    assert config.logging.export_formats == ("jsonl", "summary")
    assert config.session.auto_t0_detection_enabled is True
    assert config.session.ror_window_seconds == 45
    assert config.session.ror_min_sample_seconds == 12


def test_environment_overrides_file_config(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        """
roaster:
  driver: mock
first_crack:
  mode: disabled
  precision: int8
audio:
  input_device: file-mic
logging:
  log_dir: ./file-logs
""",
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        environ={
            "COFFEE_ROASTER_DRIVER": "hottop_kn8828b_2k_plus\n",
            "COFFEE_ROASTER_PORT": "/dev/cu.usbserial-env",
            "COFFEE_ROASTER_TEMP_UNIT": " fahrenheit ",
            "COFFEE_FIRST_CRACK_MODE": "manual\n",
            "COFFEE_FIRST_CRACK_REPO_ID": "syamaner/env-model",
            "COFFEE_FIRST_CRACK_REVISION": "main",
            "COFFEE_FIRST_CRACK_PRECISION": "fp32 ",
            "COFFEE_FIRST_CRACK_LOCAL_MODEL_DIR": "/models/env",
            "COFFEE_FIRST_CRACK_ONNX_THREADS": "8",
            "COFFEE_AUDIO_INPUT_DEVICE": "env-mic",
            "COFFEE_ROAST_LOG_DIR": "/tmp/roasts",
        },
    )

    assert config.roaster.driver == "hottop_kn8828b_2k_plus"
    assert config.roaster.port == "/dev/cu.usbserial-env"
    assert config.roaster.temperature_unit == "fahrenheit"
    assert config.first_crack.mode == "manual"
    assert config.first_crack.repo_id == "syamaner/env-model"
    assert config.first_crack.revision == "main"
    assert config.first_crack.precision == "fp32"
    assert config.first_crack.local_model_dir == Path("/models/env")
    assert config.first_crack.onnx_threads == 8
    assert config.audio.input_device == "env-mic"
    assert config.logging.log_dir == Path("/tmp/roasts")


def test_environment_config_path_is_supported(tmp_path: Path) -> None:
    config_path = tmp_path / "custom.yaml"
    config_path.write_text("roaster:\n  driver: env-path-driver\n", encoding="utf-8")

    config = load_config(environ={"COFFEE_ROASTER_MCP_CONFIG": f"  {config_path} \n"})

    assert config.source_path == config_path
    assert config.roaster.driver == "env-path-driver"


def test_invalid_enum_value_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("first_crack:\n  precision: fp16\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="first_crack.precision"):
        load_config(config_path, environ={})


def test_error_messages_include_section_context(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  baudrate: invalid\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="roaster.baudrate"):
        load_config(config_path, environ={})


def test_empty_log_dir_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_ROAST_LOG_DIR"):
        load_config(config_path, environ={"COFFEE_ROAST_LOG_DIR": "  "})


def test_empty_roaster_driver_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_ROASTER_DRIVER"):
        load_config(config_path, environ={"COFFEE_ROASTER_DRIVER": " \n"})


def test_empty_first_crack_repo_id_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("first_crack:\n  mode: disabled\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_FIRST_CRACK_REPO_ID"):
        load_config(config_path, environ={"COFFEE_FIRST_CRACK_REPO_ID": "  "})


def test_empty_config_path_environment_override_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config(
        environ={
            "COFFEE_ROASTER_MCP_CONFIG": " \n",
            "COFFEE_ROASTER_DRIVER": "mock",
        }
    )

    assert config.roaster.driver == "mock"


def test_temperature_unit_error_reports_context(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  temperature_unit: kelvin\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="roaster.temperature_unit"):
        load_config(config_path, environ={})
