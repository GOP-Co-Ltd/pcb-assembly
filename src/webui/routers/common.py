"""複数ルーターが共有するヘルパ（Klipper 接続・状態レスポンス・設定項目）.

2 つ以上の router から使われるものだけを置く（1 router 専用のヘルパは 各 router に残す）。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from pcbasm.hal import Klipper
from webui.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    FieldSpec,
    MachineSettingValue,
    SettingValueType,
)
from webui.jobs.manager import JobManager
from webui.models import JobBrief, KlipperStatus, Position
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

# --------------------------------------------------------------------------- #
# Klipper 接続
# --------------------------------------------------------------------------- #


def create_klipper(state: AppState, timeout: float) -> Klipper:
    """マシン設定で Klipper クライアントを生成する."""
    klipper_config = state.machine().klipper
    return Klipper(host=klipper_config.host, port=klipper_config.port, timeout=timeout)


def fetch_status(klipper: Klipper) -> KlipperStatus:
    """Klipper から位置と homed_axes を取得する。失敗時は connected=False."""
    try:
        position = klipper.get_status("gcode_move", "gcode_position")
        homed_axes = klipper.get_status("toolhead", "homed_axes")
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    return KlipperStatus(
        connected=True,
        position=Position(x=position[0], y=position[1], z=position[2]),
        homed_axes=homed_axes,
    )


@contextmanager
def klipper_errors_to_502() -> Iterator[None]:
    """Klipper 通信エラーを HTTP 502 へ変換する.

    Raises:
        HTTPException: httpx.HTTPError / RuntimeError / KeyError の場合（502）
    """
    try:
        yield
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc


# --------------------------------------------------------------------------- #
# アプリ状態レスポンス
# --------------------------------------------------------------------------- #


class StateResponse(BaseModel):
    pcb_file: str | None
    busy: bool
    busy_owner: str | None
    focus_z: float | None
    mainsail_url: str | None
    preview_clients: int
    job: JobBrief | None


def build_state_response(
    state: AppState, settings: Settings, preview: PreviewService, jobs: JobManager
) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = state.busy_owner
    pcb = state.selected_pcb
    record = jobs.current()
    return StateResponse(
        pcb_file=pcb.as_posix() if pcb else None,
        busy=owner is not None,
        busy_owner=owner,
        focus_z=state.focus_z(),
        mainsail_url=settings.mainsail_url,
        preview_clients=preview.client_count,
        job=(
            JobBrief(id=record.id, name=record.name, status=record.status.value)
            if record is not None
            else None
        ),
    )


# --------------------------------------------------------------------------- #
# マシン設定項目（settings ページ / API 共用）
# --------------------------------------------------------------------------- #


class SettingsField(BaseModel):
    key: str
    label: str
    value_type: SettingValueType
    unit: str | None
    value: MachineSettingValue | None


def _fields(
    specs: tuple[FieldSpec, ...], values: Mapping[str, MachineSettingValue | None]
) -> list[SettingsField]:
    return [
        SettingsField(
            key=spec.key,
            label=spec.label,
            value_type=spec.value_type,
            unit=spec.unit,
            value=values[spec.key],
        )
        for spec in specs
    ]


def machine_settings_fields(store: ConfigStore) -> list[SettingsField]:
    """machine.toml のホワイトリスト項目を現在値付きで返す."""
    return _fields(MACHINE_FIELDS, store.read_machine_settings())


# 設定セクション（key のドット区切り親パス）→ UI 表示名。
# settings ページの階層表示に使う
SECTION_LABELS: dict[str, str] = {
    "paste_dispenser": "ペーストディスペンサー",
    "paste_dispenser.toolhead": "ペーストディスペンサー / ツールヘッド",
    "paste_dispenser.pad_align": "ペーストディスペンサー / パッド位置合わせ",
    "probe": "プローブ",
    "reference_point": "基準点",
    "reference_point.offsets": "基準点 / コーナーオフセット",
    "nozzle_cap": "ノズルキャップ",
    "camera": "カメラ",
    "camera.crop": "カメラ / クロップ",
}


def section_of(key: str) -> str:
    """設定 key の属するセクション（最後のドットより前）を返す."""
    return key.rsplit(".", 1)[0]
