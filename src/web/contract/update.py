"""自己更新の状態と実行の応答.

backend の `/api/update/*` と frontend の `/api/self-update/**` が共通で使う。
欄の追加も削除も更新経路の契約を変える（`tests/repo/test_update_contract.py`）。
"""

from __future__ import annotations

from pydantic import BaseModel


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
