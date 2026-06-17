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
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, override

import attrs

from pcbasm.hal import FrameHub, Klipper
from pcbasm.hal.klipper import PRESENT_TIMEOUT
from pcbasm.vision import Image
from webui.board_settings import BoardSettingsStore
from webui.jobs.catalog import JobCatalog, JobDefinition
from webui.jobs.context import (
    Answer,
    JobAborted,
    JobContext,
    ParamValue,
    PromptSpec,
)
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

type _Event = dict[str, Any]

# next_command 待機を強制的に JobAborted 化するための番兵
_ABORT_SENTINEL: Any = object()

# ジョブコンソールへ転送する pcbasm ロガー名
_PCBASM_LOGGER_NAME = "pcbasm"


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


@attrs.frozen
class Artifact:
    """ジョブ成果物 1 件.

    Attributes:
        label: UI 表示名
        path: data/webui/ からの相対パス（URL = /artifacts/<path>）
        kind: image はインライン表示、file はダウンロードリンク
    """

    label: str
    path: str
    kind: Literal["image", "file"]


@attrs.frozen
class ApplyFile:
    """設定反映時に configs/<machine>/ 直下へ書き込むファイル（Phase 4 用）."""

    filename: str
    content: bytes


@attrs.frozen
class ApplyPayload:
    """SUCCEEDED ジョブが提示する「設定に反映」ペイロード.

    Attributes:
        label: コンソール表示用（例「canny_low = 60.0 を設定に反映」）
        values: machine.toml ホワイトリストキー → 値
        files: configs/<machine>/ へ書き込む追加ファイル
    """

    label: str
    values: Mapping[str, ParamValue]
    files: tuple[ApplyFile, ...] = ()


@attrs.frozen
class JobResult:
    """ジョブ関数の戻り値（成果サマリ・成果物・設定反映ペイロード）."""

    summary: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    apply: ApplyPayload | None = None


class JobRecord:
    """直近 1 件のジョブ状態（mutable、JobManager がロック保護で更新する）.

    更新系メソッド（set_* / append_log / consume_apply）は JobManager 内部 専用。外部からは
    read プロパティのみ参照する。
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
        return self._params

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

    def set_error(self, error: str) -> None:
        with self._lock:
            self._error = error

    def set_result(self, result: JobResult | None) -> None:
        with self._lock:
            self._result = result

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
    ) -> None:
        self.record = record
        self.abort_event = threading.Event()
        self.commands: queue.Queue[Any] = queue.Queue()
        self._preview = preview
        self._publish = publish
        self._pending_lock = threading.Lock()
        self._pending: _PendingPrompt | None = None

    def publish_status(self) -> None:
        """job_status イベントを発行する（中身は WS 送信時に最新化される）."""
        self._publish({"type": "job_status", "job_id": self.record.id})

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

    def prompt(self, spec: PromptSpec) -> Answer:
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

        pending.event.wait()
        if not pending.resolved:  # abort で起こされた
            with self._pending_lock:
                if self._pending is pending:
                    self._pending = None
            self.record.set_pending_prompt(None)
            self.publish_status()
            raise JobAborted()

        with self._pending_lock:
            self._pending = None
        self.record.set_pending_prompt(None)
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
        *,
        log_capacity: int = 500,
    ) -> None:
        """JobManager を初期化する.

        Args:
            state: アプリケーション状態（装置排他ロックの供給元）
            preview: ctx.frame() の委譲先
            catalog: ジョブ定義カタログ
            settings: WebUI 設定（data_dir / pcb_browse_root）
            log_capacity: ログのリングバッファ行数
        """
        self._state = state
        self._preview = preview
        self._catalog = catalog
        self._settings = settings
        self._log_capacity = log_capacity
        self._artifacts_root = settings.webui_data_dir
        self._board_store = BoardSettingsStore(
            settings.webui_data_dir, legacy_root=settings.data_dir / "board_settings"
        )

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
                self._state.save_job_param_defaults(name, persisted_params)
            record = JobRecord(uuid.uuid4().hex, name, params, self._log_capacity)
            runtime = _JobRuntime(record, self._preview, self._publish)
            artifacts_dir = self._artifacts_root / record.id
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            selected_pcb = self._state.selected_pcb
            context = JobContext(
                runtime,
                params=params,
                pcb_path=pcb_path,
                machine=machine,
                artifacts_dir=artifacts_dir,
                machine_name=self._state.selected_machine,
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

    def submit_command(self, command: Mapping[str, Any]) -> None:
        """実行中ジョブの command キューへ 1 件投入する.

        Raises:
            ValueError: accepts_commands なジョブが実行中でない /
                "type" キーが無い場合
        """
        if "type" not in command:
            raise ValueError('command には "type" キーが必要です')
        with self._lock:
            record = self._record
            runtime = self._runtime
        if record is None or runtime is None or record.status.terminal:
            raise ValueError("コマンドを受け付けるジョブが実行中ではありません")
        if not self._catalog.get(record.name).accepts_commands:
            raise ValueError(f"ジョブ {record.name} はコマンドを受け付けません")
        runtime.commands.put(dict(command))

    # --- Apply / Discard ---

    def apply_payload(self) -> ApplyPayload:
        """直近 SUCCEEDED ジョブの設定反映ペイロードを返す.

        Raises:
            LookupError: 直近ジョブ無し / SUCCEEDED でない / payload 無し /
                適用・破棄済みの場合（→ 409）
        """
        with self._lock:
            record = self._record
        if record is None or not record.apply_available:
            raise LookupError("設定に反映可能な計測結果がありません")
        result = record.result
        assert result is not None and result.apply is not None
        return result.apply

    def mark_applied(self) -> None:
        """書込成功後にルーターが呼ぶ（以後 apply_payload は LookupError）."""
        with self._lock:
            record = self._record
        if record is not None:
            record.consume_apply()

    def discard(self) -> None:
        """設定反映ペイロードを無効化する（冪等）."""
        self.mark_applied()

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

    def publish_state_changed(self) -> None:
        """マシン / PCB / 設定変更をクライアントに通知する."""
        self._publish({"type": "state_changed"})

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
                record.set_result(result)
                record.set_status(JobStatus.SUCCEEDED)
            except JobAborted:
                record.set_status(JobStatus.ABORTED)
            except Exception as exc:
                record.set_error(str(exc) or type(exc).__name__)
                for line in traceback.format_exc().splitlines():
                    runtime.log(line)
                record.set_status(JobStatus.FAILED)
            # 装置を動かすジョブは終了時に best-effort で基板を差し出す
            if definition.uses_machine:
                self._present_machine(runtime, context)
            # 終端ステータス確定 → job_status 発行 → ロック解放の順を守る
            runtime.publish_status()
        finally:
            logger.removeHandler(bridge)
            logger.setLevel(previous_level)
            self._state.release_machine()

    def _present_machine(self, runtime: _JobRuntime, context: JobContext) -> None:
        """ジョブ終了時にPRESENT、無ければM84を送る（失敗はlogのみ）。"""
        klipper_config = context.machine.klipper
        try:
            klipper = Klipper(
                host=klipper_config.host,
                port=klipper_config.port,
                timeout=PRESENT_TIMEOUT,
            )
            klipper.send_present_or_relax(warn=runtime.log, timeout=PRESENT_TIMEOUT)
        except Exception as exc:
            runtime.log(f"PRESENT / relax (M84) 送信失敗: {exc}")

    def _pcb_path(self) -> Path | None:
        selected = self._state.selected_pcb
        if selected is None:
            return None
        return (self._settings.pcb_browse_root / selected).resolve()

    def _publish(self, event: _Event) -> None:
        """全 subscriber へイベントを配る（loop 未 bind なら何もしない）."""
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
    choice→choices 内の str。

    Raises:
        ValueError: 型不一致・choice 範囲外の場合
    """
    match spec.kind:
        case "confirm":
            if isinstance(answer, bool):
                return answer
        case "number":
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
