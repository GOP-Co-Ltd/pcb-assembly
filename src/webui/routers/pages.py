"""Jinja2 ページのルーター."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webui.app import SettingsDep, StateDep, StoreDep, get_templates
from webui.routers.settings_api import machine_settings_fields, motion_settings_fields

# tab → feature slug 列（ヘッダのタブ表示順）
TABS: dict[str, tuple[str, ...]] = {
    "dev": (
        "extract_pcb",
        "fill_path_simulate",
        "generate_grid_pcb",
        "make_fill_coverage_pcb",
        "klipper_status",
    ),
    "pasting": (
        "paste_solder",
        "height_plane",
        "loading",
        "flow_calibration",
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
    ),
}

# feature 実装予定の Phase（プレースホルダ表示用）
TAB_PHASES: dict[str, str] = {
    "dev": "Phase 3",
    "pasting": "Phase 5",
    "pnp": "将来",
    "posctrl": "Phase 2/4",
}

# 専用テンプレートを持つ feature（無いものは feature.html プレースホルダ）
FEATURE_TEMPLATES: dict[tuple[str, str], str] = {
    ("posctrl", "camera_preview"): "posctrl/camera_preview.html",
    ("posctrl", "copper_detection"): "posctrl/copper_detection.html",
}

router = APIRouter()


def _feature_label(slug: str) -> str:
    return slug.replace("_", " ").title()


def _tab_context(tab: str) -> dict[str, Any]:
    """タブ共通のコンテキスト（サイドバー描画用）."""
    return {
        "active_tab": tab,
        "features": TABS[tab],
        "feature_labels": {slug: _feature_label(slug) for slug in TABS[tab]},
    }


def _base_context(
    request: Request, state: StateDep, store: StoreDep, settings: SettingsDep
) -> dict[str, Any]:
    pcb = state.selected_pcb
    return {
        "request": request,
        "tabs": list(TABS),
        "machines": store.list_machines(),
        "selected_machine": state.selected_machine,
        "selected_pcb": pcb.as_posix() if pcb else None,
        "mainsail_url": settings.mainsail_url,
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
        machine_fields=machine_settings_fields(store, machine),
        motion_fields=motion_settings_fields(store, machine),
        symlink_ok=store.symlink_points_to(machine, settings.printer_cfg_link),
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
    if feature == "copper_detection":
        pad_align = state.machine().paste_dispenser.pad_align
        context.update(
            canny_low=pad_align.canny_low,
            canny_high=pad_align.canny_high,
            blur_ksize=pad_align.blur_ksize,
        )
    template = FEATURE_TEMPLATES.get((tab, feature), "feature.html")
    return get_templates(request).TemplateResponse(
        request=request, name=template, context=context
    )
