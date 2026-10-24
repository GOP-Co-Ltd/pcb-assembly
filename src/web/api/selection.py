"""選択中の PCB とジョブフォームの既定値（webui_state.json に保存する）.

選択中の PCB の Board は、ファイルの (path, mtime, size) を鍵にキャッシュする。

同名の再アップロードや USB・scp での差し替えで、古い pad 座標を使わないためのもの。

preview とジョブの開始時に鍵を照合し、変わっていれば読み直す。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from pcbasm.atomic import write_bytes_atomic, write_text_atomic
from pcbasm.board import Board
from pcbasm.kicad import load_board
from web.api.env import Env
from web.api.lock import MachineLock

_STATE_FILENAME = "webui_state.json"
type StoredJobParamValue = bool | float | int | str


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


class Selection:
    """選択中の PCB（とその Board のキャッシュ）と、ジョブフォームの既定値を保持し、webui_state.json に保存する.

    ロック順序は装置排他ロック → ``_persist_lock`` に固定する（``_persist_lock`` 保持中に装置排他ロックを取る経路を作らない）。
    """

    def __init__(
        self,
        env: Env,
        lock: MachineLock,
        on_select: Callable[[], None] = lambda: None,
    ) -> None:
        """Selection を初期化する.

        webui_data_dir/webui_state.json から選択状態を復元する。
        選択していた PCB が無くなっていれば選択を解除し、:attr:`notice` で知らせる。
        解除は保存しないので、後からマウントされるファイルなら次の起動で選択が戻る。

        Args:
            env: 起動時の環境（pcb_browse_root と保存先）
            lock: 選び直す間に取る装置排他ロック
            on_select: 選び直したときにロックの内側で呼ぶ
        """
        self._root = env.pcb_browse_root
        self._lock = lock
        self._on_select = on_select
        self._state_path = env.webui_data_dir / _STATE_FILENAME
        # webui_state.json の read-modify-write 直列化用（装置排他ロックの内側）
        self._persist_lock = threading.Lock()
        self._boards = BoardCache()
        self._notice: str | None = None

        persisted = self._load_persisted()
        self._job_param_defaults = _job_param_defaults(
            persisted.get("job_param_defaults")
        )
        pcb_file = persisted.get("pcb_file")
        self._pcb = (
            self._validate(Path(pcb_file)) if isinstance(pcb_file, str) else None
        )
        if isinstance(pcb_file, str) and self._pcb is None:
            self._notice = (
                f"選択していた PCB {pcb_file} が見つからないため、選択を解除しました"
            )

    @property
    def pcb(self) -> Path | None:
        """選択中の PCB ファイル（pcb_browse_root からの相対パス）."""
        return self._pcb

    @property
    def pcb_path(self) -> Path | None:
        """選択中の PCB ファイルの絶対パス."""
        if self._pcb is None:
            return None
        return (self._root / self._pcb).resolve()

    @property
    def notice(self) -> str | None:
        """起動時に選択を解除したときの知らせ（PCB を選び直すまで出す）."""
        return self._notice

    @property
    def boards(self) -> BoardCache:
        """選択中の PCB の Board のキャッシュ（選び直すと次の取得で必ず読み直す）."""
        return self._boards

    def selected_board(self) -> tuple[Path, Board] | None:
        """選択中の PCB（pcb_browse_root からの相対パス）とその Board（未選択なら None）.

        1 回読んだ選択から両方を引くので、間に選び直されても別の基板の組み合わせにならない。

        Raises:
            OSError: ファイルが無い・読めない場合
        """
        pcb = self._pcb
        if pcb is None:
            return None
        return pcb, self._boards.get(self._root / pcb)

    def select(self, path: Path, *, content: bytes | None = None) -> None:
        """PCB ファイルを選択し保存する。アップロード保存も同じ排他区間で行う.

        Args:
            path: pcb_browse_root からの相対パス
            content: アップロード内容。指定時は選択前にファイルを atomic に保存する

        Raises:
            ValueError: root 範囲外・拡張子不正・内容未指定でファイルが存在しない場合
            BusyError: 装置排他ロックが取得できない場合
        """
        relative = self._validate(path, require_file=content is None)
        if relative is None:
            raise ValueError(f"PCB ファイルとして選択できません: {path}")
        with self._lock.hold("select-pcb"):
            if content is not None:
                destination = self._root.resolve() / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomic(destination, content)
            with self._persist_lock:
                self._persist(relative, self._job_param_defaults)
                self._pcb = relative
            self._notice = None
            self._boards.forget()
            self._on_select()

    def job_param_defaults(self, job_name: str) -> dict[str, StoredJobParamValue]:
        """ジョブフォーム用に保存された既定値を返す（未保存なら空 dict）."""
        with self._persist_lock:
            return dict(self._job_param_defaults.get(job_name, {}))

    def merge_job_param_defaults(
        self, job_name: str, values: Mapping[str, StoredJobParamValue]
    ) -> dict[str, StoredJobParamValue]:
        """ジョブフォーム既定値へ ``values`` をマージして保存し、結果を返す.

        ロック内で「現在値を読む → マージ → 保存 → 公開」を行うため、同時保存でも先行の値が失われない。
        保存に失敗した変更は公開しない。
        呼び出し側で読んでからマージし直す必要はない。

        Args:
            job_name: ジョブ名
            values: 上書きするキーと値（既存キーは置換、他は保持）

        Returns:
            マージ後の既定値
        """
        with self._persist_lock:
            merged = {**self._job_param_defaults.get(job_name, {}), **values}
            updated = {**self._job_param_defaults, job_name: merged}
            self._persist(self._pcb, updated)
            self._job_param_defaults = updated
            return dict(merged)

    def _load_persisted(self) -> dict[str, object]:
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _persist(
        self,
        pcb: Path | None,
        job_param_defaults: dict[str, dict[str, StoredJobParamValue]],
    ) -> None:
        """次の状態を保存する（呼び出し側が ``_persist_lock`` を保持し、成功後に公開）."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pcb_file": pcb.as_posix() if pcb else None,
            "job_param_defaults": job_param_defaults,
        }
        write_text_atomic(
            self._state_path, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _validate(self, path: Path, *, require_file: bool = True) -> Path | None:
        """PCB パスを検証し、正規化済み相対パスを返す（不正なら None）."""
        root = self._root.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.suffix != ".kicad_pcb":
            return None
        if require_file and not candidate.is_file():
            return None
        return candidate.relative_to(root)


def _job_param_defaults(
    persisted: object,
) -> dict[str, dict[str, StoredJobParamValue]]:
    """保存されていたジョブフォーム既定値のうち、型の合うものだけを返す."""
    if not isinstance(persisted, dict):
        return {}
    defaults: dict[str, dict[str, StoredJobParamValue]] = {}
    for job_name, values in persisted.items():
        if not isinstance(job_name, str) or not isinstance(values, dict):
            continue
        params = {
            key: value
            for key, value in values.items()
            if isinstance(key, str) and isinstance(value, bool | float | int | str)
        }
        if params:
            defaults[job_name] = params
    return defaults
