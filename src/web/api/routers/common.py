"""複数ルーターが共有するヘルパ（Klipper の状態・状態レスポンス・ジョブ定義のパラメータ）.

2 つ以上の router から使われるものだけを置く（1 router 専用のヘルパは各 router に残す）。
"""

from __future__ import annotations

import attrs
import httpx

from pcbasm.geometry import Point3d
from pcbasm.hal import KlipperClient
from pcbasm.pasting.labels import clean_position_label
from pcbasm.vision import load_focus_z
from web.api.control import ClientIdentity, LeaseInfo
from web.api.env import Env, resolve_machine_id
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.manager import JobManager
from web.api.preview import PreviewService
from web.api.state import AppState
from web.contract.discovery import API_VERSION
from web.contract.models import (
    ClientInfo,
    ControlInfo,
    JobBrief,
    KlipperStatus,
    MachineInfo,
    NozzleCleanInfo,
    Position,
    StateResponse,
)

# --------------------------------------------------------------------------- #
# Klipper の状態
# --------------------------------------------------------------------------- #


def fetch_status(klipper: KlipperClient) -> KlipperStatus:
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


# --------------------------------------------------------------------------- #
# アプリ状態レスポンス
# --------------------------------------------------------------------------- #


def control_payload(info: LeaseInfo) -> ControlInfo:
    """リーススナップショットを API 表現へ変換する（唯一の変換点）."""
    return ControlInfo(
        key=info.key,
        display_name=info.display_name,
        held=info.held,
        connections=info.connections,
    )


def nozzle_clean_payload(position: Point3d) -> NozzleCleanInfo:
    """クリーニング位置を API 表現へ変換する（唯一の変換点）.

    表示文字列は `pcbasm` が組んだものをそのまま詰める。
    """
    return NozzleCleanInfo(
        x=position.x, y=position.y, z=position.z, label=clean_position_label(position)
    )


def build_state_response(
    state: AppState,
    settings: Env,
    preview: PreviewService,
    jobs: JobManager,
    control: LeaseInfo,
    identity: ClientIdentity,
) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = state.busy_owner
    pcb = state.selected_pcb
    record = jobs.current()
    machine, config_error = state.machine_or_error()
    dispenser = None if machine is None else machine.paste_dispenser
    cap = None if dispenser is None else dispenser.nozzle_cap.position
    clean = None if dispenser is None else dispenser.nozzle_clean.position
    return StateResponse(
        control=control_payload(control),
        you=ClientInfo(key=identity.key),
        pcb_file=pcb.as_posix() if pcb else None,
        busy=owner is not None,
        busy_owner=owner,
        focus_z=(
            None if machine is None else load_focus_z(machine.camera.calibration_file)
        ),
        mainsail_url=settings.mainsail_url,
        preview_clients=preview.client_count,
        job=(
            JobBrief(id=record.id, name=record.name, status=record.status.value)
            if record is not None
            else None
        ),
        nozzle_cap=None if cap is None else Position(x=cap.x, y=cap.y, z=cap.z),
        nozzle_clean=None if clean is None else nozzle_clean_payload(clean),
        config_error=config_error,
        config_notice=state.config_notice,
        pcb_notice=state.pcb_notice,
    )


# --------------------------------------------------------------------------- #
# backend の自己申告（/api/machine-info と SSR が共有する解決結果）
# --------------------------------------------------------------------------- #


def _fb_start(settings: Env) -> str:
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


def build_machine_info(settings: Env) -> MachineInfo:
    """Backend の自己申告情報を組み立てる.

    ホスト名の解決といった環境依存の処理はここに集約する（pcbasm 層は machine.toml に
    書かれた値だけを返す）。``mainsail_url`` はリクエストのホスト名に依存させない
    （プロキシ配下で必ず誤るため）。
    """
    machine_id = resolve_machine_id(settings)
    # fake の機体は実機と同じ machine_id を名乗るので、実機の Mainsail へ導かない（空ならリンクを出さない）
    mainsail_url = (
        ""
        if settings.fake
        else settings.mainsail_url or _default_mainsail_url(machine_id)
    )
    return MachineInfo(
        machine_id=machine_id,
        mainsail_url=mainsail_url,
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
    （persisted_params 外・型不一致はエラーにせず除外 = spec 既定値のまま）。
    """
    saved = state.job_param_defaults(definition.name)
    if not saved or not definition.persisted_params:
        return definition.params
    valid = catalog.filter_persisted_defaults(definition, saved)
    return tuple(
        attrs.evolve(spec, default=valid[spec.name]) if spec.name in valid else spec
        for spec in definition.params
    )
