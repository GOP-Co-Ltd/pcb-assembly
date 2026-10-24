"""選択中の PCB の Board を、ファイルの (path, mtime, size) を鍵に持つキャッシュ.

同名の再アップロードや USB・scp での差し替えで、古い pad 座標を使わないためのもの。

preview とジョブの開始時に鍵を照合し、変わっていれば読み直す。
"""

from __future__ import annotations

import threading
from pathlib import Path

from pcbasm.board import Board
from pcbasm.kicad import load_board


class BoardCache:
    """直近に読んだ 1 枚の Board を持つ（読込は排他し、同時の要求で二重に読まない）."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[Path, int, int] | None = None
        self._board: Board | None = None

    def get(self, path: Path) -> Board:
        """``path`` の Board を返す。前回読んだときからファイルが変わっていれば読み直す.

        Raises:
            OSError: ファイルが無い・読めない場合
        """
        path = path.resolve()
        stat = path.stat()
        key = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._board is None or key != self._key:
                self._board = load_board(path)
                self._key = key
            return self._board

    def forget(self) -> None:
        """次の :meth:`get` で必ず読み直す（PCB を選び直したとき）."""
        with self._lock:
            self._key = None
            self._board = None
