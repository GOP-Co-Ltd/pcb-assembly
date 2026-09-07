"""再現性メタデータと、永続化境界での sanitize.

「どの commit の、どの依存版数で走らせたか」を run のタグと params に残す。

生の diff と untracked ファイル本文は credential を含みうるので永続化しない。

代わりに内容 fingerprint と件数だけをタグへ出す。

この module は ``ml-runtime`` すら要求しない。

torch の版数を取るときだけ関数内で import する。
"""

from __future__ import annotations

import importlib.metadata
import platform
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

import attrs

from ml.artifact.fingerprint import sha256_bytes, sha256_file

TRACKED_PACKAGE_NAMES: tuple[str, ...] = (
    "torch",
    "torchvision",
    "hydra-core",
    "hydra-optuna-sweeper",
    "optuna",
    "mlflow",
    "onnx",
    "onnxruntime",
    "onnxscript",
)

# untracked 本文を読み込む上限。超えた分は digest だけ残す。
_UNTRACKED_FILE_BYTE_LIMIT = 1024 * 1024
_UNTRACKED_TOTAL_BYTE_LIMIT = 8 * 1024 * 1024

_NOT_INSTALLED = "not-installed"
_NOT_AVAILABLE = "not-available"
_URI_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+")


def _read_only_mapping(values: Mapping[str, str]) -> Mapping[str, str]:
    """呼び出し側の辞書と切り離した、変更できない写像を返す."""

    return MappingProxyType(dict(values))


@attrs.frozen
class GitProvenance:
    """実行時点の作業ツリーの同一性.

    ``diff`` と ``untracked_content`` は credential を含みうる。

    :meth:`as_tags` はこの 2 つを出さず、:attr:`diff_fingerprint` だけを出す。
    """

    commit: str
    branch: str
    dirty: bool
    diff: str
    untracked_files: tuple[str, ...]
    untracked_content: str

    @classmethod
    def capture(cls, repository: Path) -> tuple[GitProvenance | None, str | None]:
        """作業ツリーの commit・branch・差分を収集する.

        git repository でない、あるいは git を実行できない場合は理由文字列を返す。
        """

        resolved = Path(repository).resolve()
        commit, error = _git(resolved, "rev-parse", "HEAD")
        if commit is None:
            return None, error
        branch, error = _git(resolved, "branch", "--show-current")
        if branch is None:
            return None, error
        status, error = _git(resolved, "status", "--porcelain")
        if status is None:
            return None, error
        diff, error = _git(resolved, "diff", "--binary", "HEAD")
        if diff is None:
            return None, error
        listed, error = _git(resolved, "ls-files", "--others", "--exclude-standard")
        if listed is None:
            return None, error
        untracked = tuple(line for line in listed.splitlines() if line)
        return (
            cls(
                commit=commit,
                branch=branch or "detached",
                dirty=bool(status),
                diff=diff,
                untracked_files=untracked,
                untracked_content=_untracked_content(resolved, untracked),
            ),
            None,
        )

    @property
    def diff_fingerprint(self) -> str:
        """追跡差分と untracked 本文をまとめた内容 fingerprint."""

        material = f"{self.diff}\n{self.untracked_content}".encode()
        return f"sha256:{sha256_bytes(material)}"

    def as_tags(self) -> dict[str, str]:
        """Run タグへ載せてよい範囲だけを返す.

        生の diff と untracked 本文は含めない。
        """

        return {
            "git.commit": self.commit,
            "git.branch": self.branch,
            "git.dirty": "true" if self.dirty else "false",
            "git.diff_fingerprint": self.diff_fingerprint,
            "git.untracked_file_count": str(len(self.untracked_files)),
        }


@attrs.frozen
class DependencyVersions:
    """実行環境の言語・platform・主要 package の版数."""

    versions: Mapping[str, str] = attrs.field(converter=_read_only_mapping)

    @classmethod
    def collect(cls) -> DependencyVersions:
        """Python・platform・追跡対象 package・CUDA の版数を集める.

        未 install の package は ``"not-installed"`` として残す。

        「入っていなかった」ことも再現性の情報のため。
        """

        versions = {
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
        for name in TRACKED_PACKAGE_NAMES:
            versions[name] = _installed_version(name)
        versions.update(_torch_build_versions())
        return cls(versions=versions)

    def as_params(self) -> dict[str, str]:
        """``dependency.`` 前置きで平坦化した params を返す."""

        return {f"dependency.{name}": value for name, value in self.versions.items()}


def sanitize_persisted_uri(uri: str) -> str:
    """URI から credential と query / fragment を落とす.

    scheme を持たない文字列はそのまま返す。

    接続オプションに token が載ることがあるため、query と fragment ごと捨てる。
    """

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return _sanitize_malformed_uri(uri)
    if not parsed.scheme:
        return uri
    if "@" not in parsed.netloc:
        # 落とす credential が無いので組み立て直さない。
        #
        # urlsplit は ``sqlite:///mlruns.db`` と ``sqlite:/mlruns.db`` を同じ成分へ
        # 潰すため、成分から復元すると別の URI になってしまう。
        return uri.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    if parsed.hostname is None:
        # host を持たない URI でも userinfo は落とす。credential 除去がこの関数の
        # 唯一の役目なので、解析できた形でも取りこぼさない。
        authority = parsed.netloc.rsplit("@", maxsplit=1)[-1]
        return f"{parsed.scheme}://{authority}{parsed.path}"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def sanitize_persisted_text(value: str) -> str:
    """自由文に含まれる URI をすべて sanitize する."""

    return _URI_PATTERN.sub(lambda match: sanitize_persisted_uri(match.group(0)), value)


def _sanitize_malformed_uri(uri: str) -> str:
    """``urlsplit`` が解析できない URI から、手作業で credential を落とす."""

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
        return f"{scheme}://{authority}"
    suffix = suffix.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return f"{scheme}://{authority}{suffix}"


def _git(repository: Path, *arguments: str) -> tuple[str | None, str | None]:
    """Git を 1 度実行し、標準出力か理由文字列を返す."""

    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return None, f"git を実行できません: {error}"
    if result.returncode != 0:
        return None, (
            f"git {' '.join(arguments)} が失敗しました"
            f"（終了コード {result.returncode}）: {result.stderr.strip()}"
        )
    return result.stdout.rstrip("\n"), None


def _untracked_content(repository: Path, untracked_files: tuple[str, ...]) -> str:
    """Untracked ファイルの本文を、1 本の文字列へ畳む.

    本文は追跡差分と同じく生のまま持つ（永続化はしない）。

    バイナリは復号できない byte を置換して読むが、見出し行に生 byte の digest を
    載せるので :attr:`GitProvenance.diff_fingerprint` は正確なままになる。

    大きなファイルと合計サイズには上限を置き、超えた分は本文を落として digest だけ
    残す。

    untracked の dataset や checkpoint を run 開始時に丸ごとメモリへ読まないため。

    有界なのはメモリだけで、digest を取る I/O は上限を超えたファイルも最後まで読む。

    ``diff_fingerprint`` を内容の変化に追従させるには全 byte を読む必要があるため。
    """

    parts: list[str] = []
    remaining = _UNTRACKED_TOTAL_BYTE_LIMIT
    for relative_path in sorted(untracked_files):
        file_path = repository / relative_path
        if not file_path.is_file():
            continue
        size = file_path.stat().st_size
        if size > _UNTRACKED_FILE_BYTE_LIMIT or size > remaining:
            parts.extend(
                (
                    f"untracked a/{relative_path} b/{relative_path}",
                    f"size {size}; sha256:{sha256_file(file_path)}",
                    "[本文は大きすぎるため省略]",
                )
            )
            continue
        content = file_path.read_bytes()
        remaining -= len(content)
        parts.extend(
            (
                f"untracked a/{relative_path} b/{relative_path}",
                f"size {len(content)}; sha256:{sha256_bytes(content)}",
                content.decode("utf-8", errors="replace"),
            )
        )
    return "\n".join(parts)


def _installed_version(package_name: str) -> str:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return _NOT_INSTALLED


def _torch_build_versions() -> dict[str, str]:
    """Torch が結び付いている CUDA / cuDNN の版数を返す.

    この module を依存フリー層に保つため、torch は関数内で import する。
    """

    try:
        import torch
    except ImportError:
        return {"cuda": _NOT_INSTALLED, "cudnn": _NOT_INSTALLED}
    return {
        "cuda": torch.version.cuda or _NOT_AVAILABLE,
        "cudnn": str(torch.backends.cudnn.version() or _NOT_AVAILABLE),
    }


__all__ = [
    "TRACKED_PACKAGE_NAMES",
    "DependencyVersions",
    "GitProvenance",
    "sanitize_persisted_text",
    "sanitize_persisted_uri",
]
