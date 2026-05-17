from pathlib import Path

import pytest

from coffee_roaster_mcp.artifacts import ArtifactResolutionError, resolve_hugging_face_artifact
from coffee_roaster_mcp.config import FirstCrackConfig


class RecordingDownloader:
    def __init__(self, local_path: str = "/tmp/hf-cache/model.onnx") -> None:
        self.local_path = local_path
        self.calls: list[dict[str, str | None]] = []

    def __call__(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str | None,
    ) -> str:
        self.calls.append(
            {
                "repo_id": repo_id,
                "filename": filename,
                "revision": revision,
            }
        )
        return self.local_path


def test_resolves_first_crack_artifact_from_default_repo() -> None:
    downloader = RecordingDownloader("/tmp/hf-cache/model_quantized.onnx")

    artifact = resolve_hugging_face_artifact(
        FirstCrackConfig(),
        "onnx/int8/model_quantized.onnx",
        downloader=downloader,
    )

    assert artifact.repo_id == "syamaner/coffee-first-crack-detection"
    assert artifact.revision is None
    assert artifact.filename == "onnx/int8/model_quantized.onnx"
    assert artifact.local_path == Path("/tmp/hf-cache/model_quantized.onnx")
    assert downloader.calls == [
        {
            "repo_id": "syamaner/coffee-first-crack-detection",
            "filename": "onnx/int8/model_quantized.onnx",
            "revision": None,
        }
    ]


def test_resolver_honors_configured_revision() -> None:
    downloader = RecordingDownloader()
    config = FirstCrackConfig(revision="v0.1.0")

    artifact = resolve_hugging_face_artifact(
        config,
        "feature_extractor/preprocessor_config.json",
        downloader=downloader,
    )

    assert artifact.revision == "v0.1.0"
    assert downloader.calls == [
        {
            "repo_id": "syamaner/coffee-first-crack-detection",
            "filename": "feature_extractor/preprocessor_config.json",
            "revision": "v0.1.0",
        }
    ]


def test_resolver_honors_configured_repo_id() -> None:
    downloader = RecordingDownloader()
    config = FirstCrackConfig(repo_id="syamaner/custom-first-crack")

    resolve_hugging_face_artifact(config, "model.onnx", downloader=downloader)

    assert downloader.calls[0]["repo_id"] == "syamaner/custom-first-crack"


@pytest.mark.parametrize(
    "filename", ["", "  ", "/model.onnx", "../model.onnx", "onnx/../model.onnx"]
)
def test_invalid_artifact_filename_fails_before_download(filename: str) -> None:
    downloader = RecordingDownloader()

    with pytest.raises(ArtifactResolutionError, match="filename"):
        resolve_hugging_face_artifact(FirstCrackConfig(), filename, downloader=downloader)

    assert downloader.calls == []


def test_download_failure_includes_artifact_context() -> None:
    def failing_downloader(*, repo_id: str, filename: str, revision: str | None) -> str:
        raise RuntimeError("not found")

    with pytest.raises(ArtifactResolutionError) as exc_info:
        resolve_hugging_face_artifact(
            FirstCrackConfig(revision="pinned-release"),
            "onnx/int8/model_quantized.onnx",
            downloader=failing_downloader,
        )

    message = str(exc_info.value)
    assert "onnx/int8/model_quantized.onnx" in message
    assert "syamaner/coffee-first-crack-detection" in message
    assert "pinned-release" in message
