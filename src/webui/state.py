"""WebUI のアプリケーション状態（選択マシン / PCB / 排他ロック）."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pcbasm.config import Machine
from pcbasm.vision import CalibrationResult
from webui.config_store import ConfigStore
from webui.settings import Settings

_STATE_FILENAME = "webui_state.json"


class BusyError(RuntimeError):
    """マシン排他ロックが取得できない（→ HTTP 409）."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"装置は使用中です（owner: {owner}）")
        self._owner = owner

    @property
    def owner(self) -> str:
        return self._owner


class AppState:
    """選択マシン / PCB の保持・永続化と装置排他ロックを担うクラス."""

    def __init__(self, settings: Settings, store: ConfigStore) -> None:
        """AppState を初期化する.

        data_dir/webui_state.json から選択状態を復元する。不正値は
        default_machine（マシン一覧に無ければソート先頭）へフォールバックする。

        Args:
            settings: WebUI 設定
            store: 設定ファイルストア

        Raises:
            RuntimeError: 選択可能なマシンが 1 つも無い場合
        """
        self._settings = settings
        self._store = store
        self._lock = threading.Lock()
        self._busy_owner: str | None = None
        self._state_path = settings.data_dir / _STATE_FILENAME

        persisted = self._load_persisted()
        self._selected_machine = self._resolve_machine(persisted.get("machine"))
        self._selected_pcb = self._resolve_pcb(persisted.get("pcb_file"))

    @property
    def selected_machine(self) -> str:
        return self._selected_machine

    @property
    def selected_pcb(self) -> Path | None:
        """選択中の PCB ファイル（pcb_browse_root からの相対パス）."""
        return self._selected_pcb

    @property
    def busy_owner(self) -> str | None:
        """排他ロック保持中の owner 名（未保持なら None）."""
        if self._lock.locked():
            return self._busy_owner or "unknown"
        return None

    def select_machine(self, name: str) -> None:
        """マシンを選択し永続化する.

        Raises:
            ValueError: 未知のマシン名の場合
            BusyError: 排他ロックが取得できない場合
        """
        if name not in self._store.list_machines():
            raise ValueError(f"未知のマシンです: {name}")
        with self.machine_lock("select-machine"):
            self._selected_machine = name
            self._persist()

    def select_pcb(self, path: Path) -> None:
        """PCB ファイルを選択し永続化する.

        Args:
            path: pcb_browse_root からの相対パス

        Raises:
            ValueError: root 範囲外・拡張子不正・不存在の場合
            BusyError: 排他ロックが取得できない場合
        """
        relative = self._validate_pcb(path)
        if relative is None:
            raise ValueError(f"PCB ファイルとして選択できません: {path}")
        with self.machine_lock("select-pcb"):
            self._selected_pcb = relative
            self._persist()

    def machine(self) -> Machine:
        """選択マシンの Machine 設定を読み込んで返す（毎回ロード）."""
        return Machine(self._store.machine_toml_path(self._selected_machine))

    def focus_z(self) -> float | None:
        """カメラキャリブレーションの Z 位置を返す（取得できなければ None）."""
        try:
            calibration_file = self.machine().camera.calibration_file
            return CalibrationResult.load(calibration_file).z_position
        except Exception:
            return None

    @contextmanager
    def machine_lock(self, owner: str) -> Iterator[None]:
        """装置排他ロックを非ブロッキングで取得する.

        Phase 3 の JobManager もこの同一ロックを共有する。

        Raises:
            BusyError: ロックが既に保持されている場合
        """
        if not self._lock.acquire(blocking=False):
            raise BusyError(self._busy_owner or "unknown")
        self._busy_owner = owner
        try:
            yield
        finally:
            self._busy_owner = None
            self._lock.release()

    def _load_persisted(self) -> dict[str, str | None]:
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(value, str) or value is None
        }

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "machine": self._selected_machine,
            "pcb_file": (self._selected_pcb.as_posix() if self._selected_pcb else None),
        }
        self._state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _resolve_machine(self, persisted: str | None) -> str:
        machines = self._store.list_machines()
        if not machines:
            raise RuntimeError(
                f"configs に machine.toml を持つマシンがありません: "
                f"{self._settings.configs_root}"
            )
        if persisted in machines:
            return persisted
        if self._settings.default_machine in machines:
            return self._settings.default_machine
        return machines[0]

    def _resolve_pcb(self, persisted: str | None) -> Path | None:
        if persisted is None:
            return None
        return self._validate_pcb(Path(persisted))

    def _validate_pcb(self, path: Path) -> Path | None:
        """PCB パスを検証し、正規化済み相対パスを返す（不正なら None）."""
        root = self._settings.pcb_browse_root.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.suffix != ".kicad_pcb" or not candidate.is_file():
            return None
        return candidate.relative_to(root)
