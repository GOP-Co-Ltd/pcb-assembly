"""SSR ページのルーター（backend から取った値でテンプレートを描く）.

装置の状態は一切持たず、`MachineClient` で backend から取得した pydantic モデル
（`StateResponse` / `MachineInfo` / `ReadinessResponse` / `MachineSettingsResponse` / `JobSpecInfo`）だけを
テンプレートへ渡す。タブ・グループ・テンプレートは `web.ui.features` の機能表にある。
backend 取得が 1 本でも失敗したページは 503 ページになる（`BackendUnavailable`）。

機能のページを足すときは、次の箇所を変更する。

- `web.ui.features` の ``FEATURES`` に 1 行足す。ジョブでないページは表示名（``label``）も書く。
  ジョブのページの定義は backend の ``GET /api/jobs`` から取り、無ければ 503
- ページ固有の値は ``_FEATURE_CONTEXT`` / ``_JOB_FEATURE_CONTEXT`` の provider で足す
- machine.toml の値が要るなら ``_MACHINE_SETTINGS_FEATURES`` にも足す（ここに無い feature では取得しない）

URL 空間は次のとおり。

- ``/m/{machine_id}/{feature}`` が実体。``/m/{machine_id}`` は着地点（paste_solder）へ 307 する
- ``/`` は frontend 自身のホスト名と同じ machine_id の機体（同居機）の着地点へ 307 する。
  その機体が無ければ機体一覧 ``/machines`` へ 307 する。machine を指定しないほかの URL も同じ規則で自機の URL へ飛ぶ
- 旧 URL（``/m/{machine_id}/{旧タブ}[/{旧 slug}]``、``/m/{machine_id}/settings``、``/bulk``）は新しい URL へ 307 する
- API 版のずれた機体（`MachineEndpoint.update_only`）の ``/m/{machine_id}/…`` は、どの URL も更新専用ページになる
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from web.contract.identity import SESSION_COOKIE
from web.contract.models import (
    JobCatalogResponse,
    JobSpecInfo,
    MachineInfo,
    ReadinessResponse,
    SettingsField,
    StateResponse,
)
from web.ui.features import (
    FEATURES_BY_SLUG,
    LANDING,
    LEGACY,
    LEGACY_TABS,
    TABS,
    Feature,
    tab_features,
)
from web.ui.layout import param_sections, settings_sections
from web.ui.machine_client import BackendGateway, BackendUnavailable, MachineClient
from web.ui.machines import MachineEndpoint, MachineRegistry

# 機体一覧（マシン非依存のページ。自機の無い frontend の `/` もここへ飛ぶ）
MACHINES_PATH = "/machines"

# machine.toml の現在値（`GET /api/machine_config`）が要るページの feature。
# ここに無い feature では取得しない。backend の `/api/machine_config` は
# machine.toml をパースするので、壊れた machine.toml では 500 になる。全ページで
# 取得すると、壊れた設定ファイル 1 つで全画面が 503 になり、設定を直す画面すら開けない
# （「壊れていても描けるページは描く」を frontend でも保つ）
_MACHINE_SETTINGS_FEATURES = frozenset(
    {"machine_config", "paste_solder", "loading", "copper_detection", "nozzle_cap"}
)

router = APIRouter()


class UpdateOnlyMachine(Exception):
    """API 版のずれた機体のページを開こうとした（→ 更新専用ページ）."""

    def __init__(self, endpoint: MachineEndpoint) -> None:
        """例外を初期化する.

        Args:
            endpoint: 更新専用の機体
        """
        super().__init__(f"{endpoint.label} は UI サーバーと版が違います")
        # 更新専用ページの宛先に使うため public
        self.endpoint = endpoint


def _registry(request: Request) -> MachineRegistry:
    return request.app.state.registry


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _resolve(
    request: Request, machine_id: str
) -> tuple[MachineEndpoint, MachineClient]:
    """machine_id を解決し、その backend のクライアントを作る.

    Raises:
        UnknownMachine: 未登録の machine_id（→ 404 ページ）
        UpdateOnlyMachine: 版のずれた機体（→ 更新専用ページ。backend の応答を描けない）
    """
    endpoint = _registry(request).resolve(machine_id)
    if endpoint.update_only:
        raise UpdateOnlyMachine(endpoint)
    gateway: BackendGateway = request.app.state.gateway
    return endpoint, MachineClient(endpoint, gateway)


class _MachineSettings:
    """ページが読む machine 設定（backend の `/api/machine_config` の結果）.

    設定フォームは machine.toml に書かれている値（``fields`` の ``value``）を、
    現在値の表示は実効値（``resolved``）を使う。実効値の解決は backend にしかできない
    （frontend が既定値を持つとプロセス境界の両側で二重管理になる）。
    """

    def __init__(
        self, endpoint: MachineEndpoint, fields: Sequence[SettingsField]
    ) -> None:
        """設定を保持する.

        Args:
            endpoint: 取得元の backend（解決できない値を 503 にするため持つ）
            fields: `/api/machine_config` の項目（取得しないページでは空）
        """
        self._endpoint = endpoint
        self.fields = tuple(fields)

    def number(self, key: str) -> float:
        """実効値を数値として読む.

        Raises:
            BackendUnavailable: backend が実効値を解決できていない場合。0 などで
                代替すると、その値が「設定に保存」で machine.toml へ書き戻され、
                装置が誤動作する（canny 閾値 0 で銅箔をまったく検出できなくなる等）
        """
        value = next(
            (field.resolved for field in self.fields if field.key == key), None
        )
        if isinstance(value, bool) or not isinstance(value, int | float):
            cause = LookupError(f"backend が {key} の現在値を解決できません: {value!r}")
            raise BackendUnavailable(self._endpoint, cause) from cause
        return float(value)

    def select(self, *keys: str) -> list[SettingsField]:
        """``keys`` の項目をその順に返す（機能ページの即保存フォーム用）."""
        by_key = {field.key: field for field in self.fields}
        return [by_key[key] for key in keys]


async def _machine_settings(
    endpoint: MachineEndpoint, client: MachineClient, needed: bool
) -> _MachineSettings:
    """必要なページでだけ machine 設定を取得する（他は空 = 取得しない）."""
    if not needed:
        return _MachineSettings(endpoint, ())
    return _MachineSettings(endpoint, (await client.machine_settings()).fields)


def _feature_label(jobs: Mapping[str, JobSpecInfo], feature: Feature) -> str:
    job = jobs.get(feature.slug)
    if job is not None:
        return job.label
    return feature.label or feature.slug


def _jobs_by_name(catalog: JobCatalogResponse) -> dict[str, JobSpecInfo]:
    return {job.name: job for job in catalog.jobs}


def _job_spec(
    endpoint: MachineEndpoint, jobs: Mapping[str, JobSpecInfo], feature: str
) -> JobSpecInfo:
    """ジョブページのジョブ定義.

    Raises:
        BackendUnavailable: backend がそのジョブを申告していない場合（frontend と
            backend の版がずれた状態。欠損したフォームを描くより 503 を出す）
    """
    job = jobs.get(feature)
    if job is None:
        cause = LookupError(f"backend が {feature} ジョブを申告していません")
        raise BackendUnavailable(endpoint, cause) from cause
    return job


def _suffix_of(path: str) -> str:
    """URL から ``/m/{machine_id}`` を除いた残り（マシン切替の遷移先に使う）."""
    if path.startswith("/m/"):
        _, _, suffix = path.removeprefix("/m/").partition("/")
    else:
        suffix = path.lstrip("/")
    return suffix or LANDING


def _update_notice_url(machine_id: str | None) -> str:
    """更新通知エンドポイントの URL（machine prefix を付けない frontend 自身の入口）."""
    if machine_id is None:
        return "/api/update-notice"
    return f"/api/update-notice?{urlencode({'machine_id': machine_id})}"


def _nav_tabs(base: str) -> list[tuple[str, str, str]]:
    """トップバーのタブ（slug、表示名、リンク先 = タブの先頭の機能）."""
    return [
        (tab, label, f"{base}/{tab_features(tab)[0].slug}")
        for tab, label in TABS.items()
    ]


def _chrome_context(
    request: Request, *, machine_id: str | None, current_suffix: str
) -> dict[str, Any]:
    """Backend に依存しない共通コンテキスト（エラーページもこれだけで描ける）."""
    base = f"/m/{machine_id}" if machine_id else ""
    return {
        "request": request,
        # マシン非依存のページでは空文字（機体のコマンドとタブを出さない）
        "base": base,
        "machine_id": machine_id,
        # トップバーの更新通知の問い合わせ先。マシン非依存のページでは UI サーバー
        # 自身の更新だけを確認する（機体が決まっていないので問い合わせ先が無い）
        "update_notice_url": _update_notice_url(machine_id),
        "machines": _registry(request).list(),
        "current_suffix": current_suffix,
        "tabs": _nav_tabs(base) if machine_id else [],
        "active_tab": None,
        "active_feature": None,
        # 更新専用ページでは、中継されない機体コマンド（操作権・PCB・再起動）を出さない
        "update_only": False,
        "config_error": None,
        "config_notice": None,
        "pcb_notice": None,
        # 機体のページだけが持つ（トップバーの立ち上げチップ）
        "readiness": None,
    }


def _base_context(
    request: Request,
    machine_id: str,
    current_suffix: str,
    info: MachineInfo,
    state: StateResponse,
    readiness: ReadinessResponse,
) -> dict[str, Any]:
    """全ページ共通のコンテキスト（backend の自己申告と現在状態から組む）."""
    context = _chrome_context(
        request, machine_id=machine_id, current_suffix=current_suffix
    )
    context.update(
        selected_pcb=state.pcb_file,
        fb_start=info.fb_start,
        # mainsail_url は backend が解決した値（リクエストのホスト名から導出すると
        # frontend 機を指してしまう）
        mainsail_url=info.mainsail_url,
        focus_z=state.focus_z,
        config_error=state.config_error,
        config_notice=state.config_notice,
        pcb_notice=state.pcb_notice,
        readiness=readiness,
    )
    return context


def _sidebar_context(
    feature: Feature, jobs: Mapping[str, JobSpecInfo]
) -> dict[str, Any]:
    """機能のページのタブとサイドバー（同じタブの機能を、グループごとに表の順で並べる）."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for item in tab_features(feature.tab):
        groups.setdefault(item.group, []).append(
            (item.slug, _feature_label(jobs, item))
        )
    return {
        "active_tab": feature.tab,
        "tab_label": TABS[feature.tab],
        "sidebar_groups": list(groups.items()),
        "active_feature": feature.slug,
        "feature_label": _feature_label(jobs, feature),
    }


def _html_page(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """HTML ページを描き、未発行ならセッション cookie を発行する.

    発行するのは HTML ページ応答だけ（プロキシ配下の JSON / MJPEG / 静的アセットでは
    発行しない）。この cookie で操作権リースのセッションを同定し、`ProxyApp` が backend
    向けヘッダへ変換する。認証ではなく自己申告で、LAN 上の誰でも他人になりすませる。

    `Path` は既定の ``/`` のまま上書きしない。`/m/{id}/api/**` と WS ハンドシェイクと
    `img.src`（MJPEG）に cookie が付くことが、「ブラウザは独自ヘッダを付けられない」
    制約を回避する唯一の手段になっている。
    """
    response = _templates(request).TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
        headers=dict(headers) if headers else None,
    )
    if SESSION_COOKIE not in request.cookies:
        # 毎回発行するとページ遷移ごとに別のセッションになり、操作権を失う
        response.set_cookie(SESSION_COOKIE, secrets.token_urlsafe(16), httponly=True)
    return response


def render_standalone(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    current_suffix: str,
) -> HTMLResponse:
    """マシン非依存のページを chrome 付きで描く（`/update` と `/machines`）.

    `_chrome_context` + `_html_page` の公開ラッパ。backend へ 1 度も問い合わせないので
    機体が 1 台も無いホストでも描ける（frontend 専用機の更新ページがこれにあたる）。

    Args:
        request: 現在のリクエスト
        name: テンプレート名
        context: ページ固有のコンテキスト（chrome の値へ上書きで重ねる）
        current_suffix: マシン切替時の遷移先に使う URL 断片

    Returns:
        描画結果（未発行ならセッション cookie も発行する）
    """
    full = _chrome_context(request, machine_id=None, current_suffix=current_suffix)
    full.update(context)
    return _html_page(request, name, full)


def render_message(
    request: Request,
    *,
    title: str,
    detail: str,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    machine_id: str | None = None,
) -> HTMLResponse:
    """マシンに依存しない案内ページを描く（ピッカー・案内・エラー共通）.

    backend への到達を要さないコンテキストだけで描くので、backend が停止していても
    マシン切替ドロップダウン付きのページを返せる（登録一覧は frontend が持つ）。
    """
    context = _chrome_context(
        request,
        machine_id=machine_id,
        current_suffix=_suffix_of(request.url.path),
    )
    context.update(title=title, detail=detail)
    return _html_page(
        request,
        "message.html",
        context,
        status_code=status_code,
        headers=headers,
    )


def render_update_only(request: Request, endpoint: MachineEndpoint) -> HTMLResponse:
    """版のずれた機体の更新専用ページを描く（``/m/{machine_id}/…`` のどの URL でも）.

    proxy がこの機体へ中継するのは更新と緊急停止だけなので、backend の値を使わずに描く。
    """
    context = _chrome_context(
        request,
        machine_id=endpoint.machine_id,
        current_suffix=_suffix_of(request.url.path),
    )
    # どのタブも同じこのページになるので、作業分野のタブは並べない（機体一覧だけ残す）
    context.update(title=endpoint.label, update_only=True, tabs=[])
    return _html_page(request, "update_only.html", context)


def own_machine(request: Request) -> MachineEndpoint | None:
    """Frontend 自身のホスト名と同じ machine_id の機体（同居機。無ければ None）."""
    hostname = request.app.state.settings.hostname
    return next(
        (m for m in _registry(request).list() if m.machine_id == hostname), None
    )


def _open_own(request: Request, suffix: str) -> RedirectResponse:
    """マシン未指定の URL を自機の URL へ飛ばす（自機が無ければ機体一覧）."""
    own = own_machine(request)
    if own is None:
        return RedirectResponse(url=MACHINES_PATH, status_code=307)
    return RedirectResponse(url=f"/m/{own.machine_id}/{suffix}", status_code=307)


def _legacy_redirect(machine_id: str, old: str) -> RedirectResponse:
    """旧 URL の最後の段を新しい URL へ 307 する.

    Raises:
        HTTPException: どの機能にも当たらない段（404）
    """
    target = LEGACY.get(old, old)
    if target.startswith("/"):
        return RedirectResponse(url=target, status_code=307)
    if target not in FEATURES_BY_SLUG:
        raise HTTPException(status_code=404, detail=f"未知の機能です: {old}")
    return RedirectResponse(url=f"/m/{machine_id}/{target}", status_code=307)


@router.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    return _open_own(request, LANDING)


@router.get(MACHINES_PATH, response_class=HTMLResponse)
async def machines_page(request: Request) -> HTMLResponse:
    """機体一覧（登録済みの全マシンを登録順に並べる）.

    SSR は chrome の ``machines``（``MachineRegistry.list()``）だけで描き、backend へは問い合わせない。
    行ごとの操作権・更新・PCB の状態は ``machines.js`` が各機体の ``/m/{machine_id}/api/**`` から取る。
    こうすると 1 台の backend が落ちていてもページ全体は 503 にならず、他の機体の行は使える。
    """
    return render_standalone(
        request,
        "machines.html",
        {"active_tab": "machines", "title": "機体一覧"},
        current_suffix=LANDING,
    )


# 旧 URL（一括管理）。マシン非依存の入口の規則より先に登録する
@router.get("/bulk", include_in_schema=False)
async def bulk_entry() -> RedirectResponse:
    return RedirectResponse(url=MACHINES_PATH, status_code=307)


@router.get("/m/{machine_id}", include_in_schema=False)
async def machine_entry(machine_id: str) -> RedirectResponse:
    return RedirectResponse(url=f"/m/{machine_id}/{LANDING}", status_code=307)


def _loading_context(job: JobSpecInfo, settings: _MachineSettings) -> dict[str, Any]:
    """Loading ページ専用コンテキスト（開始位置・現在設定値）."""
    return {
        "loading_control_specs": tuple(
            spec for spec in job.params if not spec.optional
        ),
        "loading_position_specs": tuple(spec for spec in job.params if spec.optional),
        "solder_paste_density": settings.number("solder.paste_density"),
        "current_rotations_per_ul": settings.number(
            "solder.dispenser.rotations_per_ul"
        ),
        "current_max_dispense_rate": settings.number(
            "solder.dispenser.max_dispense_rate"
        ),
        "current_dispense_accel": settings.number("solder.dispenser.dispense_accel"),
    }


def _paste_workspace_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """Pad editor 付き塗布ページのコンテキスト（machine 設定の即保存フォーム）."""
    return {
        "auto_threshold_fields": settings.select(
            "solder.auto.line_aspect_ratio",
            "solder.auto.area_short_side_factor",
        ),
        "pad_refinement_fields": settings.select("alignment.refine_max_short_side"),
        "flow_calibration_fields": settings.select(
            "solder.volume_correction.calibration_file",
            "solder.volume_correction.amount_ul",
            "solder.volume_correction.crop_size_mm",
            "solder.volume_correction.settle_seconds",
        ),
    }


def _machine_config_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """設定ページのコンテキスト（backend が返した順の項目を画面とカードへまとめる）."""
    return {"settings_sections": settings_sections(settings.fields)}


def _copper_detection_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """Copper_detection ページ専用コンテキスト（エッジ検出パラメータ現在値）."""
    return {
        "canny_low": settings.number("alignment.canny_low"),
        "canny_high": settings.number("alignment.canny_high"),
        "blur_ksize": int(settings.number("alignment.blur_ksize")),
    }


def _nozzle_cap_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """ノズル位置ページ専用コンテキスト（キャップ / クリーニングの記録値と動作設定）.

    表示文字列はどちらも backend が組んだものをそのまま渡す。設定フォームは
    ``settings.number()`` を使わない（未記載で 503 になり、まだ教示していない機体で
    ページが開けなくなる）。
    """
    return {
        "nozzle_cap": state.nozzle_cap,
        "nozzle_clean": state.nozzle_clean,
        "nozzle_clean_fields": settings.select(
            "solder.nozzle.clean.press_depth",
            "solder.nozzle.clean.purge_ul",
            "solder.nozzle.clean.stroke",
            "solder.nozzle.clean.passes",
            "solder.nozzle.clean.wipe_speed",
        ),
    }


# feature slug → ジョブページ専用コンテキスト（ジョブ定義依存）
_JOB_FEATURE_CONTEXT: dict[
    str, Callable[[JobSpecInfo, _MachineSettings], dict[str, Any]]
] = {
    "loading": _loading_context,
}

# feature slug → ページ専用コンテキスト（ジョブ有無に依らない）
_FEATURE_CONTEXT: dict[
    str, Callable[[StateResponse, _MachineSettings], dict[str, Any]]
] = {
    "machine_config": _machine_config_context,
    "paste_solder": _paste_workspace_context,
    "copper_detection": _copper_detection_context,
    "nozzle_cap": _nozzle_cap_context,
}


@router.get("/m/{machine_id}/{slug}", response_class=HTMLResponse)
async def feature_page(machine_id: str, slug: str, request: Request) -> Response:
    feature = FEATURES_BY_SLUG.get(slug)
    if feature is None:
        return _legacy_redirect(machine_id, slug)
    endpoint, client = _resolve(request, machine_id)
    info, state, readiness, catalog, machine_settings = await asyncio.gather(
        client.machine_info(),
        client.state(),
        client.readiness(),
        client.jobs(),
        _machine_settings(endpoint, client, slug in _MACHINE_SETTINGS_FEATURES),
    )
    jobs = _jobs_by_name(catalog)
    context = _base_context(request, machine_id, slug, info, state, readiness)
    context.update(_sidebar_context(feature, jobs))
    if feature.is_job:
        job = _job_spec(endpoint, jobs, slug)
        # preview ペインとローディング欄の有無は backend 側で決まる事実（フレーム
        # 提供の有無・ボタンを押せる段階）なので、backend の自己申告をそのまま渡す
        context.update(
            job_name=job.name,
            param_sections=param_sections(job.params),
            persists_params=bool(job.persisted_params),
            show_preview=job.provides_preview,
            loading=job.loading,
        )
        if (job_provider := _JOB_FEATURE_CONTEXT.get(slug)) is not None:
            context.update(job_provider(job, machine_settings))
    if (provider := _FEATURE_CONTEXT.get(slug)) is not None:
        context.update(provider(state, machine_settings))
    return _html_page(request, feature.template, context)


@router.get("/m/{machine_id}/{tab}/{slug}", include_in_schema=False)
async def legacy_feature(machine_id: str, tab: str, slug: str) -> RedirectResponse:
    """旧 URL ``/m/{machine_id}/{旧タブ}/{旧 slug}``（ブックマーク）."""
    if tab not in LEGACY_TABS:
        raise HTTPException(status_code=404, detail=f"未知のページです: {tab}/{slug}")
    return _legacy_redirect(machine_id, slug)


# machine を指定しない URL。キャッチオールなので最後に登録する
@router.get("/{suffix:path}", include_in_schema=False)
async def unprefixed_entry(suffix: str, request: Request) -> RedirectResponse:
    # /m/ の下でどの route にも当たらない URL は、自機の prefix を足すと同じ形に戻って 307 が循環する
    if suffix.startswith("m/"):
        raise HTTPException(status_code=404, detail=f"未知のページです: /{suffix}")
    return _open_own(request, suffix)
