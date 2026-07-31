"""複数ルーターが共有するヘルパ（Klipper 接続・状態レスポンス・設定項目）.

2 つ以上の router から使われるものだけを置く（1 router 専用のヘルパは 各 router に残す）。

pydantic モデルの定義は `webui.models` に集約してある。``StateResponse`` /
``SettingsField`` は既存 import を壊さないためここから再 export する。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import attrs
import httpx
from fastapi import HTTPException

from pcbasm.hal import Klipper
from webui.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    FieldSpec,
    MachineSettingValue,
)
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.manager import JobManager
from webui.models import (
    API_VERSION,
    JobBrief,
    KlipperStatus,
    MachineInfo,
    Position,
    SettingsField,
    StateResponse,
)
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


def build_state_response(
    state: AppState, settings: Settings, preview: PreviewService, jobs: JobManager
) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = state.busy_owner
    pcb = state.selected_pcb
    record = jobs.current()
    cap = state.nozzle_cap()
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
        nozzle_cap=(None if cap is None else Position(x=cap.x, y=cap.y, z=cap.z)),
    )


# --------------------------------------------------------------------------- #
# backend の自己申告（/api/machine-info と SSR が共有する解決結果）
# --------------------------------------------------------------------------- #


def _fb_start(settings: Settings) -> str:
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


def _default_mainsail_url(machine_id: str) -> str:
    """``mainsail_url`` 未設定時のフォールバック URL.

    ``machine_id`` は短いホスト名（``socket.gethostname()``）なので、LAN の他端末
    からは mDNS 経由の ``*.local`` しか引けない。裸のホスト名を返すとリモートから
    Mainsail を開けなくなるため ``.local`` を付ける。既にドットを含む
    （FQDN や ``.local`` 付きが注入された）場合は重ねない。
    """
    host = machine_id if "." in machine_id else f"{machine_id}.local"
    return f"http://{host}"


def build_machine_info(state: AppState, settings: Settings) -> MachineInfo:
    """Backend の自己申告情報を組み立てる.

    ホスト名・表示名の未設定フォールバックといった環境依存の解決はここに集約する
    （pcbasm 層は machine.toml に書かれた値だけを返す）。``mainsail_url`` は
    リクエストのホスト名に依存させない（プロキシ配下で必ず誤るため）。
    """
    machine_id = settings.hostname or socket.gethostname()
    return MachineInfo(
        machine_id=machine_id,
        machine_name=state.machine_name() or machine_id,
        machine_type=state.machine_type(),
        mainsail_url=settings.mainsail_url or _default_mainsail_url(machine_id),
        fb_start=_fb_start(settings),
        api_version=API_VERSION,
    )


# --------------------------------------------------------------------------- #
# ジョブ定義のパラメータ（ジョブページ SSR / /api/jobs 共用）
# --------------------------------------------------------------------------- #


def param_specs_with_saved_defaults(
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


# --------------------------------------------------------------------------- #
# マシン設定項目（settings ページ / API 共用）
# --------------------------------------------------------------------------- #


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
    # トップレベル（bare key）は section_of が生キーを返すため、明示的にラベルを持たせる
    "machine_name": "マシン",
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
