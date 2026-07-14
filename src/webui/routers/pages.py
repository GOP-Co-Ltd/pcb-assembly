"""Jinja2 ページのルーター."""

from __future__ import annotations

from collections.abc import Callable
from itertools import groupby
from typing import Any

import attrs
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webui.config_store import ConfigStore
from webui.dependencies import (
    CatalogDep,
    SettingsDep,
    StateDep,
    StoreDep,
    get_templates,
)
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.routers.common import (
    CORNER_LABELS,
    SECTION_LABELS,
    SettingsField,
    machine_settings_fields,
    section_of,
)
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
        "dispense_calibration",
        "generate_rect_pcb",
        "toolhead_offset",
        "probe_guide",
        "nozzle_cap",
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

# 非ジョブ feature slug → 表示名（サイドバー / 見出し）。ジョブは catalog の
# JobDefinition.label を正とする。未定義は単語化フォールバック
FEATURE_LABELS: dict[str, str] = {
    "klipper_status": "Klipper ステータス",
    "probe_guide": "ロードセルプローブ ガイド",
    "nozzle_cap": "ノズルキャップ位置の設定",
    "camera_preview": "カメラプレビュー",
    "copper_detection": "銅箔検出調整",
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
    ("pasting", "dispense_calibration"): "pasting/dispense_calibration.html",
    ("pasting", "generate_rect_pcb"): "pasting/job.html",
    ("pasting", "toolhead_offset"): "pasting/job.html",
    ("pasting", "probe_guide"): "pasting/probe_guide.html",
    ("pasting", "nozzle_cap"): "pasting/nozzle_cap.html",
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
        "pasting/dispense_calibration.html",
        "pasting/paste_solder.html",
        "posctrl/job.html",
        "posctrl/reference_point_setup.html",
    }
)

# preview ペイン（ジョブ提供フレームのみ）を表示する pasting feature
_PASTING_PREVIEW = frozenset({"paste_solder", "height_plane", "toolhead_offset"})

# はんだ塗布ページに即保存フォームで載せる auto しきい値（machine 全体設定）
_PASTE_AUTO_THRESHOLD_KEYS = (
    "paste_dispenser.auto_line_aspect_ratio",
    "paste_dispenser.auto_area_short_side_factor",
)

# loading コマンド UI を表示する pasting feature → 既定量の ParamSpec 名
_PASTING_LOADING_PARAM = {
    "paste_solder": "amount",
    "loading": "amount",
    "dispense_calibration": "line_amount",
    "toolhead_offset": "loading_amount",
}

# loading_controls をローディング段階以外でも有効化する feature → progress stage 名
# （カンマ区切りで複数可。loading_controls.js が Set として解釈する）。
# dispense_calibration はメニュー段階のプライム（押出/吸引）と、① 専用ローディング段階
# （"ローディング"）の両方でボタンを有効化する。
_LOADING_STAGE_OVERRIDE = {
    "dispense_calibration": "キャリブレーションメニュー,ローディング",
}

_LOADING_ROTATION_PARAMS = ("rotations", "rate", "accel", "retract_rotations")

# dispense_calibration フォームのセクション分け（表示のみ）。
# ①②③ の依存順に沿ってパラメータを視覚的にグルーピングする。
_DISPENSE_CALIBRATION_PARAM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "銅板・位置合わせ（キャリブ後固定）",
        ("board_width", "board_height", "tolerance"),
    ),
    (
        "線の共通設定（実行中変更可）",
        ("line_length", "line_count", "line_amount", "row_pitch", "removal_z_offset"),
    ),
    (
        "② max_dispense_rate（吐出効率の落ち検出）",
        ("rate_min", "rate_max", "rate_divisions"),
    ),
    (
        "③ max_fill_speed（連続塗布の最大速度）",
        ("speed_min", "speed_max", "speed_divisions"),
    ),
)

router = APIRouter()


def _feature_label(catalog: JobCatalog, slug: str) -> str:
    try:
        return catalog.get(slug).label
    except KeyError:
        return FEATURE_LABELS.get(slug, slug.replace("_", " ").title())


def _grouped_fields(
    fields: list[SettingsField],
) -> list[tuple[str, list[SettingsField]]]:
    """設定項目をセクション単位にまとめる（定義順を保つ）."""
    return [
        (SECTION_LABELS.get(section, section), list(group))
        for section, group in groupby(fields, key=lambda f: section_of(f.key))
    ]


def _tab_context(tab: str, catalog: JobCatalog) -> dict[str, Any]:
    """タブ共通のコンテキスト（サイドバー描画用）."""
    return {
        "active_tab": tab,
        "features": TABS[tab],
        "feature_labels": {slug: _feature_label(catalog, slug) for slug in TABS[tab]},
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


def _param_specs_with_saved_defaults(
    definition: JobDefinition, state: AppState, catalog: JobCatalog
) -> tuple[ParamSpec, ...]:
    """保存済み既定値を ParamSpec の default に反映する.

    型判定・coerce は :meth:`JobCatalog.filter_persisted_defaults` に一本化する
    （persisted_params 外・型不一致は黙って除外 = spec 既定値のまま）。
    """
    saved = state.job_param_defaults(definition.name)
    if not saved or not definition.persisted_params:
        return definition.params
    valid = catalog.filter_persisted_defaults(definition, saved)
    return tuple(
        attrs.evolve(spec, default=valid[spec.name]) if spec.name in valid else spec
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
        "machine_type": state.machine_type(),
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
        corner_labels=CORNER_LABELS,
    )
    return get_templates(request).TemplateResponse(
        request=request, name="settings.html", context=context
    )


def _loading_context(
    state: AppState, param_specs: tuple[ParamSpec, ...]
) -> dict[str, Any]:
    """Loading ページ専用コンテキスト（回転既定値 + 現在のマシン設定値）."""
    rotation_defaults = {
        spec.name: spec.default
        for spec in param_specs
        if spec.name in _LOADING_ROTATION_PARAMS
    }
    dispenser = state.machine().paste_dispenser
    return {
        "loading_rotation_defaults": rotation_defaults,
        "solder_paste_density": dispenser.solder_paste_density,
        "current_rotations_per_ul": dispenser.rotations_per_ul,
        "current_max_dispense_rate": dispenser.max_dispense_rate,
        "current_dispense_accel": dispenser.dispense_accel,
    }


def _dispense_calibration_context(
    state: AppState, param_specs: tuple[ParamSpec, ...]
) -> dict[str, Any]:
    """Dispense_calibration ページ専用コンテキスト（フォームのセクション分け）."""
    specs_by_name = {spec.name: spec for spec in param_specs}
    return {
        "param_groups": [
            (legend, [specs_by_name[name] for name in names])
            for legend, names in _DISPENSE_CALIBRATION_PARAM_GROUPS
        ]
    }


def _paste_solder_context(state: AppState, store: ConfigStore) -> dict[str, Any]:
    """Paste_solder ページ専用コンテキスト（auto しきい値の即保存フォーム）."""
    return {
        "auto_threshold_fields": [
            field
            for field in machine_settings_fields(store, state.selected_machine)
            if field.key in _PASTE_AUTO_THRESHOLD_KEYS
        ]
    }


def _copper_detection_context(state: AppState, store: ConfigStore) -> dict[str, Any]:
    """Copper_detection ページ専用コンテキスト（エッジ検出パラメータ現在値）."""
    pad_align = state.machine().paste_dispenser.pad_align
    return {
        "canny_low": pad_align.canny_low,
        "canny_high": pad_align.canny_high,
        "blur_ksize": pad_align.blur_ksize,
    }


def _nozzle_cap_context(state: AppState, store: ConfigStore) -> dict[str, Any]:
    """Nozzle_cap ページ専用コンテキスト（記録済みキャップ位置の現在値）."""
    return {"nozzle_cap": state.machine().nozzle_cap}


# feature slug → ジョブページ専用コンテキスト（param_specs 依存）
_JOB_FEATURE_CONTEXT: dict[
    str, Callable[[AppState, tuple[ParamSpec, ...]], dict[str, Any]]
] = {
    "loading": _loading_context,
    "dispense_calibration": _dispense_calibration_context,
}

# feature slug → ページ専用コンテキスト（ジョブ有無に依らない）
_FEATURE_CONTEXT: dict[str, Callable[[AppState, ConfigStore], dict[str, Any]]] = {
    "paste_solder": _paste_solder_context,
    "copper_detection": _copper_detection_context,
    "nozzle_cap": _nozzle_cap_context,
}


@router.get("/{tab}", response_class=HTMLResponse)
def tab_page(
    tab: str,
    request: Request,
    state: StateDep,
    store: StoreDep,
    settings: SettingsDep,
    catalog: CatalogDep,
) -> HTMLResponse:
    if tab not in TABS:
        raise HTTPException(status_code=404, detail=f"未知のタブです: {tab}")
    context = _base_context(request, state, store, settings)
    context.update(_tab_context(tab, catalog))
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
        _tab_context(tab, catalog),
        active_feature=feature,
        feature_label=_feature_label(catalog, feature),
        phase=TAB_PHASES[tab],
    )
    template = FEATURE_TEMPLATES.get((tab, feature), "feature.html")
    if template in _JOB_TEMPLATES:
        definition = catalog.get(feature)
        param_specs = _param_specs_with_saved_defaults(definition, state, catalog)
        context.update(job_name=definition.name, param_specs=param_specs)
        if tab == "pasting":
            loading_param = _PASTING_LOADING_PARAM.get(feature)
            context.update(
                show_preview=feature in _PASTING_PREVIEW,
                show_loading_controls=loading_param is not None,
                loading_stage=_LOADING_STAGE_OVERRIDE.get(feature, "ローディング"),
            )
            if loading_param is not None:
                context["loading_default"] = next(
                    spec.default for spec in param_specs if spec.name == loading_param
                )
        if (job_provider := _JOB_FEATURE_CONTEXT.get(feature)) is not None:
            context.update(job_provider(state, param_specs))
    if (provider := _FEATURE_CONTEXT.get(feature)) is not None:
        context.update(provider(state, store))
    return get_templates(request).TemplateResponse(
        request=request, name=template, context=context
    )
