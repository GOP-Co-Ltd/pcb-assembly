"""ジョブの起動・状態管理（直近 1 件のみ保持・非永続）."""

from __future__ import annotations

import logging
import shutil
import threading
import traceback
import uuid
from collections.abc import Mapping
from typing import Any, override

from pcbasm.machine.devices import Devices
from pcbasm.machine.journal import Journal
from web.api.camera import CameraFeed
from web.api.env import Env, resolve_machine_id
from web.api.events import EventBus
from web.api.jobs.runtime import hooks
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobAborted, JobChannel, JobContext
from web.api.jobs.runtime.record import (
    ApplyPayload,
    Artifact,
    JobRecord,
    JobStatus,
    ParamValue,
)
from web.api.lock import MachineLock
from web.api.machine_config import MachineConfigFile
from web.api.selection import Selection

# ジョブコンソールへ転送する pcbasm ロガー名
_PCBASM_LOGGER_NAME = "pcbasm"


class _PcbasmLogBridge(logging.Handler):
    """ワーカースレッドの pcbasm ログをジョブコンソールへ転送する Handler.

    preview スレッド等、他スレッドからの pcbasm ログは混ぜない。
    """

    def __init__(self, channel: JobChannel, thread_ident: int) -> None:
        super().__init__(level=logging.INFO)
        self._channel = channel
        self._thread_ident = thread_ident

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread_ident:
            return
        self._channel.log(record.getMessage())


class JobManager:
    """ジョブの起動・直近 1 件の状態保持を担うクラス."""

    def __init__(
        self,
        catalog: JobCatalog,
        *,
        env: Env,
        machine_config: MachineConfigFile,
        devices: Devices,
        lock: MachineLock,
        selection: Selection,
        camera: CameraFeed,
        events: EventBus,
        log_capacity: int = 500,
    ) -> None:
        """JobManager を初期化する.

        Args:
            catalog: ジョブ定義カタログ
            env: 起動時の環境（成果物とジャーナルの置き場所、machine ID）
            machine_config: ジョブ開始時に読む machine.toml（確定値の書き込みもここへ）
            devices: ジョブに貸す装置（通知音を含む）
            lock: ジョブが実行中ずっと持つ装置排他ロック
            selection: 選択中の PCB とフォーム既定値
            camera: ctx.frame() と ctx.open_camera() の委譲先
            events: ジョブのイベントの配信先
            log_capacity: ログのリングバッファ行数
        """
        self._catalog = catalog
        self._env = env
        self._machine_config = machine_config
        self._devices = devices
        self._machine_lock = lock
        self._selection = selection
        self._camera = camera
        self._events = events
        self._log_capacity = log_capacity
        self._artifacts_root = env.webui_data_dir

        self._lock = threading.Lock()
        self._record: JobRecord | None = None
        self._channel: JobChannel | None = None
        self._worker: threading.Thread | None = None

    # --- ライフサイクル ---

    def start(self, name: str, values: Mapping[str, object]) -> JobRecord:
        """ジョブを開始する（worker スレッド起動後すぐ返る）.

        前ジョブの成果物ディレクトリは削除される（直近 1 件のみ保持）。

        Raises:
            KeyError: 未知のジョブ名（→ 404）
            ValueError: パラメータ不正・PCB 未選択（→ 400）
            BusyError: 装置排他ロックが取得できない（→ 409）
        """
        definition = self._catalog.get(name)
        params = self._catalog.validate_params(definition, values)
        pcb_path = self._selection.pcb_path
        if definition.requires_pcb and pcb_path is None:
            raise ValueError(f"ジョブ {name} には PCB ファイルの選択が必要です")
        machine = self._machine_config.machine()

        self._machine_lock.acquire(f"job:{name}")
        try:
            persisted_params = {
                key: params[key] for key in definition.persisted_params if key in params
            }
            if persisted_params:
                self._selection.merge_job_param_defaults(name, persisted_params)
            record = JobRecord(uuid.uuid4().hex, name, params, self._log_capacity)
            channel = JobChannel(
                record,
                self._camera,
                self._events.publish,
                self._apply_machine_settings,
                hooks.operator_notifier(self._devices, machine.audio),
            )
            artifacts_dir = self._artifacts_root / record.id
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            selected_pcb = self._selection.pcb
            context = JobContext(
                channel,
                pcb_path=pcb_path,
                machine=machine,
                devices=self._devices,
                artifacts_dir=artifacts_dir,
                journal=Journal.for_job(self._env.journal_dir, record.id),
                machine_id=resolve_machine_id(self._env),
                source_pcb=selected_pcb.as_posix() if selected_pcb else None,
                boards=self._selection.boards,
            )
            worker = threading.Thread(
                target=self._run_worker,
                args=(definition, channel, context),
                name=f"job-{name}",
                daemon=True,
            )
            with self._lock:
                if self._record is not None:
                    shutil.rmtree(
                        self._artifacts_root / self._record.id, ignore_errors=True
                    )
                self._record = record
                self._channel = channel
                self._worker = worker
            channel.publish_status()
            worker.start()
            return record
        except BaseException:
            self._machine_lock.release()
            raise

    def current(self) -> JobRecord | None:
        """直近 1 件のジョブ record（実行中含む）。非永続."""
        with self._lock:
            return self._record

    def request_abort(self) -> bool:
        """協調的中止を要求する.

        Returns:
            アクティブ（非終端）ジョブに abort を立てたら True
        """
        with self._lock:
            record = self._record
            channel = self._channel
        if record is None or channel is None or record.status.terminal:
            return False
        channel.abort()
        return True

    def shutdown(self, timeout: float = 10.0) -> None:
        """request_abort + worker join（lifespan shutdown 用、冪等）."""
        self.request_abort()
        with self._lock:
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    # --- 対話 ---

    def respond_prompt(self, prompt_id: str, answer: object) -> None:
        """応答待ちプロンプトに答える.

        Raises:
            ValueError: pending prompt 無し / id 不一致 / 型不一致の場合
        """
        with self._lock:
            channel = self._channel
        if channel is None:
            raise ValueError("応答待ちのプロンプトがありません")
        channel.respond(prompt_id, answer)

    def update_current_params(
        self,
        values: Mapping[str, Any],
        *,
        persist: bool = False,
        expected_job_id: str | None = None,
    ) -> dict[str, ParamValue]:
        """実行中ジョブの runtime_editable パラメータを即時更新する.

        コマンドキューを経由せず、prompt / sleep 待機中でも適用する。

        Args:
            values: 更新するパラメータ（runtime_editable な subset のみ）
            persist: True で persisted_params 分を次回フォーム既定値へ保存する
            expected_job_id: 指定時はこのジョブだけを更新する

        Returns:
            検証・coerce 済みの適用値

        Raises:
            LookupError: 対象ジョブ不一致（→ 409）
            ValueError: 実行中ジョブ無し / 終端 / 検証エラー（→ 400）
        """
        with self._lock:
            record = self._record
            channel = self._channel
        if expected_job_id is not None and (
            record is None or record.id != expected_job_id
        ):
            raise LookupError(
                "ジョブが切り替わりました。現在のジョブを確認してください"
            )
        if record is None or channel is None or record.status.terminal:
            raise ValueError("実行中のジョブがありません")
        definition = self._catalog.get(record.name)
        validated = self._catalog.validate_runtime_params(definition, values)
        channel.update_params(validated)
        if persist:
            persisted = {
                key: validated[key]
                for key in definition.persisted_params
                if key in validated
            }
            if persisted:
                self._selection.merge_job_param_defaults(record.name, persisted)
        channel.publish_status()
        return validated

    def submit_command(
        self, command: Mapping[str, Any], *, expected_job_id: str | None = None
    ) -> None:
        """実行中ジョブの command キューへ 1 件投入する.

        Raises:
            LookupError: 対象ジョブ不一致
            ValueError: accepts_commands なジョブが実行中でない /
                "type" キーが無い場合
        """
        if "type" not in command:
            raise ValueError('command には "type" キーが必要です')
        with self._lock:
            record = self._record
            channel = self._channel
        if expected_job_id is not None and (
            record is None or record.id != expected_job_id
        ):
            raise LookupError(
                "ジョブが切り替わりました。現在のジョブを確認してください"
            )
        if record is None or channel is None or record.status.terminal:
            raise ValueError("コマンドを受け付けるジョブが実行中ではありません")
        if not self._catalog.get(record.name).accepts_commands:
            raise ValueError(f"ジョブ {record.name} はコマンドを受け付けません")
        channel.submit(command)

    # --- Apply / Discard ---

    def apply_payload(self, *, expected_job_id: str | None = None) -> ApplyPayload:
        """直近 SUCCEEDED ジョブの設定反映ペイロードを返す.

        Raises:
            LookupError: 対象ジョブ不一致 / 直近ジョブ無し / SUCCEEDED でない / payload 無し /
                適用・破棄済みの場合（→ 409）
        """
        with self._lock:
            record = self._record
        if expected_job_id is not None and (
            record is None or record.id != expected_job_id
        ):
            raise LookupError("ジョブが切り替わりました。現在の結果を確認してください")
        if record is None or not record.apply_available:
            raise LookupError("設定に反映可能な計測結果がありません")
        result = record.result
        assert result is not None and result.apply is not None
        return result.apply

    def mark_applied(self, *, expected_job_id: str | None = None) -> None:
        """書込成功後にルーターが呼ぶ（以後 apply_payload は LookupError）."""
        with self._lock:
            record = self._record
            if expected_job_id is not None and (
                record is None or record.id != expected_job_id
            ):
                raise LookupError(
                    "ジョブが切り替わりました。現在の結果を確認してください"
                )
            if record is None:
                return
            record.consume_apply()
        self._events.publish({"type": "job_status", "job_id": record.id})

    def discard(self, *, expected_job_id: str | None = None) -> None:
        """設定反映ペイロードを無効化する（冪等）."""
        self.mark_applied(expected_job_id=expected_job_id)

    def _apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """実行中ジョブの確定値を machine.toml へ即時書き込む（変更の通知は書き込みが行う）.

        呼び出し元のジョブワーカーが装置排他ロックを保持しているため、
        Apply エンドポイントと同様の直列化が成立している。

        Raises:
            InvalidSettingError: 未知のキー・不正な値の場合
        """
        self._machine_config.write(values)

    # --- 内部 ---

    def _run_worker(
        self, definition: JobDefinition, channel: JobChannel, context: JobContext
    ) -> None:
        record = channel.record
        logger = logging.getLogger(_PCBASM_LOGGER_NAME)
        bridge = _PcbasmLogBridge(channel, threading.get_ident())
        # effective level で INFO が出力されない場合のみ INFO へ下げる（finally で復元）
        previous_level = logger.level
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(bridge)
        try:
            record.set_status(JobStatus.RUNNING)
            channel.publish_status()
            try:
                result = definition.run(context)
                record.finish(JobStatus.SUCCEEDED, result=result)
            except JobAborted:
                record.finish(JobStatus.ABORTED)
            except Exception as exc:
                for line in traceback.format_exc().splitlines():
                    channel.log(line)
                record.finish(JobStatus.FAILED, error=str(exc) or type(exc).__name__)
            if definition.uses_machine:
                hooks.park_after_job(context)
                hooks.play_completion_sound(
                    self._devices, context.machine.audio, record.status
                )
            self._link_journal(record, context.journal)
            # 終端ステータス確定 → 経過秒の停止 → job_status 発行 → ロック解放の順を守る
            record.stop_clock()
            channel.publish_status()
        finally:
            logger.removeHandler(bridge)
            logger.setLevel(previous_level)
            self._machine_lock.release()

    def _link_journal(self, record: JobRecord, journal: Journal) -> None:
        """送信があったジョブの成果物に G-code ジャーナルへのリンクを足す."""
        if journal.path.exists():
            path = journal.path.relative_to(self._artifacts_root).as_posix()
            record.add_artifact(Artifact("G-code ジャーナル", path, "file"))
