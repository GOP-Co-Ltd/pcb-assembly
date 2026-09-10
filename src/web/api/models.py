"""API 境界で共有する pydantic モデル.

このモジュールは **pydantic と標準ライブラリのみ**に依存する（`tests/web/api/test_models.py`
が静的に回帰をピンする）。frontend を別プロセスへ分離してもそのまま import できる唯一の contract
モジュールに保つため、pcbasm / web.api の他モジュールを import しない。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# backend が `GET /api/machine-info` で自己申告する API 契約のバージョン。
# frontend / discovery 側の互換判定はこの定数を共有する（定義箇所はここだけ）
API_VERSION = 1

type JobStatusName = Literal[
    "pending", "running", "waiting_input", "succeeded", "failed", "aborted"
]

type SettingValueType = Literal[
    "float",
    "int",
    "str",
    "float_pair",
    "float_or_auto",
    "dispense_mode",
    "line_direction",
]

# bool を受け付けるフィールドは無いが、JSON の true/false が数値へ暗黙変換されず
# _coerce の「bool は受け付けません」で 400 になるよう bool を union に残す。
type MachineSettingValue = float | int | str | bool | list[float]


class Position(BaseModel):
    """ステージ座標 [mm]."""

    x: float
    y: float
    z: float


class KlipperStatus(BaseModel):
    """Klipper の接続状態とステージ状態."""

    connected: bool
    position: Position | None = None
    homed_axes: str | None = None
    error: str | None = None


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
    """/api/state 用のジョブ要約."""

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
    """/api/control/* の戻り（`StateResponse` の control / you と同じ形）."""

    control: ControlInfo
    you: ClientInfo


class StateResponse(BaseModel):
    """/api/state のアプリ状態レスポンス."""

    pcb_file: str | None
    busy: bool
    busy_owner: str | None
    focus_z: float | None
    mainsail_url: str | None
    preview_clients: int
    job: JobBrief | None
    nozzle_cap: Position | None
    # 既定値を持たせるのは、この 2 つを返さない古い backend を frontend が
    # そのまま parse できるようにするため（既定は「保持者なし」+ 空キー =
    # どの保持者とも一致しない fail-closed な値）
    control: ControlInfo = ControlInfo()
    you: ClientInfo = ClientInfo()


class SettingsField(BaseModel):
    """machine.toml のホワイトリスト項目 1 件（現在値 + 実効値）.

    ``value`` は machine.toml に**書かれている値**（未記載なら None）で、設定フォームの
    入力値になる。``resolved`` は既定値まで解決した**実効値**（装置が実際に使う値）で、
    現在値の表示に使う。両方を返すのは、未記載キーを 0 などで代替して描くと、その値が
    保存フォームに乗って machine.toml へ書き戻されるため。セクションごと欠けている /
    壊れている場合は ``resolved`` も None。
    """

    key: str
    label: str
    value_type: SettingValueType
    unit: str | None
    value: MachineSettingValue | None
    resolved: MachineSettingValue | None


class MachineSettingsResponse(BaseModel):
    """/api/settings/machine の machine.toml ホワイトリスト項目一覧."""

    fields: list[SettingsField]


class MachineInfo(BaseModel):
    """Backend が自己申告する装置情報（到達性プローブ兼用）.

    ``machine_id`` は backend ホストの hostname、``machine_name`` は machine.toml の
    表示名（未設定なら ``machine_id``）。``mainsail_url`` は backend 側で解決済みの値。
    """

    machine_id: str
    machine_name: str
    machine_type: str | None
    mainsail_url: str
    fb_start: str
    api_version: int


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


class JobSpecInfo(BaseModel):
    """ジョブ定義の公開表現（フォーム / ページ描画に必要な全量）."""

    name: str
    label: str
    tab: Literal["dev", "pasting", "posctrl"]
    params: list[ParamSpecInfo] = []
    requires_pcb: bool = False
    uses_machine: bool = True
    notify_on_completion: bool = False
    accepts_commands: bool = False
    persisted_params: list[str] = []
    runtime_params: list[str] = []
    hidden: bool = False
    provides_preview: bool = False
    loading_param: str | None = None
    loading_stages: str = "ローディング"


class JobCatalogResponse(BaseModel):
    """/api/jobs のジョブカタログレスポンス（hidden を含む全件）."""

    jobs: list[JobSpecInfo]


class UpdateRepositoryInfo(BaseModel):
    """WebUI からの更新が見る git リポジトリの現在値.

    値の意味はサーバ側で解決済み（``up_to_date`` を behind から JS で再導出しない）。
    ``error`` が入るのは ``.git`` を読めないホスト（`GET /api/update/status` は
    それでも 200 を返す）。
    """

    head: str = ""
    head_subject: str = ""
    # 表示用にサーバが組んだ 1 行（"<sha> <件名>"。読めないホストでは空）
    head_label: str = ""
    branch: str | None = None
    upstream: str | None = None
    upstream_head: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_paths: list[str] = []
    untracked_paths: list[str] = []
    up_to_date: bool = True
    error: str | None = None


class UpdateStepInfo(BaseModel):
    """更新手順 1 つの結果（``label`` はサーバが組んだ表示名）."""

    step: str = ""
    label: str = ""
    ok: bool = False
    detail: str = ""


class UpdateRunInfo(BaseModel):
    """更新 1 回分の記録（再起動を跨いで読める永続 report の公開表現）."""

    run_id: str = ""
    state: str = "idle"
    state_label: str = ""
    step: str | None = None
    steps: list[UpdateStepInfo] = []
    from_head: str | None = None
    from_subject: str | None = None
    to_head: str | None = None
    to_subject: str | None = None
    restart_units: list[str] = []
    error: str = ""
    # 失敗した手順の出力（サーバが選ぶ。JS で steps から再導出しない）
    failed_detail: str = ""
    # 更新は成功したが人手の対応が要ること（unit 定義の再 install など）
    warnings: list[str] = []
    started_at: float | None = None
    finished_at: float | None = None


class UpdateStatusResponse(BaseModel):
    """/api/update/status（backend）と /api/self-update（frontend）の共通レスポンス.

    表示文字列（``restart_notice`` / 各 ``label``）はサーバが組む。JS は受け取った値を
    そのまま描くだけにする（`webui-thin-wrapper`）。
    """

    enabled: bool = False
    hostname: str = ""
    # 1 行の要約（「最新です」「N 件の更新があります」「中断事由」）。サーバが組む
    summary: str = ""
    repository: UpdateRepositoryInfo = UpdateRepositoryInfo()
    blocker: str | None = None
    fetch_error: str | None = None
    update_available: bool = False
    restart_units: list[str] = []
    restart_notice: str = ""
    run: UpdateRunInfo = UpdateRunInfo()


class UpdateRunResponse(BaseModel):
    """更新の開始受理（202）."""

    run_id: str = ""
    run: UpdateRunInfo = UpdateRunInfo()
