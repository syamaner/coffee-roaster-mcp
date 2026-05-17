"""Hugging Face artifact resolution for first-crack model files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from coffee_roaster_mcp.config import FirstCrackConfig


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
        local_path: Local cache path returned by the Hugging Face Hub client.
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
    """Resolve one released first-crack artifact from Hugging Face Hub.

    Args:
        config: First-crack configuration containing the model repo and revision.
        filename: Repository-relative artifact path to resolve.
        downloader: Optional test double for the Hugging Face download call.

    Returns:
        Metadata for the resolved local artifact path.

    Raises:
        ArtifactResolutionError: If the filename is invalid or the download fails.
    """
    normalized_filename = _validate_hub_filename(filename)
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


def _hf_hub_download(
    *,
    repo_id: str,
    filename: str,
    revision: str | None,
) -> str:
    hub_module = import_module("huggingface_hub")
    download = cast(Callable[..., str], hub_module.__dict__["hf_hub_download"])
    return download(repo_id=repo_id, filename=filename, revision=revision)
