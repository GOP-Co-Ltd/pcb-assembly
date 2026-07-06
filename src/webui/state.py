"""WebUI のアプリケーション状態（選択マシン / PCB / 排他ロック）."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pcbasm.config import Machine
from pcbasm.hal import Camera, FrameHub, create_camera
from pcbasm.vision import CalibrationResult
from webui.config_store import ConfigStore, MachineSettingValue
from webui.fake_camera import FixedImageCamera
from webui.settings import Settings

_STATE_FILENAME = "webui_state.json"
type StoredJobParamValue = bool | float | int | str


def _is_stored_job_param_value(value: object) -> bool:
    return isinstance(value, bool | float | int | str)


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
        self._state_path = settings.webui_data_dir / _STATE_FILENAME
        self._legacy_state_path = settings.data_dir / _STATE_FILENAME
        # カメラ/FrameHub の遅延構築用（machine_lock とは別の内部ロック）
        self._camera_lock = threading.Lock()
        self._frame_hub: FrameHub | None = None

        persisted = self._load_persisted()
        self._selected_machine = self._resolve_machine(persisted.get("machine"))
        self._selected_pcb = self._resolve_pcb(persisted.get("pcb_file"))
        self._job_param_defaults = self._resolve_job_param_defaults(
            persisted.get("job_param_defaults")
        )

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
            self.rebuild_camera()

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

    def write_machine_settings(self, values: Mapping[str, MachineSettingValue]) -> None:
        """選択マシンの machine.toml へホワイトリスト項目を書き込む.

        装置排他ロックは取らない（ロック保持中のジョブワーカーからの
        即時反映用。ロックが必要な経路は呼び出し側で取る）。

        Raises:
            UnknownFieldError: 未知キーまたは型不一致の場合
        """
        self._store.write_machine_settings(self._selected_machine, values)

    def focus_z(self) -> float | None:
        """カメラキャリブレーションの Z 位置を返す（取得できなければ None）."""
        try:
            calibration_file = self.machine().camera.calibration_file
            return CalibrationResult.load(calibration_file).z_position
        except Exception:
            return None

    def job_param_defaults(self, job_name: str) -> dict[str, StoredJobParamValue]:
        """ジョブフォーム用に保存された既定値を返す（未保存なら空 dict）."""
        return dict(self._job_param_defaults.get(job_name, {}))

    def save_job_param_defaults(
        self, job_name: str, values: Mapping[str, StoredJobParamValue]
    ) -> None:
        """ジョブフォーム用の既定値を保存する."""
        self._job_param_defaults[job_name] = dict(values)
        self._persist()

    def frame_hub(self) -> FrameHub:
        """選択マシン用の FrameHub を返す（初回アクセスで遅延構築）.

        start はしない（PreviewService の責務）。

        Raises:
            OSError: カメラデバイスが見つからない・開けない場合
            RuntimeError: カメラがフォーマット等をサポートしない場合
        """
        with self._camera_lock:
            if self._frame_hub is None:
                self._frame_hub = FrameHub(self._build_camera())
            return self._frame_hub

    def rebuild_camera(self) -> None:
        """現行 FrameHub を停止し参照を破棄する（次回 frame_hub() で再構築）.

        未構築なら no-op（冪等）。
        """
        with self._camera_lock:
            hub = self._frame_hub
            self._frame_hub = None
        if hub is not None:
            hub.stop()

    def close(self) -> None:
        """シャットダウン後始末（FrameHub 停止 + 参照破棄）."""
        self.rebuild_camera()

    def _build_camera(self) -> Camera:
        if self._settings.fake_camera:
            return FixedImageCamera(self._settings.fake_camera_image, fps=15.0)
        camera = self.machine().camera
        return create_camera(
            device_id=camera.device_id,
            width=camera.width,
            height=camera.height,
            fps=camera.fps,
            format=camera.format,
            backend=camera.backend,
        )

    def acquire_machine(self, owner: str) -> None:
        """装置排他ロックを非ブロッキングで取得する.

        JobManager がジョブ開始時（request スレッド）に取得し、ワーカー
        終了時（別スレッド）に release_machine で解放する。

        Raises:
            BusyError: ロックが既に保持されている場合
        """
        if not self._lock.acquire(blocking=False):
            raise BusyError(self._busy_owner or "unknown")
        self._busy_owner = owner

    def release_machine(self) -> None:
        """取得済みの装置排他ロックを解放する.

        ``threading.Lock`` のため取得スレッドと別のスレッドからも解放できる。

        Raises:
            RuntimeError: ロックが取得されていない場合
        """
        self._busy_owner = None
        self._lock.release()

    @contextmanager
    def machine_lock(self, owner: str) -> Iterator[None]:
        """装置排他ロックを取得するコンテキストマネージャ.

        JobManager もこの同一ロックを acquire_machine / release_machine
        経由で共有する。

        Raises:
            BusyError: ロックが既に保持されている場合
        """
        self.acquire_machine(owner)
        try:
            yield
        finally:
            self.release_machine()

    def _load_persisted(self) -> dict[str, object]:
        path = (
            self._state_path if self._state_path.is_file() else self._legacy_state_path
        )
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "machine": self._selected_machine,
            "pcb_file": (self._selected_pcb.as_posix() if self._selected_pcb else None),
            "job_param_defaults": self._job_param_defaults,
        }
        self._state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _resolve_machine(self, persisted: object) -> str:
        machines = self._store.list_machines()
        if not machines:
            raise RuntimeError(
                f"configs に machine.toml を持つマシンがありません: "
                f"{self._settings.configs_root}"
            )
        if isinstance(persisted, str) and persisted in machines:
            return persisted
        if self._settings.default_machine in machines:
            return self._settings.default_machine
        return machines[0]

    def _resolve_pcb(self, persisted: object) -> Path | None:
        if not isinstance(persisted, str):
            return None
        return self._validate_pcb(Path(persisted))

    def _resolve_job_param_defaults(
        self, persisted: object
    ) -> dict[str, dict[str, StoredJobParamValue]]:
        if not isinstance(persisted, dict):
            return {}
        defaults: dict[str, dict[str, StoredJobParamValue]] = {}
        for job_name, values in persisted.items():
            if not isinstance(job_name, str) or not isinstance(values, dict):
                continue
            params = {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and _is_stored_job_param_value(value)
            }
            if params:
                defaults[job_name] = params
        return defaults

    def _validate_pcb(self, path: Path) -> Path | None:
        """PCB パスを検証し、正規化済み相対パスを返す（不正なら None）."""
        root = self._settings.pcb_browse_root.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.suffix != ".kicad_pcb" or not candidate.is_file():
            return None
        return candidate.relative_to(root)
