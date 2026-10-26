"""ジョブ 1 件の記録（状態・ログ・進捗・応答待ち・結果）と、そこに残る値の型."""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Literal

import attrs

type Answer = bool | float | str
type ParamValue = bool | float | int | str
type PromptKind = Literal["confirm", "number", "text", "choice"]


@attrs.frozen
class PromptSpec:
    """ユーザーへの問い合わせ 1 件の仕様.

    Attributes:
        kind: 入力種別（confirm / number / text / choice）
        message: 表示メッセージ
        default: 既定値（UI の初期値。None は既定値なし）
        choices: kind="choice" の選択肢（choice のみ必須）
        true_label: kind="confirm" の true 側ボタンラベル
        false_label: kind="confirm" の false 側ボタンラベル（kind="number" では中止ボタン）

    応答待ちに入ると機体のスピーカーが入力待ち音を鳴らす（:meth:`JobContext.prompt`
    が常に行う。ジョブ側の指定は不要）。
    """

    kind: PromptKind
    message: str
    default: bool | float | str | None = None
    choices: tuple[str, ...] = ()
    true_label: str | None = None
    false_label: str | None = None


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
    """設定反映時に config/ 直下へ書き込むファイル."""

    filename: str
    content: bytes


@attrs.frozen
class ApplyPayload:
    """SUCCEEDED ジョブが提示する「設定に反映」ペイロード.

    Attributes:
        label: コンソール表示用（例「canny_low = 60.0 を設定に反映」）
        values: machine.toml の設定キー → 値
        files: config/ へ書き込む追加ファイル
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

    更新系メソッド（set_* / finish / append_log / consume_apply）は実行基盤の内部専用。
    外部からは read プロパティのみ参照する。
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
        # 実測のタクトタイム（開始から stop_clock まで）
        self._started_at = time.monotonic()
        self._finished_at: float | None = None

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
    def elapsed_seconds(self) -> float:
        """開始からの経過秒（stop_clock の後はその時点の値のまま止まる）."""
        with self._lock:
            end = time.monotonic() if self._finished_at is None else self._finished_at
            return end - self._started_at

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

    # --- 以下は実行基盤の内部専用の更新メソッド ---

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

    def stop_clock(self) -> None:
        """経過秒を止める（終了後の退避も作業者が待つ時間なので、終端を知らせる直前に呼ぶ）."""
        with self._lock:
            self._finished_at = time.monotonic()

    def add_artifact(self, artifact: Artifact) -> None:
        """成果物を足す（失敗・中止で result が無ければ作る）."""
        with self._lock:
            result = self._result or JobResult()
            self._result = attrs.evolve(result, artifacts=(*result.artifacts, artifact))

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
