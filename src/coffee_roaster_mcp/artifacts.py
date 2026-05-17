"""Hugging Face artifact resolution for first-crack model files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from coffee_roaster_mcp.config import FirstCrackConfig

INT8_ONNX_MODEL_FILENAME = "onnx/int8/model_quantized.onnx"
FP32_ONNX_MODEL_FILENAME = "onnx/fp32/model.onnx"


class ArtifactResolutionError(RuntimeError):
    """Raised when a configured first-crack artifact cannot be resolved."""


class HuggingFaceDownloader(Protocol):
    """Callable interface for Hugging Face Hub artifact downloads."""

    def __call__(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str | None,
    ) -> str:
        """Download one artifact from a Hugging Face Hub repository."""
        ...


@dataclass(frozen=True)
class ResolvedArtifact:
    """Resolved first-crack artifact metadata.

    Attributes:
        repo_id: Hugging Face repository that supplied the artifact.
        revision: Configured repository revision, or `None` for the Hub default.
        filename: Repository-relative artifact path.
        local_path: Local filesystem path for the resolved artifact.
    """

    repo_id: str
    revision: str | None
    filename: str
    local_path: Path


def resolve_hugging_face_artifact(
    config: FirstCrackConfig,
    filename: str,
    *,
    downloader: HuggingFaceDownloader | None = None,
) -> ResolvedArtifact:
    """Resolve one released first-crack artifact from local storage or Hugging Face Hub.

    Args:
        config: First-crack configuration containing local directory, model repo, and revision.
        filename: Repository-relative artifact path to resolve.
        downloader: Optional test double for the Hugging Face download call.

    Returns:
        Metadata for the resolved local artifact path.

    Raises:
        ArtifactResolutionError: If the filename is invalid or the download fails.
    """
    normalized_filename = _validate_hub_filename(filename)
    if config.local_model_dir is not None:
        local_path = _resolve_local_artifact(config.local_model_dir, normalized_filename)
        return ResolvedArtifact(
            repo_id=config.repo_id,
            revision=config.revision,
            filename=normalized_filename,
            local_path=local_path,
        )

    download = _hf_hub_download if downloader is None else downloader

    try:
        local_path = download(
            repo_id=config.repo_id,
            filename=normalized_filename,
            revision=config.revision,
        )
    except Exception as exc:
        revision_label = config.revision or "default"
        raise ArtifactResolutionError(
            "Could not resolve Hugging Face artifact "
            f"{normalized_filename!r} from {config.repo_id!r} at revision {revision_label!r}: {exc}"
        ) from exc

    return ResolvedArtifact(
        repo_id=config.repo_id,
        revision=config.revision,
        filename=normalized_filename,
        local_path=Path(local_path),
    )


def resolve_first_crack_onnx_model(
    config: FirstCrackConfig,
    *,
    downloader: HuggingFaceDownloader | None = None,
) -> ResolvedArtifact:
    """Resolve the configured first-crack ONNX model artifact.

    Args:
        config: First-crack configuration containing precision, repo, and revision.
        downloader: Optional test double for the Hugging Face download call.

    Returns:
        Metadata for the resolved local ONNX model artifact path.

    Raises:
        ArtifactResolutionError: If the configured precision is unsupported or
            the artifact cannot be resolved.
    """
    precision = str(config.precision)
    match precision:
        case "int8":
            filename = INT8_ONNX_MODEL_FILENAME
        case "fp32":
            filename = FP32_ONNX_MODEL_FILENAME
        case unsupported_precision:
            raise ArtifactResolutionError(
                "Unsupported first-crack ONNX precision "
                f"{unsupported_precision!r}; expected 'int8' or 'fp32'."
            )

    return resolve_hugging_face_artifact(
        config,
        filename,
        downloader=downloader,
    )


def _validate_hub_filename(filename: str) -> str:
    normalized = filename.strip()
    if not normalized:
        raise ArtifactResolutionError("Hugging Face artifact filename must not be empty.")
    if "\\" in normalized:
        raise ArtifactResolutionError(
            "Hugging Face artifact filename must use repository-relative POSIX separators."
        )

    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactResolutionError(
            "Hugging Face artifact filename must be a repository-relative path."
        )
    return path.as_posix()


def _resolve_local_artifact(local_model_dir: Path, filename: str) -> Path:
    local_path = local_model_dir.joinpath(*PurePosixPath(filename).parts)
    if not local_path.is_file():
        raise ArtifactResolutionError(
            "Could not resolve local first-crack artifact "
            f"{filename!r} from local_model_dir {str(local_model_dir)!r}: "
            f"{local_path} is not a file."
        )
    return local_path


def _hf_hub_download(
    *,
    repo_id: str,
    filename: str,
    revision: str | None,
) -> str:
    hub_module = import_module("huggingface_hub")
    download = cast(Callable[..., str], hub_module.__dict__["hf_hub_download"])
    return download(repo_id=repo_id, filename=filename, revision=revision)
