"""Reproducibility metadata and persistence-boundary sanitization."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import re
import subprocess
import sys
from base64 import b64encode
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
_URI_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+")


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
    """Return installed ML package and platform versions without eager
    imports."""

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
    """Collect the commit, branch, tracked diff, and untracked source
    identity."""

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
    """Return the host and process identity used for run tags."""

    return {
        "hostname": platform.node(),
        "python_executable": sys.executable,
    }


def _sanitize_malformed_uri(uri: str) -> str:
    scheme, separator, remainder = uri.partition("://")
    if not separator:
        return uri
    authority_end = len(remainder)
    for delimiter in ("/", "?", "#"):
        position = remainder.find(delimiter)
        if position >= 0:
            authority_end = min(authority_end, position)
    authority = remainder[:authority_end].rsplit("@", maxsplit=1)[-1]
    suffix = remainder[authority_end:]
    if not suffix.startswith("/"):
        suffix = ""
    suffix = suffix.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return f"{scheme}://{authority}{suffix}"


def sanitize_persisted_uri(uri: str) -> str:
    """Remove credentials and connection options from a persisted URI."""

    try:
        parsed = urlsplit(uri)
        if not parsed.scheme:
            return uri
        if parsed.hostname is None:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return _sanitize_malformed_uri(uri)


def sanitize_persisted_text(value: str) -> str:
    """Remove URI credentials and options from persisted free-form text."""

    return _URI_PATTERN.sub(lambda match: sanitize_persisted_uri(match.group(0)), value)


def sanitize_persisted_value(value: object) -> object:
    """Recursively sanitize every string in a persisted payload."""

    if isinstance(value, str):
        return sanitize_persisted_text(value)
    if isinstance(value, Mapping):
        return {str(key): sanitize_persisted_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(sanitize_persisted_value(item) for item in value)
    if isinstance(value, list):
        return [sanitize_persisted_value(item) for item in value]
    return value


def summarize_persisted_git_diff(diff: str) -> str:
    """Persist only the SHA-256 and byte count of a potentially secret diff."""

    encoded = diff.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return (
        "[omitted at persistence boundary; " f"sha256:{digest}; bytes:{len(encoded)}]"
    )
