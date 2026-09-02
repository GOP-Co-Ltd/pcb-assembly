"""ML runの再現性に必要な実行環境provenance."""

from __future__ import annotations

import importlib.metadata
import platform
import subprocess
import sys
from base64 import b64encode
from dataclasses import asdict, dataclass
from pathlib import Path

_PACKAGES = (
    "torch",
    "torchvision",
    "hydra-core",
    "hydra-optuna-sweeper",
    "optuna",
    "mlflow",
    "onnx",
    "onnxruntime",
)


@dataclass(frozen=True)
class GitProvenance:
    commit: str
    branch: str
    dirty: bool
    diff: str
    untracked_files: tuple[str, ...]
    untracked_content: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def dependency_versions() -> dict[str, str]:
    """利用可能なML関連packageとplatformのversionを返す."""

    versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    for package in _PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    try:
        import torch

        versions["cuda"] = torch.version.cuda or "not-available"
        versions["cudnn"] = str(torch.backends.cudnn.version() or "not-available")
    except ImportError:
        versions["cuda"] = "not-installed"
        versions["cudnn"] = "not-installed"
    return versions


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def git_provenance(repo: Path) -> GitProvenance:
    """Commit、branch、dirty diffを収集する."""

    resolved = repo.resolve()
    commit = _git(resolved, "rev-parse", "HEAD")
    branch = _git(resolved, "branch", "--show-current") or "detached"
    status = _git(resolved, "status", "--porcelain")
    tracked_diff = _git(resolved, "diff", "--binary", "HEAD")
    untracked = tuple(
        line
        for line in _git(
            resolved,
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "tests",
            "docs",
            "pyproject.toml",
            "Makefile",
            ".gitlab-ci.yml",
        ).splitlines()
        if line
    )
    untracked_parts: list[str] = []
    for relative_path in sorted(untracked):
        file_path = resolved / relative_path
        if not file_path.is_file():
            continue
        content = file_path.read_bytes()
        untracked_parts.extend(
            (
                f"diff --pcbasm-untracked a/{relative_path} b/{relative_path}",
                f"encoding base64; size {len(content)}",
                b64encode(content).decode("ascii"),
            )
        )
    return GitProvenance(
        commit=commit,
        branch=branch,
        dirty=bool(status),
        diff=tracked_diff,
        untracked_files=untracked,
        untracked_content="\n".join(untracked_parts),
    )


def runtime_identity() -> dict[str, str]:
    """MLflow tag用のhost/process identityを返す."""

    return {
        "hostname": platform.node(),
        "python_executable": sys.executable,
    }


__all__ = [
    "GitProvenance",
    "dependency_versions",
    "git_provenance",
    "runtime_identity",
]
