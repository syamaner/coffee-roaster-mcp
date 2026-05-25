"""Fixture metadata checks for committed audio replay assets."""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from typing import Any, cast

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "audio"
FIXTURE_STEM = "roastpilot-fc-replay-001"


def test_first_crack_replay_fixture_metadata_matches_wav() -> None:
    """Verify the committed labelled WAV fixture remains internally consistent."""
    wav_path = FIXTURE_DIR / f"{FIXTURE_STEM}.wav"
    labels_path = FIXTURE_DIR / f"{FIXTURE_STEM}.labels.json"
    manifest_path = FIXTURE_DIR / f"{FIXTURE_STEM}.manifest.json"

    labels = cast(dict[str, Any], json.loads(labels_path.read_text(encoding="utf-8")))
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    audio_manifest = cast(dict[str, Any], manifest["output_audio"])

    with wave.open(str(wav_path), "rb") as wav_file:
        duration_seconds = wav_file.getnframes() / wav_file.getframerate()
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2

    annotation = labels["annotations"][0]
    adjusted_interval = manifest["adjusted_label_interval_seconds"]
    trim_interval = manifest["trim_seconds"]
    original_interval = manifest["original_label_interval_seconds"]
    wav_sha256 = hashlib.sha256(wav_path.read_bytes()).hexdigest()

    assert labels["audio_file"] == wav_path.name
    assert labels["sample_rate"] == audio_manifest["sample_rate"] == 16_000
    assert labels["duration"] == audio_manifest["duration_seconds"] == duration_seconds
    assert annotation["label"] == "first_crack"
    assert annotation["start_time"] == adjusted_interval["start"]
    assert annotation["end_time"] == adjusted_interval["end"]
    assert 0.0 <= annotation["start_time"] < annotation["end_time"] <= duration_seconds
    assert adjusted_interval["start"] == original_interval["start"] - trim_interval["start"]
    assert adjusted_interval["end"] == trim_interval["end"] - trim_interval["start"]
    assert audio_manifest["sha256"] == wav_sha256
