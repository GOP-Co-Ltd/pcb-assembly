"""API 境界で共有する pydantic モデル."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

type JobStatusName = Literal[
    "pending", "running", "waiting_input", "succeeded", "failed", "aborted"
]


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
    apply_available: bool = False


class JobBrief(BaseModel):
    """/api/state 用のジョブ要約."""

    id: str
    name: str
    status: str
