"""G-code ジャーナル.

実機へ送ったブロックを、実効 timeout の見出しを付けてファイルへ追記する。
ジョブの成果物と違って次のジョブで消えないので、版の前後で送信列を diff できる。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from pcbasm.gcode import GCode

logger = logging.getLogger(__name__)

# 保持するファイル数。超えたら最後に書いた時刻の古いものから消す
LIMIT = 200


def format_block(gcode: GCode, timeout: float | None) -> str:
    """送信 1 ブロックを、実効 timeout の見出し行と G-code の行にする."""
    return f"# send timeout={timeout}\n{gcode}\n"


class Journal:
    """1 つのファイルへ送信ブロックを追記する G-code ジャーナル.

    ファイルは最初の書き込みで作り、そのとき同じディレクトリの古いファイルを消して上限に収める。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_job(cls, root: Path, job_id: str) -> Journal:
        """ジョブ 1 件の送信を書くジャーナル（``<日付>-<job_id>.gcode``）."""
        return cls(root / f"{date.today().isoformat()}-{job_id}.gcode")

    @classmethod
    def for_today(cls, root: Path) -> Journal:
        """ジョブの外の操作（REST）を書く当日のジャーナル（``<日付>-rest.gcode``）."""
        return cls(root / f"{date.today().isoformat()}-rest.gcode")

    @property
    def path(self) -> Path:
        return self._path

    def write(self, gcode: GCode, timeout: float | None) -> None:
        """送信ブロックを 1 つ追記する（書けなければ log に残し、送信は止めない）."""
        try:
            if not self._path.exists():
                self._path.parent.mkdir(parents=True, exist_ok=True)
                _prune(self._path.parent, LIMIT - 1)
            with self._path.open("a", encoding="utf-8") as file:
                file.write(format_block(gcode, timeout))
        except OSError as exc:
            logger.warning("G-code ジャーナルに書けません: %s", exc)


def _prune(root: Path, keep: int) -> None:
    files = sorted(root.glob("*.gcode"), key=lambda path: path.stat().st_mtime)
    for path in files[: max(len(files) - keep, 0)]:
        path.unlink()
