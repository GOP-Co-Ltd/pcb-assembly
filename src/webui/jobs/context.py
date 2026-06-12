"""ワーカースレッドと Web 層をつなぐ JobContext と関連型."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import attrs

from pcbasm.config import Machine
from pcbasm.vision import Image

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
    """

    kind: PromptKind
    message: str
    default: bool | float | str | None = None
    choices: tuple[str, ...] = ()


class JobBridge(Protocol):
    """JobContext が委譲するワーカー同期機構（JobManager 内部が実装する）."""

    def log(self, message: str) -> None: ...

    def progress(self, stage: str, percent: float | None) -> None: ...

    def frame(self, image: Image) -> None: ...

    def prompt(self, spec: PromptSpec) -> Answer: ...

    def next_command(self, timeout: float | None) -> dict[str, Any] | None: ...

    def checkpoint(self) -> None: ...


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
        params: Mapping[str, ParamValue],
        pcb_path: Path | None,
        machine: Machine,
        artifacts_dir: Path,
    ) -> None:
        self._bridge = bridge
        self._params = dict(params)
        self._pcb_path = pcb_path
        self._machine = machine
        self._artifacts_dir = artifacts_dir

    @property
    def params(self) -> Mapping[str, ParamValue]:
        """Catalog 検証済み（default 充填済み）のパラメータ."""
        return self._params

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

    def log(self, message: str) -> None:
        """ログのリングバッファへ追記し、WS "log" イベントを発行する."""
        self._bridge.log(message)

    def progress(self, stage: str, percent: float | None = None) -> None:
        """直近の進捗を record に保持し、WS "progress" イベントを発行する."""
        self._bridge.progress(stage, percent)

    def frame(self, image: Image) -> None:
        """プレビューのオーバーライドスロットへフレームを書き込む."""
        self._bridge.frame(image)

    def prompt(self, spec: PromptSpec) -> Answer:
        """WAITING_INPUT へ遷移し、ユーザー応答までブロックする.

        応答後 RUNNING に復帰し "prompt_resolved" + "job_status" を発行する。

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._bridge.prompt(spec)

    def next_command(self, timeout: float | None = None) -> dict[str, Any] | None:
        """WS "command" のキューから 1 件取得する（timeout 超過は None）.

        Raises:
            JobAborted: 待機中に abort された場合
        """
        return self._bridge.next_command(timeout)

    def checkpoint(self) -> None:
        """Abort 要求済みなら JobAborted を送出する（それ以外は no-op）.

        Raises:
            JobAborted: abort 要求済みの場合
        """
        self._bridge.checkpoint()
