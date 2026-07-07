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
    assert config.first_crack.confidence_threshold == 0.9
    assert config.first_crack.min_positive_windows == 1
    assert config.first_crack.confirmation_window_seconds == 20.0
    assert config.audio.source == "microphone"
    assert config.audio.wav_path is None
    assert config.audio.replay_mode == "realtime"
    assert config.audio.window_seconds == 1.0
    assert config.audio.overlap == 0.0
    assert config.audio.hop_seconds is None
    assert config.logging.log_dir == Path("./logs")
    assert config.logging.sample_interval_seconds == 5.0
    assert config.logging.export_formats == ("jsonl", "csv", "summary")
    assert config.session.auto_t0_detection_enabled is False
    assert config.session.auto_t0_drop_threshold_c == 25.0
    assert config.ambient.mode == "disabled"
    assert config.ambient.device is None
    assert config.ambient.poll_interval_seconds == 30.0


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
  confidence_threshold: 0.6
  min_positive_windows: 5
  confirmation_window_seconds: 20.0
  allow_manual_override: false
audio:
  input_device: roast-mic
  sample_rate: 48000
  window_seconds: 10.0
  overlap: 0.7
logging:
  log_dir: ./roast-logs
  sample_interval_seconds: 0.5
  export_formats:
    - jsonl
    - summary
session:
  auto_t0_detection_enabled: true
  auto_t0_drop_threshold_c: 20.5
  ror_window_seconds: 45
  ror_min_sample_seconds: 12
ambient:
  mode: " yoctopuce "
  device: METEOMK2-98765
  poll_interval_seconds: 15.0
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
    assert config.first_crack.confidence_threshold == 0.6
    assert config.first_crack.min_positive_windows == 5
    assert config.first_crack.confirmation_window_seconds == 20.0
    assert config.first_crack.allow_manual_override is False
    assert config.audio.source == "microphone"
    assert config.audio.input_device == "roast-mic"
    assert config.audio.sample_rate == 48_000
    assert config.audio.wav_path is None
    assert config.audio.replay_mode == "realtime"
    assert config.audio.window_seconds == 10.0
    assert config.audio.overlap == 0.7
    assert config.audio.hop_seconds is None
    assert config.logging.log_dir == Path("./roast-logs")
    assert config.logging.sample_interval_seconds == 0.5
    assert config.logging.export_formats == ("jsonl", "summary")
    assert config.session.auto_t0_detection_enabled is True
    assert config.session.auto_t0_drop_threshold_c == 20.5
    assert config.session.ror_window_seconds == 45
    assert config.session.ror_min_sample_seconds == 12
    assert config.ambient.mode == "yoctopuce"
    assert config.ambient.device == "METEOMK2-98765"
    assert config.ambient.poll_interval_seconds == 15.0


def test_logging_sample_interval_must_be_positive(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        """
logging:
  sample_interval_seconds: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="logging.sample_interval_seconds"):
        load_config(config_path, environ={})


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
  source: wav
  input_device: file-mic
  sample_rate: 8000
  wav_path: ./fixture.wav
  replay_mode: detector_paced
  window_seconds: 5.0
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
            "COFFEE_FIRST_CRACK_CONFIDENCE_THRESHOLD": "0.7",
            "COFFEE_FIRST_CRACK_MIN_POSITIVE_WINDOWS": "3",
            "COFFEE_FIRST_CRACK_CONFIRMATION_WINDOW_SECONDS": "30",
            "COFFEE_AUDIO_SOURCE": "microphone",
            "COFFEE_AUDIO_INPUT_DEVICE": "env-mic",
            "COFFEE_AUDIO_SAMPLE_RATE": "16000",
            "COFFEE_AUDIO_WAV_PATH": "",
            "COFFEE_AUDIO_REPLAY_MODE": "realtime",
            "COFFEE_AUDIO_WINDOW_SECONDS": "2.5",
            "COFFEE_AUDIO_OVERLAP": "0.25",
            "COFFEE_AUDIO_HOP_SECONDS": "1.25",
            "COFFEE_ROAST_LOG_DIR": "/tmp/roasts",
            "COFFEE_AUTO_T0_DROP_THRESHOLD_C": "30",
            "COFFEE_AMBIENT_MODE": "yoctopuce\n",
            "COFFEE_AMBIENT_DEVICE": "METEOMK2-12345",
            "COFFEE_AMBIENT_POLL_INTERVAL_SECONDS": "45",
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
    assert config.first_crack.confidence_threshold == 0.7
    assert config.first_crack.min_positive_windows == 3
    assert config.first_crack.confirmation_window_seconds == 30.0
    assert config.audio.source == "microphone"
    assert config.audio.input_device == "env-mic"
    assert config.audio.sample_rate == 16_000
    assert config.audio.wav_path is None
    assert config.audio.replay_mode == "realtime"
    assert config.audio.window_seconds == 2.5
    assert config.audio.overlap == 0.25
    assert config.audio.hop_seconds == 1.25
    assert config.logging.log_dir == Path("/tmp/roasts")
    assert config.session.auto_t0_drop_threshold_c == 30.0
    assert config.ambient.mode == "yoctopuce"
    assert config.ambient.device == "METEOMK2-12345"
    assert config.ambient.poll_interval_seconds == 45.0


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

    config_path.write_text("audio:\n  replay_mode: warp_speed\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="audio.replay_mode"):
        load_config(config_path, environ={})


def test_invalid_audio_source_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("audio:\n  source: bluetooth\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="audio.source"):
        load_config(config_path, environ={})


def test_invalid_ambient_mode_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("ambient:\n  mode: bluetooth\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="ambient.mode"):
        load_config(config_path, environ={})


def test_invalid_ambient_poll_interval_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "ambient:\n  mode: yoctopuce\n  poll_interval_seconds: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ambient.poll_interval_seconds"):
        load_config(config_path, environ={})


def test_invalid_ambient_mode_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_AMBIENT_MODE"):
        load_config(config_path, environ={"COFFEE_AMBIENT_MODE": "bluetooth"})


def test_invalid_ambient_poll_interval_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_AMBIENT_POLL_INTERVAL_SECONDS"):
        load_config(
            config_path,
            environ={"COFFEE_AMBIENT_POLL_INTERVAL_SECONDS": "-1"},
        )


def test_empty_ambient_device_environment_override_clears_selector(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "ambient:\n  mode: yoctopuce\n  device: METEOMK2-11111\n",
        encoding="utf-8",
    )

    config = load_config(config_path, environ={"COFFEE_AMBIENT_DEVICE": "  "})

    assert config.ambient.device is None


@pytest.mark.parametrize(
    ("yaml_body", "message"),
    (
        ("first_crack:\n  confidence_threshold: 1.5\n", "first_crack.confidence_threshold"),
        ("first_crack:\n  confidence_threshold: nan\n", "first_crack.confidence_threshold"),
        ("first_crack:\n  min_positive_windows: 0\n", "first_crack.min_positive_windows"),
        (
            "first_crack:\n  confirmation_window_seconds: -1\n",
            "first_crack.confirmation_window_seconds",
        ),
        ("audio:\n  overlap: 1.0\n", "audio.overlap"),
        ("audio:\n  overlap: -0.1\n", "audio.overlap"),
        ("audio:\n  hop_seconds: 0\n", "audio.hop_seconds"),
        (
            "audio:\n  window_seconds: 1.0\n  hop_seconds: 2.0\n",
            "audio.hop_seconds",
        ),
    ),
)
def test_invalid_detector_window_config_fails(
    tmp_path: Path,
    yaml_body: str,
    message: str,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path, environ={})


@pytest.mark.parametrize(
    ("env", "message"),
    (
        (
            {"COFFEE_FIRST_CRACK_CONFIDENCE_THRESHOLD": "-0.1"},
            "COFFEE_FIRST_CRACK_CONFIDENCE_THRESHOLD",
        ),
        (
            {"COFFEE_FIRST_CRACK_MIN_POSITIVE_WINDOWS": "0"},
            "COFFEE_FIRST_CRACK_MIN_POSITIVE_WINDOWS",
        ),
        (
            {"COFFEE_FIRST_CRACK_CONFIRMATION_WINDOW_SECONDS": "nan"},
            "COFFEE_FIRST_CRACK_CONFIRMATION_WINDOW_SECONDS",
        ),
        ({"COFFEE_AUDIO_OVERLAP": "1.0"}, "COFFEE_AUDIO_OVERLAP"),
        ({"COFFEE_AUDIO_HOP_SECONDS": "-1"}, "COFFEE_AUDIO_HOP_SECONDS"),
        (
            {
                "COFFEE_AUDIO_WINDOW_SECONDS": "1.0",
                "COFFEE_AUDIO_HOP_SECONDS": "2.0",
            },
            "COFFEE_AUDIO_HOP_SECONDS",
        ),
    ),
)
def test_invalid_detector_window_environment_overrides_fail(
    tmp_path: Path,
    env: dict[str, str],
    message: str,
) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path, environ=env)


def test_invalid_auto_t0_threshold_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("session:\n  auto_t0_drop_threshold_c: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="session.auto_t0_drop_threshold_c"):
        load_config(config_path, environ={})


@pytest.mark.parametrize("threshold", ["nan", "inf", "-inf"])
def test_non_finite_auto_t0_threshold_fails(tmp_path: Path, threshold: str) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        f"session:\n  auto_t0_drop_threshold_c: {threshold}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="session.auto_t0_drop_threshold_c"):
        load_config(config_path, environ={})


def test_non_finite_auto_t0_threshold_environment_override_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("roaster:\n  driver: mock\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_AUTO_T0_DROP_THRESHOLD_C"):
        load_config(config_path, environ={"COFFEE_AUTO_T0_DROP_THRESHOLD_C": "nan"})


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


def test_recording_defaults_are_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config(environ={})

    assert config.recording.enabled is False
    assert config.recording.autocapture is False
    assert config.recording.export_location is None
    assert config.recording.sample_rate is None
    assert config.recording.device is None
    assert config.recording.channels is None


def test_recording_yaml_config_is_parsed(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        """
recording:
  enabled: true
  autocapture: "yes"
  export_location: ~/roasts/captures
  sample_rate: 44100
  device: aggregate-2ch
  channels:
    - 0
    - 1
""",
        encoding="utf-8",
    )

    config = load_config(config_path, environ={})

    assert config.recording.enabled is True
    assert config.recording.autocapture is True
    assert config.recording.export_location == Path("~/roasts/captures")
    assert config.recording.sample_rate == 44_100
    assert config.recording.device == "aggregate-2ch"
    assert config.recording.channels == (0, 1)


def test_recording_environment_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("recording:\n  enabled: false\n", encoding="utf-8")

    config = load_config(
        config_path,
        environ={
            "COFFEE_RECORDING_ENABLED": "true",
            "COFFEE_RECORDING_AUTOCAPTURE": "1",
            "COFFEE_RECORDING_EXPORT_LOCATION": "  /tmp/captures \n",
            "COFFEE_RECORDING_SAMPLE_RATE": "48000",
        },
    )

    assert config.recording.enabled is True
    assert config.recording.autocapture is True
    assert config.recording.export_location == Path("/tmp/captures")
    assert config.recording.sample_rate == 48_000


def test_recording_empty_environment_clears_optionals(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        "recording:\n  export_location: ~/x\n  sample_rate: 44100\n",
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        environ={
            "COFFEE_RECORDING_EXPORT_LOCATION": "   ",
            "COFFEE_RECORDING_SAMPLE_RATE": "  ",
        },
    )

    assert config.recording.export_location is None
    assert config.recording.sample_rate is None


def test_recording_invalid_sample_rate_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("recording:\n  sample_rate: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="recording.sample_rate"):
        load_config(config_path, environ={})


def test_recording_invalid_channels_fail(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("recording:\n  channels:\n    - -1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="recording.channels"):
        load_config(config_path, environ={})


def test_recording_invalid_enabled_environment_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("recording:\n  enabled: false\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COFFEE_RECORDING_ENABLED"):
        load_config(config_path, environ={"COFFEE_RECORDING_ENABLED": "maybe"})


def test_recording_devices_yaml_and_env(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(
        """
recording:
  enabled: true
  autocapture: true
  devices:
    - USB PnP
    - ATR2100x
""",
        encoding="utf-8",
    )

    config = load_config(config_path, environ={})
    assert config.recording.devices == ("USB PnP", "ATR2100x")

    overridden = load_config(
        config_path,
        environ={"COFFEE_RECORDING_DEVICES": " USB PnP , ATR2100x , "},
    )
    assert overridden.recording.devices == ("USB PnP", "ATR2100x")

    # An all-empty CSV clears the list back to None.
    cleared = load_config(config_path, environ={"COFFEE_RECORDING_DEVICES": " , "})
    assert cleared.recording.devices is None


def test_recording_devices_invalid_entries_fail(tmp_path: Path) -> None:
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text("recording:\n  devices:\n    - 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="recording.devices"):
        load_config(config_path, environ={})

    config_path.write_text("recording:\n  devices:\n    - '   '\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="recording.devices"):
        load_config(config_path, environ={})

    config_path.write_text("recording:\n  devices: USB PnP\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="recording.devices"):
        load_config(config_path, environ={})
