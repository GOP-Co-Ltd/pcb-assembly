"""Backend の REST / WS が受け取る命令と、返して frontend が検証して描く pydantic モデル."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel

type JobStatusName = Literal[
    "pending", "running", "waiting_input", "succeeded", "failed", "aborted"
]

type SettingValueType = Literal[
    "float", "int", "str", "float_or_auto", "float_pair", "float_triple", "choice"
]

# bool を受け付けるフィールドは無いが、JSON の true/false が数値へ暗黙変換されず
# 型の合わない値として 400 になるよう bool を union に残す。
type MachineSettingValue = float | int | str | bool | list[float]


class Position(BaseModel):
    """ステージ座標 [mm]."""

    x: float
    y: float
    z: float


class Point2dInfo(BaseModel):
    """平面座標 [mm]."""

    x: float
    y: float


class RectInfo(BaseModel):
    """軸平行矩形 [mm]（``(x, y)`` は左上の角）."""

    x: float
    y: float
    width: float
    height: float


class NozzleCleanInfo(BaseModel):
    """記録されたノズルクリーニング面の位置と、その表示用文字列.

    ``label`` はサーバーが組んで返す（JS に整形させない）。押し込み後の高さは設定値から
    決まるので運ばない（表示するのは記録した面の位置だけ）。
    """

    x: float
    y: float
    z: float
    label: str


class KlipperStatus(BaseModel):
    """Klipper の接続状態とステージ状態."""

    connected: bool
    position: Position | None = None
    homed_axes: str | None = None
    error: str | None = None


type Axis = Literal["x", "y", "z"]


class HomeCommand(BaseModel):
    type: Literal["home"]
    axes: list[Axis] = []


class JogCommand(BaseModel):
    type: Literal["jog"]
    axis: Axis
    distance: float


class MoveCommand(BaseModel):
    type: Literal["move"]
    x: float | None = None
    y: float | None = None
    z: float | None = None


class RelaxCommand(BaseModel):
    type: Literal["relax"]


class FocusZCommand(BaseModel):
    type: Literal["focus_z"]


class MoveToCapCommand(BaseModel):
    type: Literal["move_to_cap"]


class GCodeCommand(BaseModel):
    type: Literal["gcode"]
    gcode: str = Field(pattern=r"\S")  # 空白だけの G-code は送らない


class MachineCommandRequest(
    RootModel[
        Annotated[
            HomeCommand
            | JogCommand
            | MoveCommand
            | RelaxCommand
            | FocusZCommand
            | MoveToCapCommand
            | GCodeCommand,
            Field(discriminator="type"),
        ]
    ]
):
    """マシン操作パネルの命令（REST の /api/machine/command と、対話ジョブの WS command が同じ形で送る）."""


class PromptInfo(BaseModel):
    """応答待ちプロンプトの公開表現（REST / WS 共用）."""

    id: str
    kind: Literal["confirm", "number", "text", "choice"]
    message: str
    default: bool | float | str | None = None
    true_label: str | None = None
    false_label: str | None = None
    choices: list[str] = []


class ArtifactInfo(BaseModel):
    """ジョブ成果物の公開表現."""

    label: str
    url: str  # "/artifacts/<path>"
    kind: Literal["image", "file"]


class ApplyInfo(BaseModel):
    """設定反映ペイロードの公開表現."""

    label: str
    values: dict[str, bool | float | int | str]


class JobResultInfo(BaseModel):
    """ジョブ結果の公開表現."""

    summary: str | None = None
    artifacts: list[ArtifactInfo] = []
    apply: ApplyInfo | None = None


class JobSummary(BaseModel):
    """ジョブ状態の全量サマリ（REST / WS "job_status" 共用）."""

    id: str
    name: str
    label: str
    status: JobStatusName
    params: dict[str, bool | float | int | str]
    error: str | None = None
    progress_stage: str | None = None
    progress_percent: float | None = None
    log_tail: list[str] = []  # リングバッファ全量（最大 log_capacity 行）
    pending_prompt: PromptInfo | None = None
    result: JobResultInfo | None = None
    accepts_commands: bool = False
    notify_on_completion: bool = False
    apply_available: bool = False


class JobBrief(BaseModel):
    """/api/session/state 用のジョブ要約."""

    id: str
    name: str
    status: str


class ControlInfo(BaseModel):
    """操作権リースの公開スナップショット（生の session_id は含まない）.

    誰も保持していないときも ``held: false`` の形で返す（null にしないのは、
    frontend の分岐を減らすため）。「自分が保持者か」は ``key`` を
    ``ClientInfo.key`` と比べて判定する。
    """

    key: str | None = None
    display_name: str | None = None
    held: bool = False
    connections: int = 0


class ClientInfo(BaseModel):
    """リクエスト元クライアント自身の公開キー（`ControlInfo.key` との比較用）."""

    key: str = ""


class ControlStateResponse(BaseModel):
    """/api/session/control/* の戻り（`StateResponse` の control / you と同じ形）."""

    control: ControlInfo
    you: ClientInfo


class StateResponse(BaseModel):
    """/api/session/state のアプリ状態レスポンス."""

    pcb_file: str | None
    busy: bool
    busy_owner: str | None
    focus_z: float | None
    mainsail_url: str | None
    preview_clients: int
    job: JobBrief | None
    nozzle_cap: Position | None
    control: ControlInfo
    you: ClientInfo
    nozzle_clean: NozzleCleanInfo | None
    # machine.toml が読めない・検証に通らないときの理由（degraded。直せば次の取得で消える）
    config_error: str | None
    # 起動時に machine.toml を移行したときの知らせ（次の再起動まで出す）
    config_notice: str | None
    # 起動時に選択していた PCB が無く、選択を解除したときの知らせ（選び直すまで出す）
    pcb_notice: str | None


class SettingChoice(BaseModel):
    """選択式の設定項目の 1 選択肢."""

    value: str
    label: str


class SettingsField(BaseModel):
    """machine.toml の設定項目 1 件（表示の定義 + 現在値 + 実効値）.

    表示の定義（見出し・表示名・単位・選択肢・下限・空欄の扱い）は backend が設定のスキーマから組む。
    ``section`` が設定ページの 1 画面、``group`` がその中のカードで、項目は表示する順に並ぶ。

    ``value`` は machine.toml に書かれている値（未記載なら None）で、設定フォームの
    入力値になる。``resolved`` は既定値まで解決した実効値（装置が実際に使う値）で、
    現在値の表示に使う。両方を返すのは、未記載キーを 0 などで代替して描くと、その値が
    保存フォームに乗って machine.toml へ書き戻されるため。セクションごと欠けている /
    壊れている場合は ``resolved`` も None。
    """

    key: str
    section: str
    section_label: str
    group: str
    label: str
    value_type: SettingValueType
    unit: str | None
    choices: list[SettingChoice]
    minimum: float | None
    clearable: bool
    value: MachineSettingValue | None
    resolved: MachineSettingValue | None


class MachineSettingsResponse(BaseModel):
    """/api/machine_config の machine.toml 設定項目一覧（表示する順）."""

    fields: list[SettingsField]


class ConfigHistoryEntry(BaseModel):
    """machine.toml の 1 世代（書き換える直前の内容）.

    ``changes`` はこの世代の次に入った変更で、「キー: 前 → 後」の文字列をサーバーが組む。
    """

    name: str
    time_label: str
    changes: list[str]


class ConfigHistoryResponse(BaseModel):
    """/api/backup/history の世代一覧（新しい順）."""

    entries: list[ConfigHistoryEntry]


class MachineInfo(BaseModel):
    """Backend が自己申告する装置情報（到達性プローブ兼用）.

    ``machine_id`` は backend の自己申告 ID（既定は hostname）で、これがそのまま
    機体名になる（machine.toml に表示名の設定は無い）。``mainsail_url`` は backend 側で
    解決済みの値。
    """

    machine_id: str
    mainsail_url: str
    fb_start: str
    api_version: int


class ParamGroupInfo(BaseModel):
    """ジョブフォーム内のまとまり（``advanced`` は普段は触らない項目のまとまり）."""

    label: str
    advanced: bool = False


class ParamSpecInfo(BaseModel):
    """ジョブパラメータ定義の公開表現（``default`` は保存済み既定値を反映済み）."""

    name: str
    label: str
    value_type: Literal["float", "int", "str", "bool", "choice"]
    default: bool | float | int | str | None = None
    choices: list[str] = []
    unit: str | None = None
    help: str | None = None
    runtime_editable: bool = False
    minimum: float | None = None
    optional: bool = False
    group: ParamGroupInfo | None = None


class LoadingRotationInfo(BaseModel):
    """ローディング欄の回転押出の既定値（保存済み既定値を反映済み）."""

    rotations: float
    rate: float
    accel: float
    retract_rotations: float


class LoadingPanelInfo(BaseModel):
    """ジョブ画面のローディング欄.

    ``amount`` と ``rotation`` は入力欄の既定値（保存済み既定値を反映済み）、``stages`` はボタンを押せる progress の段階名。
    """

    amount: float
    rotation: LoadingRotationInfo | None = None
    stages: list[str]


class JobSpecInfo(BaseModel):
    """ジョブ定義の公開表現（フォーム / ページ描画に必要な全量）."""

    name: str
    label: str
    params: list[ParamSpecInfo] = []
    requires_pcb: bool = False
    uses_machine: bool = True
    notify_on_completion: bool = False
    accepts_commands: bool = False
    persisted_params: list[str] = []
    runtime_params: list[str] = []
    provides_preview: bool = False
    loading: LoadingPanelInfo | None = None


class JobCatalogResponse(BaseModel):
    """/api/jobs のジョブカタログレスポンス（登録済みの全件）."""

    jobs: list[JobSpecInfo]


class FirmwareRestartResponse(BaseModel):
    """ファームウェア再起動の結果（WebUI サービスの再起動予約を含む）.

    表示文字列はサーバが組む。Klipper へ送れなければ 502 を投げるので、ここに
    「失敗」は入らない。``warning`` が入るのはファームウェアは再起動できたが WebUI
    サービスを再起動できなかったとき（sudoers 未設置・更新の実行中）。
    """

    # トーストに出す 1 行（何を再起動したか）
    message: str = ""
    # サービスを再起動できなかった理由（空なら予約済み）
    warning: str = ""
