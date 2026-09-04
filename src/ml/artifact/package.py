"""不変な成果物パッケージの公開と、active pointer の切替.

パッケージは「完成してから 1 度だけ rename する」。公開後は中身を書き換えず、
差し替えは新しいディレクトリを作って pointer を切り替える。pointer は直前の
1 世代を保持し、:meth:`ActivePointer.rollback` で手動で戻せる。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping, Set
from pathlib import Path
from types import MappingProxyType

import attrs

from ml.artifact.atomic import fsync_directory
from ml.artifact.document import DocumentKind
from ml.artifact.fingerprint import sha256_file
from ml.serialization import make_strict_converter

CHECKSUM_FILENAME = "SHA256SUMS"
ACTIVE_POINTER_DOCUMENT = DocumentKind(
    kind="ml-active-package-pointer", schema_version=1
)

_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
_DIGEST_LENGTH = 64
_PACKAGE_ID_DIGEST_LENGTH = 16
_CONVERTER = make_strict_converter()


def _read_only_checksums(checksums: Mapping[str, str]) -> Mapping[str, str]:
    """呼び出し側が保持する mapping を後から書き換えられないようにする."""

    return MappingProxyType(dict(checksums))


@attrs.frozen
class ImmutablePackage:
    """公開済みパッケージと、その全 payload のダイジェスト."""

    path: Path
    checksums: Mapping[str, str] = attrs.field(converter=_read_only_checksums)

    @classmethod
    def publish(
        cls,
        output_directory: Path,
        *,
        payload_filenames: Iterable[str],
        write_payloads: Callable[[Path], object],
    ) -> ImmutablePackage:
        """Payload を非公開の場所に揃えてから、1 度の rename で公開する."""

        filenames = frozenset(_validated_filenames(payload_filenames))
        destination = Path(output_directory).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(f"model package が既に存在します: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        try:
            write_payloads(temporary)
            checksums = _payload_checksums(temporary, filenames)
            (temporary / CHECKSUM_FILENAME).write_text(
                _format_checksums(checksums), encoding="utf-8"
            )
            os.replace(temporary, destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        fsync_directory(destination.parent)
        return cls(path=destination, checksums=checksums)

    @classmethod
    def verify(
        cls, package: Path, *, expected_filenames: Iterable[str] | None = None
    ) -> tuple[ImmutablePackage | None, str | None]:
        """記録済みダイジェストと突き合わせ、改竄や欠落を検出する."""

        path = Path(package).expanduser().resolve()
        if not path.is_dir():
            return None, f"model package が見つかりません: {path}"
        checksum_path = path / CHECKSUM_FILENAME
        if not checksum_path.is_file():
            return None, f"{CHECKSUM_FILENAME} がありません: {path}"
        recorded, error = _read_checksums(checksum_path)
        if recorded is None:
            return None, error

        present = {entry.name for entry in path.iterdir()} - {CHECKSUM_FILENAME}
        if difference := _describe_difference(frozenset(recorded), present):
            return (
                None,
                f"package のファイル構成が {CHECKSUM_FILENAME} と一致しません{difference}",
            )
        if expected_filenames is not None:
            expected = frozenset(expected_filenames)
            if difference := _describe_difference(expected, frozenset(recorded)):
                return None, f"package のファイル構成が期待値と一致しません{difference}"

        for filename in sorted(recorded):
            payload = path / filename
            if payload.is_symlink() or not payload.is_file():
                return None, f"package payload が通常ファイルではありません: {filename}"
        if tampered := sorted(
            filename
            for filename, digest in recorded.items()
            if sha256_file(path / filename) != digest
        ):
            return (
                None,
                f"package の内容が {CHECKSUM_FILENAME} と一致しません: {tampered}",
            )
        return cls(path=path, checksums=recorded), None


@attrs.frozen
class ActivePointer:
    """現在 active なパッケージと、戻し先の直前 1 世代."""

    pointer_path: Path
    active_package_path: Path
    previous_package_path: Path | None
    active_package_id: str
    active_package_sha256: str

    @classmethod
    def switch(
        cls, pointer_file: Path, package: Path
    ) -> tuple[ActivePointer | None, str | None]:
        """検証済みパッケージを active にし、直前の 1 世代を保持する.

        pointer の置き場所が誤っている場合は呼び出し側の不変条件違反として
        ``ValueError`` を送出する。パッケージの検証失敗は運用時に起こりうる状態なので、
        例外ではなく理由文字列で返す。
        """

        pointer_path = Path(pointer_file).expanduser().resolve()
        package_path = Path(package).expanduser().resolve()
        if pointer_path.is_relative_to(package_path):
            raise ValueError(
                f"active pointer は model package の外に置いてください: {pointer_path}"
            )
        verified, error = ImmutablePackage.verify(package_path)
        if verified is None:
            return None, error

        previous: Path | None = None
        if pointer_path.exists():
            current, error = cls.load(pointer_path)
            if current is None:
                return None, error
            previous = current.active_package_path
            if previous == package_path:
                previous = current.previous_package_path
        return _write_pointer(pointer_path, package_path, previous), None

    @classmethod
    def load(cls, pointer_file: Path) -> tuple[ActivePointer | None, str | None]:
        """Pointer file を読み、失敗したら理由を返す."""

        pointer_path = Path(pointer_file).expanduser().resolve()
        record, error = ACTIVE_POINTER_DOCUMENT.load(
            pointer_path, _PointerRecord, converter=_CONVERTER
        )
        if record is None:
            return None, error
        return _active_pointer(pointer_path, record), None

    @classmethod
    def rollback(cls, pointer_file: Path) -> tuple[ActivePointer | None, str | None]:
        """直前の 1 世代へ戻し、戻す前の active を次の戻し先にする."""

        current, error = cls.load(pointer_file)
        if current is None:
            return None, error
        if current.previous_package_path is None:
            return None, f"戻せる直前の package がありません: {current.pointer_path}"
        verified, error = ImmutablePackage.verify(current.previous_package_path)
        if verified is None:
            return None, error
        return (
            _write_pointer(
                current.pointer_path,
                current.previous_package_path,
                current.active_package_path,
            ),
            None,
        )


@attrs.frozen
class _PointerRecord:
    """`ActivePointer` のうち、pointer file へ書き出す部分."""

    active_package_path: Path
    previous_package_path: Path | None
    active_package_id: str
    active_package_sha256: str


def _write_pointer(
    pointer_path: Path, package_path: Path, previous: Path | None
) -> ActivePointer:
    digest = sha256_file(package_path / CHECKSUM_FILENAME)
    record = _PointerRecord(
        active_package_path=package_path,
        previous_package_path=previous,
        active_package_id=f"{package_path.name}:{digest[:_PACKAGE_ID_DIGEST_LENGTH]}",
        active_package_sha256=digest,
    )
    ACTIVE_POINTER_DOCUMENT.save(pointer_path, record, converter=_CONVERTER)
    return _active_pointer(pointer_path, record)


def _active_pointer(pointer_path: Path, record: _PointerRecord) -> ActivePointer:
    return ActivePointer(
        pointer_path=pointer_path,
        active_package_path=record.active_package_path,
        previous_package_path=record.previous_package_path,
        active_package_id=record.active_package_id,
        active_package_sha256=record.active_package_sha256,
    )


def _validated_filenames(payload_filenames: Iterable[str]) -> tuple[str, ...]:
    filenames = tuple(payload_filenames)
    if not filenames:
        raise ValueError("payload_filenames が空です")
    if len(set(filenames)) != len(filenames):
        raise ValueError(f"payload_filenames が重複しています: {sorted(filenames)}")
    for filename in filenames:
        if not filename or Path(filename).name != filename:
            raise ValueError(
                f"payload はディレクトリを含まない名前が必要です: {filename!r}"
            )
        if filename == CHECKSUM_FILENAME:
            raise ValueError(f"{CHECKSUM_FILENAME} は payload にできません")
    return filenames


def _payload_checksums(directory: Path, expected: Set[str]) -> dict[str, str]:
    present = frozenset(entry.name for entry in directory.iterdir())
    if difference := _describe_difference(expected, present):
        raise ValueError(f"package の payload が宣言と一致しません{difference}")
    for filename in sorted(expected):
        payload = directory / filename
        if payload.is_symlink() or not payload.is_file():
            raise ValueError(
                f"package payload が通常ファイルではありません: {filename}"
            )
    return {
        filename: sha256_file(directory / filename) for filename in sorted(expected)
    }


def _describe_difference(expected: Set[str], present: Set[str]) -> str:
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if not missing and not unexpected:
        return ""
    return f"（不足: {missing}、余分: {unexpected}）"


def _format_checksums(checksums: Mapping[str, str]) -> str:
    return "".join(
        f"{checksums[filename]}  {filename}\n" for filename in sorted(checksums)
    )


def _read_checksums(path: Path) -> tuple[dict[str, str] | None, str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        return None, f"{CHECKSUM_FILENAME} を読めません: {path}（{error}）"
    checksums: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        digest, separator, filename = line.partition("  ")
        if (
            not separator
            or len(digest) != _DIGEST_LENGTH
            or not set(digest) <= _DIGEST_CHARACTERS
            or not filename
        ):
            return None, f"{CHECKSUM_FILENAME} の {number} 行目が不正です: {line!r}"
        checksums[filename] = digest
    if not checksums:
        return None, f"{CHECKSUM_FILENAME} が空です: {path}"
    return checksums, None


__all__ = [
    "ACTIVE_POINTER_DOCUMENT",
    "CHECKSUM_FILENAME",
    "ActivePointer",
    "ImmutablePackage",
]
