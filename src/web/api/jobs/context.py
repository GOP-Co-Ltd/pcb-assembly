"""ワーカースレッドと Web 層をつなぐ JobContext と関連型."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol

import attrs

from pcbasm.config import Machine
from pcbasm.hal import Camera, FrameHub
from pcbasm.vision import Image
from web.api.board_settings import BoardSettingsStore

type Answer = bool | float | str
type ParamValue = bool | float | int | str
type PromptKind = Literal["confirm", "number", "text", "choice"]


class JobAborted(Exception):
    """Abort 要求により協調的に中断されたことを示す（worker 内部制御用）."""


@attrs.frozen
class PromptSpec:
    """ユーザーへの問い合わせ 1 件の仕様.

    Attributes:
        kind: 入力種別（confirm / number / text / choice）
        message: 表示メッセージ
        default: 既定値（UI の初期値。None は既定値なし）
        choices: kind="choice" の選択肢（choice のみ必須）
        true_label: kind="confirm" の true 側ボタンラベル
        false_label: kind="confirm" の false 側ボタンラベル

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
    """設定反映時に config/ 直下へ書き込むファイル（Phase 4 用）."""

    filename: str
    content: bytes


@attrs.frozen
class ApplyPayload:
    """SUCCEEDED ジョブが提示する「設定に反映」ペイロード.

    Attributes:
        label: コンソール表示用（例「canny_low = 60.0 を設定に反映」）
        values: machine.toml ホワイトリストキー → 値
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


class JobBridge(Protocol):
    """JobContext が委譲するワーカー同期機構（JobManager 内部が実装する）."""

    def live_params(self) -> Mapping[str, ParamValue]: ...

    def apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None: ...

    def log(self, message: str) -> None: ...

    def progress(self, stage: str, percent: float | None) -> None: ...

    def frame(self, image: Image, *, persist: bool = False) -> None: ...

    def clear_frame(self) -> None: ...

    def prompt(
        self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None
    ) -> Answer: ...

    def next_command(self, timeout: float | None) -> dict[str, Any] | None: ...

    def notify_operator(self) -> None: ...

    def checkpoint(self) -> None: ...

    def hold_camera(self) -> AbstractContextManager[FrameHub]: ...


class JobContext:
    """ワーカースレッド ↔ Web 層の橋。JobManager だけが生成する.

    ジョブ関数（``JobDefinition.run``）はこのオブジェクトを通じてログ・
    進捗・プレビューフレーム・対話（prompt / command）・中断チェックを行う。
    直接コンストラクトしない。
    """

    def __init__(
        self,
        bridge: JobBridge,
        *,
        pcb_path: Path | None,
        machine: Machine,
        artifacts_dir: Path,
        machine_id: str,
        paste_dataset_dir: Path,
        paste_volume_calibration_dir: Path,
        source_pcb: str | None = None,
        board_store: BoardSettingsStore | None = None,
    ) -> None:
        # パラメータは保持しない（ブリッジの live_params() 経由で都度読む）
        self._bridge = bridge
        self._pcb_path = pcb_path
        self._machine = machine
        self._artifacts_dir = artifacts_dir
        self._machine_id = machine_id
        self._paste_dataset_dir = paste_dataset_dir
        self._paste_volume_calibration_dir = paste_volume_calibration_dir
        self._source_pcb = source_pcb
        self._board_store = board_store

    @property
    def params(self) -> Mapping[str, ParamValue]:
        """Catalog 検証済みのパラメータ（実行中編集を毎アクセスで反映）.

        ライブストア（ブリッジの ``live_params()``）から都度読み直すため、
        ``JobManager.update_current_params`` での変更が次回読みで反映される。
        """
        return self._bridge.live_params()

    @property
    def pcb_path(self) -> Path | None:
        """選択 PCB の絶対パス（requires_pcb=False なら None があり得る）."""
        return self._pcb_path

    @property
    def machine(self) -> Machine:
        """選択マシンの設定（ジョブ開始時に 1 回ロード済み）."""
        return self._machine

    @property
    def artifacts_dir(self) -> Path:
        """成果物ディレクトリ data/webui/<job_id>/（作成済み）."""
        return self._artifacts_dir

    @property
    def machine_id(self) -> str:
        """Backend が自己申告する一意な machine ID."""
        return self._machine_id

    @property
    def paste_dataset_dir(self) -> Path:
        """永続ペースト dataset root（テストでは Settings から注入可能）."""
        return self._paste_dataset_dir

    @property
    def paste_volume_calibration_dir(self) -> Path:
        """永続の塗布量校正 root（テストでは Settings から注入可能）."""
        return self._paste_volume_calibration_dir

    @property
    def source_pcb(self) -> str | None:
        """選択 PCB の pcb_browse_root 相対 posix パス（未選択/未配線なら None）."""
        return self._source_pcb

    @property
    def board_store(self) -> BoardSettingsStore | None:
        """基板ごとの塗布設定ストア（未配線なら None）."""
        return self._board_store

    def artifact(
        self, label: str, filename: str, kind: Literal["image", "file"]
    ) -> Artifact:
        """``artifacts_dir`` 直下のファイルを指す Artifact を作る."""
        return Artifact(
            label=label, path=f"{self._artifacts_dir.name}/{filename}", kind=kind
        )

    def apply_machine_settings(self, values: Mapping[str, ParamValue]) -> None:
        """選択マシンの machine.toml へホワイトリスト項目を即時書き込む.

        キャリブレーション値の確定時など、ジョブ完了（と Apply 操作）を
        待たずに計測結果を永続化する用途。書き込み後は "state_changed" が
        発行され、設定画面等が追従する。

        Raises:
            UnknownFieldError: ホワイトリスト外キー・型不一致の場合
        """
        self._bridge.apply_machine_settings(values)

    def log(self, message: str) -> None:
        """ログのリングバッファへ追記し、WS "log" イベントを発行する."""
        self._bridge.log(message)

    def progress(self, stage: str, percent: float | None = None) -> None:
        """直近の進捗を record に保持し、WS "progress" イベントを発行する."""
        self._bridge.progress(stage, percent)

    def frame(self, image: Image, *, persist: bool = False) -> None:
        """プレビューのオーバーライドスロットへフレームを書き込む."""
        self._bridge.frame(image, persist=persist)

    def clear_frame(self) -> None:
        """プレビューのオーバーライドスロットを空にする."""
        self._bridge.clear_frame()

    def prompt(
        self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None
    ) -> Answer:
        """WAITING_INPUT へ遷移し、ユーザー応答までブロックする.

        応答後 RUNNING に復帰し "prompt_resolved" + "job_status" を発行する。

        Args:
            spec: 問い合わせ内容
            while_waiting: 応答が来るまでポーリング間隔ごとに呼ばれるコールバック。
                ``frame()`` の override は PreviewService の TTL で失効するため、
                待機中もオーバーレイを出し続けたいジョブがライブフレームの
                再送に使う。省略時はイベント待ちでブロックする（従来動作）。
                コールバックが投げた例外はそのまま伝播し、ジョブは FAILED になる

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._bridge.prompt(spec, while_waiting=while_waiting)

    def next_command(self, timeout: float | None = None) -> dict[str, Any] | None:
        """WS "command" のキューから 1 件取得する（timeout 超過は None）.

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._bridge.next_command(timeout)

    def notify_operator(self) -> None:
        """機体のスピーカーで入力待ち音を鳴らし、装置の前を離れた作業者を呼び戻す.

        :meth:`prompt` は応答待ちに入るときこれを自動で行う。コマンド待ち
        （``next_command(timeout=None)``）のように prompt を出さないオペレータ
        待ちへ入る段階で、ジョブが明示的に呼ぶ。再生の失敗は warning ログに残り、
        ジョブへは伝播しない。
        """
        self._bridge.notify_operator()

    def checkpoint(self) -> None:
        """Abort 要求済みなら JobAborted を送出する（それ以外は no-op）.

        Raises:
            JobAborted: abort 要求済みの場合
        """
        self._bridge.checkpoint()

    @contextlib.contextmanager
    def open_camera(self) -> Iterator[Camera]:
        """カメラパイプラインを起動保持し FrameSource を貸し出す.

        PreviewService と参照カウントを共有するため、preview クライアントの
        切断でジョブ使用中の hub が止まることはない（逆も同様）。

        Raises:
            OSError: カメラデバイスが見つからない・開けない場合
            RuntimeError: カメラがフォーマット等をサポートしない場合
        """
        with self._bridge.hold_camera() as hub:
            yield hub.subscribe()
