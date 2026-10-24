"""ワーカースレッドと Web 層をつなぐ JobContext と、その同期を担う JobChannel."""

from __future__ import annotations

import contextlib
import queue
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, overload

from pcbasm.board import Board
from pcbasm.config import MachineConfig
from pcbasm.hal import Camera, FrameHub, KlipperClient
from pcbasm.machine.devices import Devices
from pcbasm.machine.journal import Journal
from pcbasm.validators import is_finite_number
from pcbasm.vision import Image
from web.api.camera import CameraFeed
from web.api.events import Event
from web.api.jobs.runtime.record import (
    Answer,
    Artifact,
    JobRecord,
    JobStatus,
    ParamValue,
    PromptSpec,
)
from web.api.selection import BoardCache

# next_command 待機を強制的に JobAborted 化するための番兵
_ABORT_SENTINEL: Any = object()

# prompt(while_waiting=...) のポーリング間隔 [sec]
_PROMPT_POLL_SEC = 0.05


class JobAborted(Exception):
    """Abort 要求により協調的に中断されたことを示す（worker 内部制御用）."""


class _PendingPrompt:
    """応答待ちプロンプト 1 件のワーカー側待機状態."""

    def __init__(self, spec: PromptSpec) -> None:
        self.id = uuid.uuid4().hex
        self.spec = spec
        self.answer: Answer | None = None
        self.resolved = False
        self.event = threading.Event()


class JobChannel:
    """ワーカー 1 本分の同期（応答待ち・コマンド・中止・イベントの発行）.

    JobManager がジョブごとに 1 つ作る。ワーカー側は JobContext を通して、Web 側は JobManager
    を通して使う。
    """

    def __init__(
        self,
        record: JobRecord,
        camera: CameraFeed,
        publish: Callable[[Event], None],
        apply_settings: Callable[[Mapping[str, ParamValue]], None],
        notify_operator: Callable[[], None] = lambda: None,
    ) -> None:
        self.record = record
        self._abort_event = threading.Event()
        self._commands: queue.Queue[Any] = queue.Queue()
        self._camera = camera
        self._publish = publish
        self._apply_settings = apply_settings
        self._notify_operator = notify_operator
        self._pending_lock = threading.Lock()
        self._pending: _PendingPrompt | None = None

    def publish_status(self) -> None:
        """job_status イベントを発行する（中身は WS 送信時に最新化される）."""
        self._publish({"type": "job_status", "job_id": self.record.id})

    # --- ワーカー側（JobContext が呼ぶ） ---

    def apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """選択マシンの machine.toml へ即時書き込む（ワーカーからの確定値反映用）."""
        self._apply_settings(values)

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
        self._camera.submit_override(image, persist=persist)

    def clear_frame(self) -> None:
        self._camera.clear_override()

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
        """応答待ちプロンプトを channel と record の両方から取り下げる."""
        with self._pending_lock:
            if self._pending is pending:
                self._pending = None
        self.record.set_pending_prompt(None)

    def notify_operator(self) -> None:
        """オペレータ待ちに入ったことを機体スピーカーで知らせる.

        abort 要求済みなら鳴らさない。待ちへ入る前にジョブが終了処理へ進むので、
        鳴らすと終了済みのジョブのために作業者を呼び戻すことになる。``checkpoint`` と違って
        送出はしない（通知は副作用であって制御点ではない）。
        """
        if self._abort_event.is_set():
            return
        self._notify_operator()

    def next_command(self, timeout: float | None) -> dict[str, Any] | None:
        self.checkpoint()
        try:
            item = self._commands.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _ABORT_SENTINEL:
            raise JobAborted()
        return item

    def checkpoint(self) -> None:
        if self._abort_event.is_set():
            raise JobAborted()

    def hold_camera(self) -> AbstractContextManager[FrameHub]:
        return self._camera.hold()

    # --- Web 側（JobManager が呼ぶ） ---

    def update_params(self, updates: Mapping[str, ParamValue]) -> None:
        """Record へ patch を適用する（prompt / sleep 待機中でも反映）."""
        self.record.update_params(updates)

    def submit(self, command: Mapping[str, Any]) -> None:
        """WS command をワーカーの待ち行列へ積む."""
        self._commands.put(dict(command))

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
        first_request = not self._abort_event.is_set()
        self._abort_event.set()
        self._commands.put(_ABORT_SENTINEL)
        with self._pending_lock:
            if self._pending is not None and not self._pending.resolved:
                self._pending.event.set()
        if first_request:
            self.log("中止要求を受け付けました。安全な停止点で停止します")
            self.publish_status()


class JobContext:
    """ワーカースレッド ↔ Web 層の橋。JobManager だけが生成する.

    ジョブ関数（``JobDefinition.run``）はこのオブジェクトを通じてログ・
    進捗・プレビューフレーム・対話（prompt / command）・中断チェックを行う。
    直接コンストラクトしない。
    """

    def __init__(
        self,
        channel: JobChannel,
        *,
        pcb_path: Path | None,
        machine: MachineConfig,
        devices: Devices,
        artifacts_dir: Path,
        journal: Journal,
        machine_id: str,
        source_pcb: str | None,
        boards: BoardCache,
    ) -> None:
        # パラメータは保持しない（record から都度読む）
        self._channel = channel
        self._pcb_path = pcb_path
        self._machine = machine
        self._devices = devices
        self._artifacts_dir = artifacts_dir
        self._journal = journal
        self._machine_id = machine_id
        self._source_pcb = source_pcb
        self._boards = boards

    @property
    def params(self) -> Mapping[str, ParamValue]:
        """Catalog 検証済みのパラメータ（実行中編集を毎アクセスで反映）.

        record から都度読み直すため、``JobManager.update_current_params`` での変更が次回読みで反映される。
        """
        return self._channel.record.params

    @property
    def pcb_path(self) -> Path | None:
        """選択 PCB の絶対パス（requires_pcb=False なら None があり得る）."""
        return self._pcb_path

    @property
    def machine(self) -> MachineConfig:
        """選択マシンの設定（ジョブ開始時に 1 回ロード済み）."""
        return self._machine

    @property
    def devices(self) -> Devices:
        """機体の装置（Klipper、カメラ、通知音）."""
        return self._devices

    @property
    def artifacts_dir(self) -> Path:
        """成果物ディレクトリ data/webui/<job_id>/（作成済み）."""
        return self._artifacts_dir

    @property
    def journal(self) -> Journal:
        """このジョブの送信を書く G-code ジャーナル."""
        return self._journal

    def klipper(self, timeout: float | None = None) -> KlipperClient:
        """装置の Klipper への接続（接続先はジョブ開始時の設定、送信はこのジョブのジャーナルへ書く）.

        Args:
            timeout: 送信ごとの指定が無いときと、問い合わせの timeout 秒数（None は無制限）
        """
        return self._devices.klipper(
            timeout, journal=self._journal, endpoint=self._machine.klipper
        )

    @property
    def machine_id(self) -> str:
        """Backend が自己申告する一意な machine ID."""
        return self._machine_id

    @property
    def source_pcb(self) -> str | None:
        """選択 PCB の pcb_browse_root 相対 posix パス（未選択なら None）."""
        return self._source_pcb

    def board(self) -> Board:
        """選択 PCB の Board を返す（requires_pcb=True のジョブだけが呼ぶ）.

        前回読んだときからファイルが差し替わっていれば読み直す。
        """
        assert self._pcb_path is not None  # requires_pcb=True
        return self._boards.get(self._pcb_path)

    def artifact(
        self, label: str, filename: str, kind: Literal["image", "file"]
    ) -> Artifact:
        """``artifacts_dir`` 直下のファイルを指す Artifact を作る."""
        return Artifact(
            label=label, path=f"{self._artifacts_dir.name}/{filename}", kind=kind
        )

    def apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """選択マシンの machine.toml へ設定項目を即時書き込む.

        キャリブレーション値の確定時など、ジョブ完了（と Apply 操作）を
        待たずに計測結果を永続化する用途。書き込み後は "state_changed" が
        発行され、設定画面等が追従する。

        Raises:
            InvalidSettingError: 未知のキー・不正な値の場合
        """
        self._channel.apply_machine_settings(values)

    def log(self, message: str) -> None:
        """ログのリングバッファへ追記し、WS "log" イベントを発行する."""
        self._channel.log(message)

    def progress(self, stage: str, percent: float | None = None) -> None:
        """直近の進捗を record に保持し、WS "progress" イベントを発行する."""
        self._channel.progress(stage, percent)

    def frame(self, image: Image, *, persist: bool = False) -> None:
        """プレビューのオーバーライドスロットへフレームを書き込む."""
        self._channel.frame(image, persist=persist)

    def clear_frame(self) -> None:
        """プレビューのオーバーライドスロットを空にする."""
        self._channel.clear_frame()

    def prompt(
        self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None
    ) -> Answer:
        """WAITING_INPUT へ遷移し、ユーザー応答までブロックする.

        応答後 RUNNING に復帰し "prompt_resolved" + "job_status" を発行する。

        Args:
            spec: 問い合わせ内容
            while_waiting: 応答が来るまでポーリング間隔ごとに呼ばれるコールバック。
                ``frame()`` の override は CameraFeed の TTL で失効するため、
                待機中もオーバーレイを出し続けたいジョブがライブフレームの
                再送に使う。省略時はイベント待ちでブロックする（従来動作）。
                コールバックが投げた例外はそのまま伝播し、ジョブは FAILED になる

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._channel.prompt(spec, while_waiting=while_waiting)

    def confirm(
        self,
        message: str,
        *,
        true_label: str = "続行",
        false_label: str = "中止",
        while_waiting: Callable[[], None] | None = None,
    ) -> bool:
        """2 択のボタンで尋ね、true 側なら True を返す（既定のボタンは true 側）.

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return bool(
            self.prompt(
                PromptSpec(
                    kind="confirm",
                    message=message,
                    default=True,
                    true_label=true_label,
                    false_label=false_label,
                ),
                while_waiting=while_waiting,
            )
        )

    def require(
        self, message: str, *, true_label: str = "続行", false_label: str = "中止"
    ) -> None:
        """:meth:`confirm` で尋ね、false 側ならジョブを中止する.

        Raises:
            JobAborted: false 側を選んだ場合と、待機中に abort された場合
        """
        if not self.confirm(message, true_label=true_label, false_label=false_label):
            raise JobAborted()

    def choose(self, message: str, choices: tuple[str, ...], *, default: str) -> str:
        """選択肢から 1 つ選ばせる.

        Raises:
            JobAborted: 待機中に abort された場合
        """
        answer = self.prompt(
            PromptSpec(kind="choice", message=message, default=default, choices=choices)
        )
        assert isinstance(answer, str)  # choice の応答は choices のどれか
        return answer

    @overload
    def ask_positive(self, message: str, *, default: float | None = None) -> float: ...

    @overload
    def ask_positive(
        self, message: str, *, default: float | None = None, cancel_label: str
    ) -> float | None: ...

    def ask_positive(
        self,
        message: str,
        *,
        default: float | None = None,
        cancel_label: str | None = None,
    ) -> float | None:
        """正の数が入力されるまで尋ねる.

        ``cancel_label`` を渡すと入力欄に中止ボタンを出し、押されたら ``None`` を返す。

        Raises:
            JobAborted: 待機中に abort された場合
        """
        while True:
            answer = self.prompt(
                PromptSpec(
                    kind="number",
                    message=message,
                    default=default,
                    false_label=cancel_label,
                )
            )
            if answer is False:  # 中止ボタン（cancel_label 指定時のみ届く）
                return None
            assert isinstance(answer, float)
            if answer > 0:
                return answer
            self.log(f"正の数値を入力してください（与えられた値: {answer}）")

    def next_command(self, timeout: float | None = None) -> dict[str, Any] | None:
        """WS "command" のキューから 1 件取得する（timeout 超過は None）.

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._channel.next_command(timeout)

    def notify_operator(self) -> None:
        """機体のスピーカーで入力待ち音を鳴らし、装置の前を離れた作業者を呼び戻す.

        :meth:`prompt` は応答待ちに入るときこれを自動で行う。コマンド待ち
        （``next_command(timeout=None)``）のように prompt を出さないオペレータ
        待ちへ入る段階で、ジョブが明示的に呼ぶ。再生の失敗は warning ログに残り、
        ジョブへは伝播しない。
        """
        self._channel.notify_operator()

    def checkpoint(self) -> None:
        """Abort 要求済みなら JobAborted を送出する（それ以外は no-op）.

        Raises:
            JobAborted: abort 要求済みの場合
        """
        self._channel.checkpoint()

    @contextlib.contextmanager
    def open_camera(self) -> Iterator[Camera]:
        """カメラパイプラインを起動保持し FrameSource を貸し出す.

        MJPEG ストリーム（CameraFeed）と参照カウントを共有するため、preview クライアントの
        切断でジョブ使用中の hub が止まることはない（逆も同様）。

        Raises:
            OSError: カメラデバイスが見つからない・開けない場合
            RuntimeError: カメラがフォーマット等をサポートしない場合
        """
        with self._channel.hold_camera() as hub:
            yield hub.subscribe()


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

    confirm→bool, number→有限な float（int は float 化）, text→str,
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
            if is_finite_number(answer):
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
