"""machine.toml の世代スナップショット.

書き込みの前に、その時点の machine.toml を ``config/history/machine-<時刻>.toml`` へ写す。
世代は新しいものから ``MAX_SNAPSHOTS`` 個だけ残す。
移行の直前に取る名前付きの世代（``machine-pre-<移行名>.toml``）は手動ロールバックの戻り先なので、ローテーションで消さない。
"""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

from pcbasm.atomic import write_bytes_atomic
from web.api.models import ConfigHistoryEntry

MAX_SNAPSHOTS = 50

_NAMED_PREFIX = "machine-pre-"
_STAMP = "%Y%m%d-%H%M%S-%f"


class UnknownSnapshotError(LookupError):
    """指定の世代が無い（→ HTTP 404）."""


class ConfigHistory:
    """machine.toml のスナップショットの作成・一覧・復元."""

    def __init__(self, machine_toml: Path) -> None:
        """ConfigHistory を初期化する.

        Args:
            machine_toml: 世代を残す machine.toml（世代は同じディレクトリの history/ に置く）
        """
        self._target = machine_toml
        self._dir = machine_toml.parent / "history"

    def snapshot(self) -> None:
        """現在の machine.toml を新しい世代として残し、上限を超えた古い世代を消す."""
        self._dir.mkdir(exist_ok=True)
        name = f"machine-{datetime.now():{_STAMP}}.toml"
        write_bytes_atomic(self._dir / name, self._target.read_bytes())
        rotated = sorted(
            path.name
            for path in self._snapshots()
            if not path.name.startswith(_NAMED_PREFIX)
        )
        for stale in rotated[:-MAX_SNAPSHOTS]:
            (self._dir / stale).unlink()

    def entries(self) -> list[ConfigHistoryEntry]:
        """世代を新しい順に、次に入った変更の要約を付けて返す."""
        snapshots = sorted(
            ((_taken_at(path), path.name, path) for path in self._snapshots()),
            reverse=True,
        )
        after = self._target.read_bytes()
        entries = []
        for taken_at, _, path in snapshots:
            before = path.read_bytes()
            entries.append(
                ConfigHistoryEntry(
                    name=path.name,
                    time_label=f"{taken_at:%Y-%m-%d %H:%M:%S}",
                    changes=_changes(before, after),
                )
            )
            after = before
        return entries

    def restore(self, name: str) -> None:
        """指定の世代を machine.toml へ書き戻す（書き戻す前の状態も世代に残す）.

        Raises:
            UnknownSnapshotError: 指定の世代が無い場合
        """
        if name not in {path.name for path in self._snapshots()}:
            raise UnknownSnapshotError(f"設定履歴にありません: {name}")
        content = (self._dir / name).read_bytes()
        self.snapshot()
        write_bytes_atomic(self._target, content)

    def _snapshots(self) -> list[Path]:
        return list(self._dir.glob("machine-*.toml"))


def _taken_at(path: Path) -> datetime:
    """世代を取った時刻（名前に時刻が無い名前付きの世代だけ mtime を使う）.

    cp や unzip で置き直すと mtime はそろうので、時刻入りの世代は名前の時刻で並べる。
    """
    if path.name.startswith(_NAMED_PREFIX):
        return datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.strptime(path.stem.removeprefix("machine-"), _STAMP)


def _changes(before: bytes, after: bytes) -> list[str]:
    """2 つの machine.toml の差分を「キー: 前 → 後」の行にする."""
    old, new = _flatten_toml(before), _flatten_toml(after)
    if old is None or new is None:
        return ["TOML として読めないため差分を出せません"]
    return [
        f"{key}: {_show(old.get(key))} → {_show(new.get(key))}"
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]


def _flatten_toml(content: bytes) -> dict[str, object] | None:
    """TOML をドット区切りのキーと値の辞書にする（読めなければ None）."""
    try:
        doc = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    flat: dict[str, object] = {}

    def walk(prefix: str, table: dict[str, object]) -> None:
        for key, value in table.items():
            if isinstance(value, dict):
                walk(f"{prefix}{key}.", value)
            else:
                flat[f"{prefix}{key}"] = value

    walk("", doc)
    return flat


def _show(value: object) -> str:
    if value is None:
        return "（なし）"
    return json.dumps(value, ensure_ascii=False, default=str)
