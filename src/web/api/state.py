"""WebUI のアプリケーション状態（選択 PCB / 排他ロック）."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pcbasm.atomic import write_bytes_atomic, write_text_atomic
from pcbasm.config import ConfigError, MachineConfig, load_machine
from pcbasm.machine.devices import Devices, open_devices
from pcbasm.migrate.machine_toml import NAME, MigrationError, migrate_machine_toml
from web.api.machine_config import MachineConfigFile
from web.api.selection import BoardCache
from web.api.settings import Settings

logger = logging.getLogger(__name__)

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
    """選択 PCB の保持・永続化（とその Board のキャッシュ）、装置、装置排他ロックを担うクラス.

    ロック順序は ``machine_lock`` → ``_persist_lock`` に固定する
    （``_persist_lock`` 保持中に装置排他ロックを取る経路を作らない）。
    """

    def __init__(
        self,
        settings: Settings,
        machine_config: MachineConfigFile,
        devices: Devices | None = None,
    ) -> None:
        """AppState を初期化する.

        webui_data_dir/webui_state.json から選択状態を復元する。
        選択していた PCB が無くなっていれば選択を解除し、:attr:`pcb_notice` で知らせる。
        解除は保存しないので、後からマウントされるファイルなら次の起動で選択が戻る。

        Args:
            settings: WebUI 設定
            machine_config: machine.toml の読み書き
            devices: 装置（None なら settings どおりに組む。テストは fake を注入する）
        """
        self._settings = settings
        self._machine_config = machine_config
        if devices is None:
            devices = open_devices(
                machine_config.path,
                settings.journal_dir,
                fake=False,
                camera_image=(
                    settings.fake_camera_image if settings.fake_camera else None
                ),
            )
        self._devices = devices
        self._lock = threading.Lock()
        self._busy_owner: str | None = None
        self._state_path = settings.webui_data_dir / _STATE_FILENAME
        # webui_state.json の read-modify-write 直列化用（machine_lock の内側）
        self._persist_lock = threading.Lock()
        self._config_notice: str | None = None
        self._migration_error: str | None = None
        self._boards = BoardCache()
        self._pcb_notice: str | None = None

        persisted = self._load_persisted()
        self._job_param_defaults = self._resolve_job_param_defaults(
            persisted.get("job_param_defaults")
        )
        pcb_file = persisted.get("pcb_file")
        self._selected_pcb = self._resolve_pcb(pcb_file)
        if isinstance(pcb_file, str) and self._selected_pcb is None:
            self._pcb_notice = (
                f"選択していた PCB {pcb_file} が見つからないため、選択を解除しました"
            )

    @property
    def selected_pcb(self) -> Path | None:
        """選択中の PCB ファイル（pcb_browse_root からの相対パス）."""
        return self._selected_pcb

    @property
    def pcb_notice(self) -> str | None:
        """起動時に選択を解除したときの知らせ（PCB を選び直すまで出す）."""
        return self._pcb_notice

    @property
    def boards(self) -> BoardCache:
        """選択中の PCB の Board のキャッシュ（選び直すと次の取得で必ず読み直す）."""
        return self._boards

    @property
    def busy_owner(self) -> str | None:
        """排他ロック保持中の owner 名（未保持なら None）."""
        if self._lock.locked():
            return self._busy_owner or "unknown"
        return None

    def select_pcb(self, path: Path, *, content: bytes | None = None) -> None:
        """PCB ファイルを選択し永続化する。アップロード保存も同じ排他区間で行う.

        Args:
            path: pcb_browse_root からの相対パス
            content: アップロード内容。指定時は選択前にファイルを atomic に保存する

        Raises:
            ValueError: root 範囲外・拡張子不正・内容未指定でファイルが存在しない場合
            BusyError: 排他ロックが取得できない場合
        """
        relative = self._validate_pcb(path, require_file=content is None)
        if relative is None:
            raise ValueError(f"PCB ファイルとして選択できません: {path}")
        with self.machine_lock("select-pcb"):
            if content is not None:
                destination = self._settings.pcb_browse_root.resolve() / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomic(destination, content)
            with self._persist_lock:
                self._persist(relative, self._job_param_defaults)
                self._selected_pcb = relative
            self._pcb_notice = None
            self._boards.forget()

    @property
    def devices(self) -> Devices:
        """装置（Klipper とカメラ）."""
        return self._devices

    @property
    def machine_config(self) -> MachineConfigFile:
        """machine.toml の読み書き（書き込みは装置排他ロックの内側で行う）."""
        return self._machine_config

    def machine(self) -> MachineConfig:
        """machine.toml をディスクから読み、全体を検証して返す（毎回ロード）.

        手編集と書き込みが再起動なしで次の読み込みに効く。

        Raises:
            ConfigError: machine.toml が読めない・検証に通らない場合（degraded）
        """
        return load_machine(self._machine_config.path)

    def machine_or_error(self) -> tuple[MachineConfig, None] | tuple[None, str]:
        """:meth:`machine` の degraded 版。設定エラーなら ``(None, 理由)`` を返す.

        起動時の移行に失敗していれば、読めない理由として移行の失敗を返す。
        """
        try:
            return self.machine(), None
        except ConfigError as exc:
            if self._migration_error is not None:
                return None, (
                    "machine.toml を移行できませんでした（ファイルは変えていません）: "
                    f"{self._migration_error}"
                )
            return None, str(exc)

    @property
    def config_notice(self) -> str | None:
        """起動時に machine.toml を移行したときの知らせ（移行しなければ None）."""
        return self._config_notice

    def migrate_config(self) -> None:
        """machine.toml を移行する（起動時、設定を読む前に 1 回だけ呼ぶ）.

        失敗しても例外は出さない。ファイルに触れず、読めない間はその理由を設定エラーとして出す（degraded）。
        """
        try:
            changes = migrate_machine_toml(self._machine_config.path)
        except MigrationError as exc:
            logger.error("machine.toml を移行できません: %s", exc)
            self._migration_error = str(exc)
            return
        if changes:
            logger.info(
                "machine.toml を移行 %s で書き換えました: %s", NAME, " / ".join(changes)
            )
            self._config_notice = (
                "machine.toml を新しい形へ移行しました"
                f"（元の内容は設定履歴の machine-pre-{NAME}.toml）"
            )

    def job_param_defaults(self, job_name: str) -> dict[str, StoredJobParamValue]:
        """ジョブフォーム用に保存された既定値を返す（未保存なら空 dict）."""
        with self._persist_lock:
            return dict(self._job_param_defaults.get(job_name, {}))

    def merge_job_param_defaults(
        self, job_name: str, values: Mapping[str, StoredJobParamValue]
    ) -> dict[str, StoredJobParamValue]:
        """ジョブフォーム既定値へ ``values`` をマージして保存し、結果を返す.

        ロック内で「現在値を読む → マージ → 永続化 → 公開」を行うため、同時保存でも
        先行の値が失われない。保存に失敗した変更は公開しない。
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
            self._persist(self._selected_pcb, updated)
            self._job_param_defaults = updated
            return dict(merged)

    def close(self) -> None:
        """シャットダウン後始末（装置の FrameHub を止める）."""
        self._devices.close()

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
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _persist(
        self,
        selected_pcb: Path | None,
        job_param_defaults: dict[str, dict[str, StoredJobParamValue]],
    ) -> None:
        """次の状態を保存する（呼び出し側が ``_persist_lock`` を保持し、成功後に公開）."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pcb_file": selected_pcb.as_posix() if selected_pcb else None,
            "job_param_defaults": job_param_defaults,
        }
        write_text_atomic(
            self._state_path, json.dumps(data, ensure_ascii=False, indent=2)
        )

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

    def _validate_pcb(self, path: Path, *, require_file: bool = True) -> Path | None:
        """PCB パスを検証し、正規化済み相対パスを返す（不正なら None）."""
        root = self._settings.pcb_browse_root.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.suffix != ".kicad_pcb":
            return None
        if require_file and not candidate.is_file():
            return None
        return candidate.relative_to(root)
