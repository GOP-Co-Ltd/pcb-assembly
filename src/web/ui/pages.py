"""SSR ページのルーター（backend から取った値でテンプレートを描く）.

`web.api.routers.pages` の移設。装置の状態は一切持たず、`MachineClient` で backend
から取得した pydantic モデル（`StateResponse` / `MachineInfo` /
`MachineSettingsResponse` / `JobSpecInfo`）だけを見てテンプレートへ渡す。表示知識は
`web.ui.layout` にある。

URL 空間:

- ``/`` と ``/{tab}[/{feature}]`` / ``/settings`` は machine を指定しない入口。既知
  マシンが 1 台なら ``/m/{machine_id}/…`` へ 307、複数ならピッカー、0 台なら案内を出す
- ``/m/{machine_id}`` 単体も入口で、既定タブへ 307 する
- ``/m/{machine_id}/…`` が実体。``/m/{machine_id}/settings`` は ``/{tab}`` より**先に**
  登録する（後だと tab として食われる）
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from web.api.models import (
    JobCatalogResponse,
    JobSpecInfo,
    MachineInfo,
    SettingsField,
    StateResponse,
)
from web.ui.layout import (
    CLEARABLE_MACHINE_KEYS,
    DISPENSE_CALIBRATION_PARAM_GROUPS,
    FEATURE_LABELS,
    FEATURE_TEMPLATES,
    JOB_TEMPLATES,
    LOADING_ROTATION_PARAMS,
    NOZZLE_CLEAN_SETTING_KEYS,
    PASTE_AUTO_THRESHOLD_KEYS,
    PASTE_FLOW_CALIBRATION_FILE_KEY,
    PASTE_FLOW_CALIBRATION_KEYS,
    PASTE_PAD_REFINEMENT_KEYS,
    PASTE_VOLUME_CALIBRATION_PARAM_GROUPS,
    POSITIVE_ONLY_MACHINE_KEYS,
    TAB_LABELS,
    TAB_PHASES,
    TABS,
    grouped_fields,
)
from web.ui.machine_client import BackendGateway, BackendUnavailable, MachineClient
from web.ui.machines import MachineEndpoint, MachineRegistry
from web.ui.proxy import SESSION_COOKIE

# machine を指定しない URL で開くタブ
DEFAULT_TAB = "posctrl"

# machine.toml の現在値（`GET /api/settings/machine`）を要るページの feature。
# ここに無い feature では取得しない。backend の `/api/settings/machine` は
# machine.toml をパースするので、壊れた machine.toml では 500 になる。全ページで
# 取ると壊れた設定ファイル 1 つで全画面が 503 になり、設定を直す画面すら開けない
# （MR2 で backend 側に入れた「壊れていても描けるページは描く」防御を保つ）
_MACHINE_SETTINGS_FEATURES = frozenset(
    {"paste_solder", "loading", "copper_detection", "nozzle_cap"}
)

router = APIRouter()


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
    """
    endpoint = _registry(request).resolve(machine_id)
    gateway: BackendGateway = request.app.state.gateway
    return endpoint, MachineClient(endpoint, gateway)


class _MachineSettings:
    """ページが読む machine 設定（backend の `/api/settings/machine` の結果）.

    設定フォームは machine.toml に**書かれている値**（``fields`` の ``value``）を、
    現在値の表示は**実効値**（``resolved``）を使う。実効値の解決は backend にしかできない
    （frontend が既定値を持つとプロセス境界の両側で二重管理になる）。
    """

    def __init__(
        self, endpoint: MachineEndpoint, fields: Sequence[SettingsField]
    ) -> None:
        """設定を保持する.

        Args:
            endpoint: 取得元の backend（解決できない値を 503 にするため持つ）
            fields: `/api/settings/machine` の項目（取得しないページでは空）
        """
        self._endpoint = endpoint
        self.fields = tuple(fields)

    def number(self, key: str) -> float:
        """実効値を数値として読む.

        Raises:
            BackendUnavailable: backend が実効値を解決できていない場合。0 などで
                代替すると、その値が「設定に保存」で machine.toml へ書き戻されて
                装置の挙動を壊す（canny 閾値 0 で銅箔検出が全滅する等）
        """
        value = next(
            (field.resolved for field in self.fields if field.key == key), None
        )
        if isinstance(value, bool) or not isinstance(value, int | float):
            cause = LookupError(f"backend が {key} の現在値を解決できません: {value!r}")
            raise BackendUnavailable(self._endpoint, cause) from cause
        return float(value)


async def _machine_settings(
    endpoint: MachineEndpoint, client: MachineClient, needed: bool
) -> _MachineSettings:
    """必要なページだけ machine 設定を取る（他は空 = 取得しない）."""
    if not needed:
        return _MachineSettings(endpoint, ())
    return _MachineSettings(endpoint, (await client.machine_settings()).fields)


def _feature_label(jobs: Mapping[str, JobSpecInfo], slug: str) -> str:
    job = jobs.get(slug)
    if job is not None:
        return job.label
    return FEATURE_LABELS.get(slug, slug.replace("_", " ").title())


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
    return suffix or DEFAULT_TAB


def _chrome_context(
    request: Request, *, machine_id: str | None, current_suffix: str
) -> dict[str, Any]:
    """Backend に依存しない共通コンテキスト（エラーページもこれだけで描ける）."""
    return {
        "request": request,
        # 未 prefix（マシン非依存ページ）では空文字。テンプレートの href は
        # "{{ base }}/posctrl" なので未 prefix URL に落ちる
        "base": f"/m/{machine_id}" if machine_id else "",
        "machine_id": machine_id,
        "machines": _registry(request).list(),
        "current_suffix": current_suffix,
        "tabs": list(TABS),
        "tab_labels": TAB_LABELS,
        "active_tab": None,
        "active_feature": None,
    }


def _base_context(
    request: Request,
    machine_id: str,
    current_suffix: str,
    info: MachineInfo,
    state: StateResponse,
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
        machine_type=info.machine_type,
    )
    return context


def _tab_context(tab: str, jobs: Mapping[str, JobSpecInfo]) -> dict[str, Any]:
    """タブ共通のコンテキスト（サイドバー描画用）."""
    return {
        "active_tab": tab,
        "features": TABS[tab],
        "feature_labels": {slug: _feature_label(jobs, slug) for slug in TABS[tab]},
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

    発行するのは **HTML ページ応答だけ**（プロキシ配下の JSON / MJPEG / 静的アセットでは
    発行しない）。この cookie が操作権リースのセッション同定で、`ProxyApp` が backend
    向けヘッダへ翻訳する。**認証ではなく自己申告**で、LAN 上の誰でも他人を騙れる。

    `Path` は既定の ``/`` のまま上書きしない。`/m/{id}/api/**` と WS ハンドシェイクと
    `img.src`（MJPEG）に cookie が乗ることが、「ブラウザは独自ヘッダを付けられない」
    制約の唯一の抜け道になっている。
    """
    response = _templates(request).TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
        headers=dict(headers) if headers else None,
    )
    if SESSION_COOKIE not in request.cookies:
        # 毎回発行するとページ遷移ごとに別人になり、操作権が自分から離れる
        response.set_cookie(SESSION_COOKIE, secrets.token_urlsafe(16), httponly=True)
    return response


def render_standalone(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    current_suffix: str,
) -> HTMLResponse:
    """マシン非依存のページを chrome 付きで描く（`/update` など）.

    `_chrome_context` + `_html_page` の公開ラッパ。backend へ 1 度も問い合わせないので
    機体が 1 台も居ないホストでも描ける（frontend 専用機の更新ページがこれ）。

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

    backend への到達を要さないコンテキストだけで描くので、backend が落ちていても
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


async def _open_default(request: Request, suffix: str) -> Response:
    """マシン未指定の URL を既知マシンへ振り分ける."""
    machines = _registry(request).list()
    if len(machines) == 1:
        return RedirectResponse(
            url=f"/m/{machines[0].machine_id}/{suffix}", status_code=307
        )
    if machines:
        return render_message(
            request,
            title="マシンを選択してください",
            detail="右上のドロップダウンから操作するマシンを選びます。",
        )
    return render_message(
        request,
        title="マシンが登録されていません",
        detail=(
            "config/machines.toml に [[machine]] を書いて frontend を再起動してください"
            "（machine_id / host は必須、port の既定は 8081）。"
        ),
    )


@router.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    return await _open_default(request, DEFAULT_TAB)


@router.get("/settings", include_in_schema=False)
async def settings_entry(request: Request) -> Response:
    return await _open_default(request, "settings")


# `/{tab}/{feature}` より先に登録する（後だと tab="m" / feature=machine_id として
# 食われ、`/m/{id}/m/{id}` へ 307 したうえで 404 になる）
@router.get("/m/{machine_id}", include_in_schema=False)
async def machine_entry(machine_id: str, request: Request) -> Response:
    return RedirectResponse(url=f"/m/{machine_id}/{DEFAULT_TAB}", status_code=307)


@router.get("/{tab}", include_in_schema=False)
async def tab_entry(tab: str, request: Request) -> Response:
    return await _open_default(request, tab)


@router.get("/{tab}/{feature}", include_in_schema=False)
async def feature_entry(tab: str, feature: str, request: Request) -> Response:
    return await _open_default(request, f"{tab}/{feature}")


# /{tab} より先に登録する（後だと settings が tab として食われる）
@router.get("/m/{machine_id}/settings", response_class=HTMLResponse)
async def settings_page(machine_id: str, request: Request) -> HTMLResponse:
    endpoint, client = _resolve(request, machine_id)
    info, state, machine_settings = await asyncio.gather(
        client.machine_info(), client.state(), client.machine_settings()
    )
    context = _base_context(request, machine_id, "settings", info, state)
    context.update(
        machine_groups=grouped_fields(machine_settings.fields),
        clearable_keys=CLEARABLE_MACHINE_KEYS,
    )
    return _html_page(request, "settings.html", context)


def _loading_context(job: JobSpecInfo, settings: _MachineSettings) -> dict[str, Any]:
    """Loading ページ専用コンテキスト（開始位置・回転値・現在設定値）."""
    rotation_defaults = {
        spec.name: spec.default
        for spec in job.params
        if spec.name in LOADING_ROTATION_PARAMS
    }
    return {
        "loading_control_specs": tuple(
            spec for spec in job.params if not spec.optional
        ),
        "loading_position_specs": tuple(spec for spec in job.params if spec.optional),
        "loading_rotation_defaults": rotation_defaults,
        "solder_paste_density": settings.number("paste_dispenser.solder_paste_density"),
        "current_rotations_per_ul": settings.number("paste_dispenser.rotations_per_ul"),
        "current_max_dispense_rate": settings.number(
            "paste_dispenser.max_dispense_rate"
        ),
        "current_dispense_accel": settings.number("paste_dispenser.dispense_accel"),
    }


def _dispense_calibration_context(
    job: JobSpecInfo, settings: _MachineSettings
) -> dict[str, Any]:
    """Dispense_calibration ページ専用コンテキスト（フォームのセクション分け）."""
    specs_by_name = {spec.name: spec for spec in job.params}
    return {
        "param_groups": [
            (legend, [specs_by_name[name] for name in names])
            for legend, names in DISPENSE_CALIBRATION_PARAM_GROUPS
        ]
    }


def _paste_volume_calibration_context(
    job: JobSpecInfo, settings: _MachineSettings
) -> dict[str, Any]:
    """塗布量校正の生成ページ専用コンテキスト（セクション分けと回転ローディング）.

    塗布パス先頭のローディングでは体積と回転の両方を使うので、``loading_controls``
    partial の回転セクションを出すための既定値を渡す。ジョブ側の ParamSpec 名は
    ``loading_`` prefix 付きなので、partial が読む素の名前へ写す。
    """
    specs_by_name = {spec.name: spec for spec in job.params}
    return {
        "param_groups": [
            (legend, [specs_by_name[name] for name in names])
            for legend, names in PASTE_VOLUME_CALIBRATION_PARAM_GROUPS
        ],
        "loading_rotation_defaults": {
            name: specs_by_name[f"loading_{name}"].default
            for name in LOADING_ROTATION_PARAMS
        },
    }


def _paste_workspace_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """Pad editor 付き塗布ページのコンテキスト（machine 設定の即保存フォーム）."""
    return {
        "auto_threshold_fields": [
            field for field in settings.fields if field.key in PASTE_AUTO_THRESHOLD_KEYS
        ],
        "pad_refinement_fields": [
            field for field in settings.fields if field.key in PASTE_PAD_REFINEMENT_KEYS
        ],
        "flow_calibration_fields": [
            field
            for field in settings.fields
            if field.key in PASTE_FLOW_CALIBRATION_KEYS
        ],
        "flow_calibration_file_key": PASTE_FLOW_CALIBRATION_FILE_KEY,
    }


def _copper_detection_context(
    state: StateResponse, settings: _MachineSettings
) -> dict[str, Any]:
    """Copper_detection ページ専用コンテキスト（エッジ検出パラメータ現在値）."""
    prefix = "paste_dispenser.pad_align"
    return {
        "canny_low": settings.number(f"{prefix}.canny_low"),
        "canny_high": settings.number(f"{prefix}.canny_high"),
        "blur_ksize": int(settings.number(f"{prefix}.blur_ksize")),
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
        "nozzle_clean_fields": [
            field for field in settings.fields if field.key in NOZZLE_CLEAN_SETTING_KEYS
        ],
        "positive_only_keys": POSITIVE_ONLY_MACHINE_KEYS,
    }


# feature slug → ジョブページ専用コンテキスト（ジョブ定義依存）
_JOB_FEATURE_CONTEXT: dict[
    str, Callable[[JobSpecInfo, _MachineSettings], dict[str, Any]]
] = {
    "loading": _loading_context,
    "dispense_calibration": _dispense_calibration_context,
    "paste_volume_calibration": _paste_volume_calibration_context,
}

# feature slug → ページ専用コンテキスト（ジョブ有無に依らない）
_FEATURE_CONTEXT: dict[
    str, Callable[[StateResponse, _MachineSettings], dict[str, Any]]
] = {
    "paste_solder": _paste_workspace_context,
    "copper_detection": _copper_detection_context,
    "nozzle_cap": _nozzle_cap_context,
}


@router.get("/m/{machine_id}/{tab}", response_class=HTMLResponse)
async def tab_page(machine_id: str, tab: str, request: Request) -> HTMLResponse:
    if tab not in TABS:
        raise HTTPException(status_code=404, detail=f"未知のタブです: {tab}")
    _endpoint, client = _resolve(request, machine_id)
    info, state, catalog = await asyncio.gather(
        client.machine_info(), client.state(), client.jobs()
    )
    context = _base_context(request, machine_id, tab, info, state)
    context.update(_tab_context(tab, _jobs_by_name(catalog)))
    return _html_page(request, "tab.html", context)


@router.get("/m/{machine_id}/{tab}/{feature}", response_class=HTMLResponse)
async def feature_page(
    machine_id: str, tab: str, feature: str, request: Request
) -> HTMLResponse:
    if tab not in TABS or feature not in TABS[tab]:
        raise HTTPException(
            status_code=404, detail=f"未知のフィーチャーです: {tab}/{feature}"
        )
    endpoint, client = _resolve(request, machine_id)
    info, state, catalog, machine_settings = await asyncio.gather(
        client.machine_info(),
        client.state(),
        client.jobs(),
        _machine_settings(endpoint, client, feature in _MACHINE_SETTINGS_FEATURES),
    )
    jobs = _jobs_by_name(catalog)
    context = _base_context(request, machine_id, f"{tab}/{feature}", info, state)
    context.update(
        _tab_context(tab, jobs),
        active_feature=feature,
        feature_label=_feature_label(jobs, feature),
        phase=TAB_PHASES[tab],
    )
    template = FEATURE_TEMPLATES.get((tab, feature), "feature.html")
    if template in JOB_TEMPLATES:
        job = _job_spec(endpoint, jobs, feature)
        # preview ペインとローディング UI の有無は backend の事実（フレーム提供の
        # 有無・progress_stage 文字列）なので backend の自己申告から導出する
        context.update(
            job_name=job.name,
            param_specs=job.params,
            show_preview=job.provides_preview,
            show_loading_controls=job.loading_param is not None,
            loading_stage=job.loading_stages,
        )
        if job.loading_param is not None:
            context["loading_default"] = next(
                spec.default for spec in job.params if spec.name == job.loading_param
            )
        if (job_provider := _JOB_FEATURE_CONTEXT.get(feature)) is not None:
            context.update(job_provider(job, machine_settings))
    if (provider := _FEATURE_CONTEXT.get(feature)) is not None:
        context.update(provider(state, machine_settings))
    return _html_page(request, template, context)
