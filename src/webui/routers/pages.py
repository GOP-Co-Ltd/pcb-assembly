"""Jinja2 ページのルーター."""

from __future__ import annotations

from itertools import groupby
from typing import Any

import attrs
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webui.app import CatalogDep, SettingsDep, StateDep, StoreDep, get_templates
from webui.config_store import SECTION_LABELS, section_of
from webui.jobs.catalog import JobDefinition, ParamSpec
from webui.routers.settings_api import SettingsField, machine_settings_fields
from webui.state import AppState

# tab → feature slug 列（ヘッダのタブ表示順）
TABS: dict[str, tuple[str, ...]] = {
    "dev": (
        "extract_pcb",
        "make_fill_coverage_pcb",
        "klipper_status",
    ),
    "pasting": (
        "paste_solder",
        "height_plane",
        "loading",
        "flow_calibration",
        "generate_rect_pcb",
        "toolhead_offset",
        "probe_gnd_down_adjust",
    ),
    "pnp": (),
    "posctrl": (
        "camera_preview",
        "copper_detection",
        "camera_calibration",
        "reference_point_setup",
        "board_tour",
        "orthogonality_test",
        "generate_grid_pcb",
    ),
}

# tab slug → 表示名（ヘッダのタブラベル）
TAB_LABELS: dict[str, str] = {
    "dev": "開発",
    "pasting": "はんだ塗布",
    "pnp": "部品実装",
    "posctrl": "位置合わせ",
}

# feature slug → 表示名（サイドバー / 見出し）。未定義は単語化フォールバック
FEATURE_LABELS: dict[str, str] = {
    "extract_pcb": "PCB 情報抽出",
    "make_fill_coverage_pcb": "塗布カバレッジ PCB 生成",
    "klipper_status": "Klipper ステータス",
    "paste_solder": "はんだ塗布",
    "height_plane": "高さ平面計測",
    "loading": "ペーストローディング",
    "flow_calibration": "吐出量キャリブレーション",
    "generate_rect_pcb": "キャリブレーション矩形 PCB 生成",
    "toolhead_offset": "ツールヘッドオフセット計測",
    "probe_gnd_down_adjust": "GND プローブ下降量調整",
    "camera_preview": "カメラプレビュー",
    "copper_detection": "銅箔検出調整",
    "camera_calibration": "カメラキャリブレーション",
    "reference_point_setup": "基準点設定",
    "board_tour": "ボード巡回",
    "orthogonality_test": "直行性テスト",
    "generate_grid_pcb": "グリッド PCB 生成",
}

# feature 実装予定の Phase（プレースホルダ表示用）
TAB_PHASES: dict[str, str] = {
    "dev": "Phase 3",
    "pasting": "Phase 5",
    "pnp": "将来",
    "posctrl": "Phase 2/4",
}

# 専用テンプレートを持つ feature（無いものは feature.html プレースホルダ）
# job.html はカメラ preview を持たない汎用ジョブページ（タブ横断で共用）
FEATURE_TEMPLATES: dict[tuple[str, str], str] = {
    ("dev", "extract_pcb"): "job.html",
    ("dev", "make_fill_coverage_pcb"): "job.html",
    ("dev", "klipper_status"): "dev/klipper_status.html",
    ("pasting", "paste_solder"): "pasting/paste_solder.html",
    ("pasting", "height_plane"): "pasting/job.html",
    ("pasting", "loading"): "pasting/loading.html",
    ("pasting", "flow_calibration"): "pasting/job.html",
    ("pasting", "generate_rect_pcb"): "pasting/job.html",
    ("pasting", "toolhead_offset"): "pasting/job.html",
    ("pasting", "probe_gnd_down_adjust"): "pasting/job.html",
    ("posctrl", "camera_preview"): "posctrl/camera_preview.html",
    ("posctrl", "copper_detection"): "posctrl/copper_detection.html",
    ("posctrl", "camera_calibration"): "posctrl/job.html",
    ("posctrl", "board_tour"): "posctrl/job.html",
    ("posctrl", "orthogonality_test"): "posctrl/job.html",
    ("posctrl", "reference_point_setup"): "posctrl/reference_point_setup.html",
    ("posctrl", "generate_grid_pcb"): "job.html",
}

# ジョブコンテキスト（job_name / param_specs）を注入するテンプレート
_JOB_TEMPLATES = frozenset(
    {
        "job.html",
        "pasting/job.html",
        "pasting/loading.html",
        "pasting/paste_solder.html",
        "posctrl/job.html",
        "posctrl/reference_point_setup.html",
    }
)

# preview ペイン（ジョブ提供フレームのみ）を表示する pasting feature
_PASTING_PREVIEW = frozenset({"paste_solder", "height_plane", "toolhead_offset"})

# loading コマンド UI を表示する pasting feature → 既定量の ParamSpec 名
_PASTING_LOADING_PARAM = {
    "paste_solder": "amount",
    "loading": "amount",
    "flow_calibration": "load_amount",
    "toolhead_offset": "loading_amount",
}

_LOADING_ROTATION_PARAMS = ("rotations", "rate", "accel")

router = APIRouter()


def _feature_label(slug: str) -> str:
    return FEATURE_LABELS.get(slug, slug.replace("_", " ").title())


def _grouped_fields(
    fields: list[SettingsField],
) -> list[tuple[str, list[SettingsField]]]:
    """設定項目をセクション単位にまとめる（定義順を保つ）."""
    return [
        (SECTION_LABELS.get(section, section), list(group))
        for section, group in groupby(fields, key=lambda f: section_of(f.key))
    ]


def _tab_context(tab: str) -> dict[str, Any]:
    """タブ共通のコンテキスト（サイドバー描画用）."""
    return {
        "active_tab": tab,
        "features": TABS[tab],
        "feature_labels": {slug: _feature_label(slug) for slug in TABS[tab]},
    }


def _fb_start(settings: SettingsDep) -> str:
    """ファイルブラウザの初期表示パス（pcb_browse_root からの相対）."""
    try:
        start = (
            settings.pcb_browse_start.resolve()
            .relative_to(settings.pcb_browse_root.resolve())
            .as_posix()
        )
    except ValueError:
        return ""
    return "" if start == "." else start


def _saved_default_matches(spec: ParamSpec, value: object) -> bool:
    if spec.value_type == "bool":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    match spec.value_type:
        case "float":
            return isinstance(value, (int, float))
        case "int":
            return isinstance(value, int)
        case "str":
            return isinstance(value, str)
        case "choice":
            return isinstance(value, str) and value in spec.choices
    return False


def _param_specs_with_saved_defaults(
    definition: JobDefinition, state: AppState
) -> tuple[ParamSpec, ...]:
    saved = state.job_param_defaults(definition.name)
    if not saved or not definition.persisted_params:
        return definition.params
    persisted = set(definition.persisted_params)
    return tuple(
        attrs.evolve(spec, default=saved[spec.name])
        if spec.name in persisted
        and spec.name in saved
        and _saved_default_matches(spec, saved[spec.name])
        else spec
        for spec in definition.params
    )


def _base_context(
    request: Request, state: StateDep, store: StoreDep, settings: SettingsDep
) -> dict[str, Any]:
    pcb = state.selected_pcb
    return {
        "request": request,
        "tabs": list(TABS),
        "tab_labels": TAB_LABELS,
        "machines": store.list_machines(),
        "selected_machine": state.selected_machine,
        "selected_pcb": pcb.as_posix() if pcb else None,
        "fb_start": _fb_start(settings),
        "mainsail_url": settings.mainsail_url
        or f"http://{request.url.hostname or 'localhost'}",
        "focus_z": state.focus_z(),
        "active_tab": None,
        "active_feature": None,
    }


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/posctrl", status_code=307)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, state: StateDep, store: StoreDep, settings: SettingsDep
) -> HTMLResponse:
    context = _base_context(request, state, store, settings)
    machine = state.selected_machine
    context.update(
        machine_groups=_grouped_fields(machine_settings_fields(store, machine)),
    )
    return get_templates(request).TemplateResponse(
        request=request, name="settings.html", context=context
    )


@router.get("/{tab}", response_class=HTMLResponse)
def tab_page(
    tab: str, request: Request, state: StateDep, store: StoreDep, settings: SettingsDep
) -> HTMLResponse:
    if tab not in TABS:
        raise HTTPException(status_code=404, detail=f"未知のタブです: {tab}")
    context = _base_context(request, state, store, settings)
    context.update(_tab_context(tab))
    return get_templates(request).TemplateResponse(
        request=request, name="tab.html", context=context
    )


@router.get("/{tab}/{feature}", response_class=HTMLResponse)
def feature_page(
    tab: str,
    feature: str,
    request: Request,
    state: StateDep,
    store: StoreDep,
    settings: SettingsDep,
    catalog: CatalogDep,
) -> HTMLResponse:
    if tab not in TABS or feature not in TABS[tab]:
        raise HTTPException(
            status_code=404, detail=f"未知のフィーチャーです: {tab}/{feature}"
        )
    context = _base_context(request, state, store, settings)
    context.update(
        _tab_context(tab),
        active_feature=feature,
        feature_label=_feature_label(feature),
        phase=TAB_PHASES[tab],
    )
    template = FEATURE_TEMPLATES.get((tab, feature), "feature.html")
    if template in _JOB_TEMPLATES:
        definition = catalog.get(feature)
        param_specs = _param_specs_with_saved_defaults(definition, state)
        context.update(job_name=definition.name, param_specs=param_specs)
        if tab == "pasting":
            loading_param = _PASTING_LOADING_PARAM.get(feature)
            context.update(
                show_preview=feature in _PASTING_PREVIEW,
                show_loading_controls=loading_param is not None,
            )
            if loading_param is not None:
                context["loading_default"] = next(
                    spec.default for spec in param_specs if spec.name == loading_param
                )
            if feature == "loading":
                rotation_defaults = {
                    spec.name: spec.default
                    for spec in param_specs
                    if spec.name in _LOADING_ROTATION_PARAMS
                }
                dispenser = state.machine().paste_dispenser
                context.update(
                    loading_rotation_defaults=rotation_defaults,
                    solder_paste_density=dispenser.solder_paste_density,
                    current_rotations_per_ul=dispenser.rotations_per_ul,
                    current_max_dispense_rate=dispenser.max_dispense_rate,
                    current_dispense_accel=dispenser.dispense_accel,
                )
    if feature == "copper_detection":
        pad_align = state.machine().paste_dispenser.pad_align
        context.update(
            canny_low=pad_align.canny_low,
            canny_high=pad_align.canny_high,
            blur_ksize=pad_align.blur_ksize,
        )
    return get_templates(request).TemplateResponse(
        request=request, name=template, context=context
    )
