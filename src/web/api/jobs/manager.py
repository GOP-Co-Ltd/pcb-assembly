"""ジョブの起動・状態管理・イベント配信（直近 1 件のみ保持・非永続）."""

from __future__ import annotations

import asyncio
import enum
import logging
import queue
import shutil
import threading
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, override

from pcbasm.config import Audio, Machine
from pcbasm.hal import AudioPlayer, FrameHub, Klipper
from pcbasm.hal.audio import Sound
from pcbasm.hal.klipper import PRESENT_TIMEOUT
from pcbasm.parking import park_or_present
from pcbasm.vision import Image
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import (
    Answer,
    ApplyPayload,
    JobAborted,
    JobContext,
    JobResult,
    ParamValue,
    PromptSpec,
)
from web.api.preview import PreviewService
from web.api.settings import Settings, resolve_machine_id
from web.api.state import AppState

type _Event = dict[str, Any]

# next_command 待機を強制的に JobAborted 化するための番兵
_ABORT_SENTINEL: Any = object()

# ジョブコンソールへ転送する pcbasm ロガー名
_PCBASM_LOGGER_NAME = "pcbasm"

_logger = logging.getLogger(__name__)

# prompt(while_waiting=...) のポーリング間隔 [sec]
_PROMPT_POLL_SEC = 0.05


class _PcbasmLogBridge(logging.Handler):
    """ワーカースレッドの pcbasm ログをジョブコンソールへ転送する Handler.

    preview スレッド等、他スレッドからの pcbasm ログは混ぜない。
    """

    def __init__(self, runtime: _JobRuntime, thread_ident: int) -> None:
        super().__init__(level=logging.INFO)
        self._runtime = runtime
        self._thread_ident = thread_ident

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread_ident:
            return
        self._runtime.log(record.getMessage())


class JobStatus(enum.StrEnum):
    """ジョブのライフサイクルステータス."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        """終端ステータス（SUCCEEDED / FAILED / ABORTED）か."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.ABORTED)


class JobRecord:
    """直近 1 件のジョブ状態（mutable、JobManager がロック保護で更新する）.

    更新系メソッド（set_* / finish / append_log / consume_apply）は JobManager 内部
    専用。外部からは read プロパティのみ参照する。
    """

    def __init__(
        self,
        job_id: str,
        name: str,
        params: Mapping[str, ParamValue],
        log_capacity: int,
    ) -> None:
        self._lock = threading.Lock()
        self._id = job_id
        self._name = name
        self._params = dict(params)
        self._status = JobStatus.PENDING
        self._error: str | None = None
        self._result: JobResult | None = None
        self._log: deque[str] = deque(maxlen=log_capacity)
        self._progress_stage: str | None = None
        self._progress_percent: float | None = None
        self._pending_prompt: tuple[str, PromptSpec] | None = None
        self._apply_consumed = False

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def params(self) -> Mapping[str, ParamValue]:
        with self._lock:
            return dict(self._params)

    @property
    def status(self) -> JobStatus:
        with self._lock:
            return self._status

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def result(self) -> JobResult | None:
        with self._lock:
            return self._result

    @property
    def log_lines(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._log)

    @property
    def progress_stage(self) -> str | None:
        with self._lock:
            return self._progress_stage

    @property
    def progress_percent(self) -> float | None:
        with self._lock:
            return self._progress_percent

    @property
    def pending_prompt(self) -> tuple[str, PromptSpec] | None:
        """応答待ちプロンプト（(prompt_id, PromptSpec)。無ければ None）."""
        with self._lock:
            return self._pending_prompt

    @property
    def apply_available(self) -> bool:
        """「設定に反映」が可能か（SUCCEEDED + payload あり + 未消費）."""
        with self._lock:
            return (
                self._status is JobStatus.SUCCEEDED
                and self._result is not None
                and self._result.apply is not None
                and not self._apply_consumed
            )

    # --- 以下は JobManager 内部専用の更新メソッド ---

    def set_status(self, status: JobStatus) -> None:
        with self._lock:
            self._status = status

    def finish(
        self,
        status: JobStatus,
        *,
        error: str | None = None,
        result: JobResult | None = None,
    ) -> None:
        """終端ステータス・error・result を 1 ロックで原子的に確定する."""
        with self._lock:
            self._status = status
            self._error = error
            self._result = result

    def update_params(self, updates: Mapping[str, ParamValue]) -> None:
        """実行中編集をレコードへ反映する（GET /jobs/current が新値を映す）."""
        with self._lock:
            self._params.update(updates)

    def append_log(self, line: str) -> None:
        with self._lock:
            self._log.append(line)

    def set_progress(self, stage: str, percent: float | None) -> None:
        with self._lock:
            self._progress_stage = stage
            self._progress_percent = percent

    def set_pending_prompt(self, pending: tuple[str, PromptSpec] | None) -> None:
        with self._lock:
            self._pending_prompt = pending

    def consume_apply(self) -> None:
        with self._lock:
            self._apply_consumed = True


class _PendingPrompt:
    """応答待ちプロンプト 1 件のワーカー側待機状態."""

    def __init__(self, spec: PromptSpec) -> None:
        self.id = uuid.uuid4().hex
        self.spec = spec
        self.answer: Answer | None = None
        self.resolved = False
        self.event = threading.Event()


class _JobRuntime:
    """ワーカー 1 本分の同期プリミティブ（JobContext の橋渡し実装）."""

    def __init__(
        self,
        record: JobRecord,
        preview: PreviewService,
        publish: Callable[[_Event], None],
        apply_settings: Callable[[Mapping[str, ParamValue]], None],
        notify_operator: Callable[[], None] = lambda: None,
    ) -> None:
        self.record = record
        self.abort_event = threading.Event()
        self.commands: queue.Queue[Any] = queue.Queue()
        self._preview = preview
        self._publish = publish
        self._apply_settings = apply_settings
        self._notify_operator = notify_operator
        self._pending_lock = threading.Lock()
        self._pending: _PendingPrompt | None = None

    def live_params(self) -> Mapping[str, ParamValue]:
        """現在のパラメータのスナップショット（record が唯一のストア）."""
        return self.record.params

    def update_params(self, updates: Mapping[str, ParamValue]) -> None:
        """Record へ patch を適用する（prompt / sleep 待機中でも反映）."""
        self.record.update_params(updates)

    def publish_status(self) -> None:
        """job_status イベントを発行する（中身は WS 送信時に最新化される）."""
        self._publish({"type": "job_status", "job_id": self.record.id})

    def apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """選択マシンの machine.toml へ即時書き込む（ワーカーからの確定値反映用）."""
        self._apply_settings(values)

    # --- JobContext bridge ---

    def log(self, message: str) -> None:
        self.record.append_log(message)
        self._publish({"type": "log", "job_id": self.record.id, "line": message})

    def progress(self, stage: str, percent: float | None) -> None:
        self.record.set_progress(stage, percent)
        self._publish(
            {
                "type": "progress",
                "job_id": self.record.id,
                "stage": stage,
                "percent": percent,
            }
        )

    def frame(self, image: Image, *, persist: bool = False) -> None:
        self._preview.submit_override(image, persist=persist)

    def clear_frame(self) -> None:
        self._preview.clear_override()

    def prompt(
        self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None
    ) -> Answer:
        self.checkpoint()
        pending = _PendingPrompt(spec)
        with self._pending_lock:
            self._pending = pending
        self.record.set_pending_prompt((pending.id, spec))
        self.record.set_status(JobStatus.WAITING_INPUT)
        self._publish(
            {
                "type": "prompt",
                "job_id": self.record.id,
                "prompt": prompt_payload(pending.id, spec),
            }
        )
        self.publish_status()
        self.notify_operator()

        try:
            if while_waiting is None:
                pending.event.wait()
            else:
                while not pending.event.wait(_PROMPT_POLL_SEC):
                    while_waiting()
            if not pending.resolved:  # abort で起こされた
                raise JobAborted()
        except BaseException:
            # 未解決のまま抜ける経路（abort / while_waiting の例外）でも掃除する。
            # 残すと終端後もクライアントがプロンプトを開いたままになる。
            self._discard_pending(pending)
            self.publish_status()
            raise

        self._discard_pending(pending)
        self.record.set_status(JobStatus.RUNNING)
        self._publish(
            {
                "type": "prompt_resolved",
                "job_id": self.record.id,
                "prompt_id": pending.id,
            }
        )
        self.publish_status()
        assert pending.answer is not None
        return pending.answer

    def _discard_pending(self, pending: _PendingPrompt) -> None:
        """応答待ちプロンプトを runtime と record の両方から取り下げる."""
        with self._pending_lock:
            if self._pending is pending:
                self._pending = None
        self.record.set_pending_prompt(None)

    def notify_operator(self) -> None:
        """オペレータ待ちに入ったことを機体スピーカーで知らせる.

        abort 要求済みなら鳴らさない。待ちへ入る前にジョブが畳まれるので、鳴らすと
        既に死んだジョブの前へ作業者を呼び戻すことになる。``checkpoint`` と違って
        送出はしない（通知は副作用であって制御点ではない）。
        """
        if self.abort_event.is_set():
            return
        self._notify_operator()

    def next_command(self, timeout: float | None) -> dict[str, Any] | None:
        self.checkpoint()
        try:
            item = self.commands.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _ABORT_SENTINEL:
            raise JobAborted()
        return item

    def checkpoint(self) -> None:
        if self.abort_event.is_set():
            raise JobAborted()

    def hold_camera(self) -> AbstractContextManager[FrameHub]:
        return self._preview.hold_camera()

    # --- JobManager 側から呼ばれる対話操作 ---

    def respond(self, prompt_id: str, answer: object) -> None:
        """応答待ちプロンプトに答える.

        Raises:
            ValueError: pending prompt 無し / id 不一致 / 型不一致の場合
                （prompt は未解決のまま待ち続ける）
        """
        with self._pending_lock:
            pending = self._pending
            if pending is None or pending.resolved:
                raise ValueError("応答待ちのプロンプトがありません")
            if pending.id != prompt_id:
                raise ValueError(f"prompt_id が一致しません: {prompt_id}")
            pending.answer = _coerce_answer(pending.spec, answer)
            pending.resolved = True
            pending.event.set()

    def abort(self) -> None:
        """Abort フラグを立て、prompt / command 待機者を起こす."""
        first_request = not self.abort_event.is_set()
        self.abort_event.set()
        self.commands.put(_ABORT_SENTINEL)
        with self._pending_lock:
            if self._pending is not None and not self._pending.resolved:
                self._pending.event.set()
        if first_request:
            self.log("中止要求を受け付けました。安全な停止点で停止します")
            self.publish_status()


class JobManager:
    """ジョブの起動・直近 1 件の状態保持・イベント配信を担うクラス."""

    def __init__(
        self,
        state: AppState,
        preview: PreviewService,
        catalog: JobCatalog,
        settings: Settings,
        board_store: BoardSettingsStore,
        *,
        audio_player: AudioPlayer | None = None,
        log_capacity: int = 500,
    ) -> None:
        """JobManager を初期化する.

        Args:
            state: アプリケーション状態（装置排他ロックの供給元）
            preview: ctx.frame() の委譲先
            catalog: ジョブ定義カタログ
            settings: WebUI 設定（data_dir / pcb_browse_root）
            board_store: 基板設定ストア。**HTTP 経路と同一インスタンス**を
                渡すこと（別インスタンスだと更新ロックが効かない）
            audio_player: ジョブ完了通知音のプレイヤー（None なら再生しない）
            log_capacity: ログのリングバッファ行数
        """
        self._state = state
        self._preview = preview
        self._catalog = catalog
        self._settings = settings
        self._audio_player = audio_player
        self._log_capacity = log_capacity
        self._artifacts_root = settings.webui_data_dir
        self._board_store = board_store

        self._lock = threading.Lock()
        self._record: JobRecord | None = None
        self._runtime: _JobRuntime | None = None
        self._worker: threading.Thread | None = None

        self._subscribers_lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[_Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

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
        pcb_path = self._pcb_path()
        if definition.requires_pcb and pcb_path is None:
            raise ValueError(f"ジョブ {name} には PCB ファイルの選択が必要です")
        machine = self._state.machine()

        self._state.acquire_machine(f"job:{name}")
        try:
            persisted_params = {
                key: params[key] for key in definition.persisted_params if key in params
            }
            if persisted_params:
                self._state.merge_job_param_defaults(name, persisted_params)
            record = JobRecord(uuid.uuid4().hex, name, params, self._log_capacity)
            runtime = _JobRuntime(
                record,
                self._preview,
                self.publish,
                self._apply_machine_settings,
                self._operator_notifier(machine),
            )
            artifacts_dir = self._artifacts_root / record.id
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            selected_pcb = self._state.selected_pcb
            context = JobContext(
                runtime,
                pcb_path=pcb_path,
                machine=machine,
                artifacts_dir=artifacts_dir,
                machine_id=resolve_machine_id(self._settings),
                paste_dataset_dir=self._settings.paste_dataset_dir,
                paste_volume_calibration_dir=(
                    self._settings.paste_volume_calibration_dir
                ),
                source_pcb=selected_pcb.as_posix() if selected_pcb else None,
                board_store=self._board_store,
            )
            worker = threading.Thread(
                target=self._run_worker,
                args=(definition, runtime, context),
                name=f"job-{name}",
                daemon=True,
            )
            with self._lock:
                if self._record is not None:
                    shutil.rmtree(
                        self._artifacts_root / self._record.id, ignore_errors=True
                    )
                self._record = record
                self._runtime = runtime
                self._worker = worker
            runtime.publish_status()
            worker.start()
            return record
        except BaseException:
            self._state.release_machine()
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
            runtime = self._runtime
        if record is None or runtime is None or record.status.terminal:
            return False
        runtime.abort()
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
            runtime = self._runtime
        if runtime is None:
            raise ValueError("応答待ちのプロンプトがありません")
        runtime.respond(prompt_id, answer)

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
            runtime = self._runtime
        if expected_job_id is not None and (
            record is None or record.id != expected_job_id
        ):
            raise LookupError(
                "ジョブが切り替わりました。現在のジョブを確認してください"
            )
        if record is None or runtime is None or record.status.terminal:
            raise ValueError("実行中のジョブがありません")
        definition = self._catalog.get(record.name)
        validated = self._catalog.validate_runtime_params(definition, values)
        runtime.update_params(validated)
        if persist:
            persisted = {
                key: validated[key]
                for key in definition.persisted_params
                if key in validated
            }
            if persisted:
                self._state.merge_job_param_defaults(record.name, persisted)
        runtime.publish_status()
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
            runtime = self._runtime
        if expected_job_id is not None and (
            record is None or record.id != expected_job_id
        ):
            raise LookupError(
                "ジョブが切り替わりました。現在のジョブを確認してください"
            )
        if record is None or runtime is None or record.status.terminal:
            raise ValueError("コマンドを受け付けるジョブが実行中ではありません")
        if not self._catalog.get(record.name).accepts_commands:
            raise ValueError(f"ジョブ {record.name} はコマンドを受け付けません")
        runtime.commands.put(dict(command))

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
        self.publish({"type": "job_status", "job_id": record.id})

    def discard(self, *, expected_job_id: str | None = None) -> None:
        """設定反映ペイロードを無効化する（冪等）."""
        self.mark_applied(expected_job_id=expected_job_id)

    # --- イベント購読（async 側から呼ぶ）---

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """イベント配信先の event loop を登録する（lifespan startup で 1 回）."""
        with self._subscribers_lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[_Event]:
        """イベント購読キューを作成して返す."""
        events: asyncio.Queue[_Event] = asyncio.Queue()
        with self._subscribers_lock:
            self._subscribers.add(events)
        return events

    def unsubscribe(self, events: asyncio.Queue[_Event]) -> None:
        """購読を解除する（未登録キューは無視）."""
        with self._subscribers_lock:
            self._subscribers.discard(events)

    def publish(self, event: _Event) -> None:
        """全 subscriber へイベントを配る（loop 未 bind なら何もしない）.

        購読キューの配布点をここに一本化する。ジョブ以外の発生源（操作権リースの
        `control_changed` 等）も同じキューへ流すため public。ワーカースレッドから
        呼ばれるので、キューへの投入は bind 済み loop の `call_soon_threadsafe` 経由。
        """
        with self._subscribers_lock:
            loop = self._loop
            subscribers = tuple(self._subscribers)
        if loop is None:
            return
        for events in subscribers:
            try:
                loop.call_soon_threadsafe(events.put_nowait, event)
            except RuntimeError:
                # loop が閉じた後の遅延 publish は捨てる
                return

    def publish_state_changed(self) -> None:
        """マシン / PCB / 設定変更をクライアントに通知する."""
        self.publish({"type": "state_changed"})

    def _apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """実行中ジョブの確定値を machine.toml へ即時書き込み、変更を通知する.

        呼び出し元のジョブワーカーが装置排他ロックを保持しているため、
        Apply エンドポイントと同様の直列化が成立している。

        Raises:
            UnknownFieldError: ホワイトリスト外キー・型不一致の場合
        """
        self._state.write_machine_settings(values)
        self.publish_state_changed()

    # --- 内部 ---

    def _run_worker(
        self, definition: JobDefinition, runtime: _JobRuntime, context: JobContext
    ) -> None:
        record = runtime.record
        logger = logging.getLogger(_PCBASM_LOGGER_NAME)
        bridge = _PcbasmLogBridge(runtime, threading.get_ident())
        # INFO が effective level で落ちる場合のみ下げる（finally で復元）
        previous_level = logger.level
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(bridge)
        try:
            record.set_status(JobStatus.RUNNING)
            runtime.publish_status()
            try:
                result = definition.run(context)
                record.finish(JobStatus.SUCCEEDED, result=result)
            except JobAborted:
                record.finish(JobStatus.ABORTED)
            except Exception as exc:
                for line in traceback.format_exc().splitlines():
                    runtime.log(line)
                record.finish(JobStatus.FAILED, error=str(exc) or type(exc).__name__)
            # 装置を動かすジョブは終了時に best-effort で退避する
            if definition.uses_machine:
                self._park_machine(runtime, context)
            self._play_completion_sound(definition, record, context)
            # 終端ステータス確定 → job_status 発行 → ロック解放の順を守る
            runtime.publish_status()
        finally:
            logger.removeHandler(bridge)
            logger.setLevel(previous_level)
            self._state.release_machine()

    def _park_machine(self, runtime: _JobRuntime, context: JobContext) -> None:
        """ジョブ終了時にノズルキャップ駐機（フォールバックは PRESENT / M84）を行う（失敗はlogのみ）。"""
        klipper_config = context.machine.klipper
        try:
            klipper = Klipper(
                host=klipper_config.host,
                port=klipper_config.port,
                timeout=PRESENT_TIMEOUT,
            )
            park_or_present(
                klipper, context.machine, warn=runtime.log, timeout=PRESENT_TIMEOUT
            )
        except Exception as exc:
            runtime.log(f"タスク終了時の退避に失敗: {exc}")

    def _operator_notifier(self, machine: Machine) -> Callable[[], None]:
        """オペレータ待ちで鳴らす入力待ち音の再生関数を作る.

        再生完了を待たずに戻り、再生失敗は warning のみ（待ちを塞がない）。
        """
        player = self._audio_player
        if player is None:
            return lambda: None
        return lambda: self._play_sound(player, "prompt", machine.audio, "入力待ち音")

    def _play_sound(
        self, player: AudioPlayer, sound: Sound, audio: Audio, label: str
    ) -> None:
        """通知音を非同期に開始する（開始・再生の失敗はいずれも warning のみ）."""
        try:
            future = player.play(sound, audio)
        except Exception:
            _logger.warning("%sを開始できませんでした", label, exc_info=True)
            return
        future.add_done_callback(_warn_sound_failure(label))

    def _play_completion_sound(
        self, definition: JobDefinition, record: JobRecord, context: JobContext
    ) -> None:
        """成功・失敗通知音を非同期に開始する（失敗は warning のみ）.

        装置を動かすジョブ（``uses_machine``）だけを鳴らす。実時間がかかり作業者が
        装置の前を離れうるのはこの区分で、PCB 生成のような即終了ジョブは画面で足りる。
        ブラウザ完了通知の ``notify_on_completion`` とは独立の判定。
        """
        if not definition.uses_machine or self._audio_player is None:
            return
        status = record.status
        if status not in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return
        sound: Sound = "success" if status is JobStatus.SUCCEEDED else "failure"
        self._play_sound(
            self._audio_player, sound, context.machine.audio, "ジョブ完了通知音"
        )

    def _pcb_path(self) -> Path | None:
        selected = self._state.selected_pcb
        if selected is None:
            return None
        return (self._settings.pcb_browse_root / selected).resolve()


def _warn_sound_failure(label: str) -> Callable[[Future[None]], None]:
    """通知音の再生失敗を warning に落とす done callback を作る."""

    def callback(future: Future[None]) -> None:
        if (error := future.exception()) is not None:
            _logger.warning("%sの再生に失敗しました: %s", label, error)

    return callback


def prompt_payload(prompt_id: str, spec: PromptSpec) -> dict[str, Any]:
    """WS "prompt" イベント / PromptInfo 用の辞書を作る."""
    return {
        "id": prompt_id,
        "kind": spec.kind,
        "message": spec.message,
        "default": spec.default,
        "true_label": spec.true_label,
        "false_label": spec.false_label,
        "choices": list(spec.choices),
    }


def _coerce_answer(spec: PromptSpec, answer: object) -> Answer:
    """プロンプト応答を PromptSpec の型に合わせて検証・変換する.

    confirm→bool, number→float（int は float 化）, text→str,
    choice→choices 内の str。false_label 付き number の中止（bool False）は
    そのまま False を返す。

    Raises:
        ValueError: 型不一致・choice 範囲外の場合
    """
    match spec.kind:
        case "confirm":
            if isinstance(answer, bool):
                return answer
        case "number":
            # false_label 付き number は中止可能。中止ボタンは bool False を送る。
            if answer is False and spec.false_label is not None:
                return False
            if not isinstance(answer, bool) and isinstance(answer, (int, float)):
                return float(answer)
        case "text":
            if isinstance(answer, str):
                return answer
        case "choice":
            if isinstance(answer, str) and answer in spec.choices:
                return answer
    raise ValueError(
        f"{spec.kind} プロンプトへの応答として不正です（与えられた値: {answer!r}）"
    )
