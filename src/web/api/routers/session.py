"""セッションの API（アプリ状態・backend の自己申告・操作権リース）.

操作権の奪取（takeover）は「保持者がタブを閉じ忘れたまま帰った」等で誰も操作できなくなった状態から抜け出す手段なので
操作権でゲートしない（クールダウンも置かない。誤操作で握った人から取り戻せなく
なる方が有害）。
"""

from __future__ import annotations

from fastapi import APIRouter

from pcbasm.geometry import Point3d
from pcbasm.machine.labels import position_label
from pcbasm.pasting.labels import clean_position_label
from pcbasm.vision import load_focus_z
from web.api.backend import Backend
from web.api.control import ClientIdentity, LeaseInfo, control_payload
from web.api.deps import BackendDep, EnvDep, IdentityDep, LeaseDep
from web.api.env import Env, resolve_machine_id
from web.contract.discovery import API_VERSION
from web.contract.models import (
    ControlStateResponse,
    JobBrief,
    MachineInfo,
    NozzlePositionInfo,
    StateResponse,
)

router = APIRouter(prefix="/api/session")


def _nozzle_position_payload(position: Point3d, label: str) -> NozzlePositionInfo:
    """記録したノズル保守位置を API 表現へ変換する（表示文字列は ``pcbasm`` が組んだもの）."""
    return NozzlePositionInfo(x=position.x, y=position.y, z=position.z, label=label)


def nozzle_cap_payload(position: Point3d) -> NozzlePositionInfo:
    """キャップ位置の API 表現（唯一の変換点）."""
    return _nozzle_position_payload(position, position_label(position))


def nozzle_clean_payload(position: Point3d) -> NozzlePositionInfo:
    """クリーニング位置の API 表現（唯一の変換点）."""
    return _nozzle_position_payload(position, clean_position_label(position))


def build_state_response(backend: Backend, identity: ClientIdentity) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = backend.lock.owner
    pcb = backend.selection.pcb
    record = backend.jobs.current()
    machine, config_error = backend.machine_config.machine_or_error()
    nozzle = None if machine is None else machine.solder.nozzle
    cap = None if nozzle is None else nozzle.cap.position
    clean = None if nozzle is None else nozzle.clean.position
    return StateResponse(
        control=control_payload(backend.control.snapshot(), identity),
        pcb_file=pcb.as_posix() if pcb else None,
        busy=owner is not None,
        busy_owner=owner,
        focus_z=(
            None if machine is None else load_focus_z(machine.camera.calibration_file)
        ),
        mainsail_url=_mainsail_url(backend.env),
        preview_clients=backend.camera.client_count,
        job=(
            JobBrief(id=record.id, name=record.name, status=record.status.value)
            if record is not None
            else None
        ),
        nozzle_cap=None if cap is None else nozzle_cap_payload(cap),
        nozzle_clean=None if clean is None else nozzle_clean_payload(clean),
        config_error=config_error,
        config_notice=backend.machine_config.notice,
        pcb_notice=backend.selection.notice,
    )


def _fb_start(env: Env) -> str:
    """ファイルブラウザの初期表示パス（pcb_browse_root からの相対）."""
    try:
        start = (
            env.pcb_browse_start.resolve()
            .relative_to(env.pcb_browse_root.resolve())
            .as_posix()
        )
    except ValueError:
        return ""
    return "" if start == "." else start


def _mainsail_url(env: Env) -> str:
    """この機体の Mainsail の URL（fake の機体は空で、リンクを出さない）.

    ``machine_id`` は短いホスト名（``socket.gethostname()``）なので、LAN の他端末
    からは mDNS 経由の ``*.local`` しか引けない。裸のホスト名を返すとリモートから
    Mainsail を開けなくなるため ``.local`` を付ける。既にドットを含む
    （FQDN や ``.local`` 付きが注入された）場合は重ねない。
    fake の機体は実機と同じ machine_id を名乗るので、実機の Mainsail へ導かない。
    """
    if env.fake:
        return ""
    machine_id = resolve_machine_id(env)
    host = machine_id if "." in machine_id else f"{machine_id}.local"
    return f"http://{host}"


def _control_response(
    info: LeaseInfo, identity: ClientIdentity
) -> ControlStateResponse:
    return ControlStateResponse(control=control_payload(info, identity))


@router.get("/state")
def get_state(backend: BackendDep, identity: IdentityDep) -> StateResponse:
    """選択 PCB・装置の使用状況・直近ジョブ・操作権などのアプリ状態を返す.

    閲覧は自由なので操作権でゲートしない。
    ``control.state`` はリクエスト元から見た操作権の状態（held / viewer / free）。
    """
    return build_state_response(backend, identity)


@router.get("/machine-info")
def get_machine_info(env: EnvDep) -> MachineInfo:
    """Backend が自己申告する装置情報を返す（到達性プローブ兼用）.

    ホスト名の解決といった環境依存の処理はここに集約する（pcbasm 層は machine.toml に
    書かれた値だけを返す）。``mainsail_url`` はリクエストのホスト名に依存させない
    （プロキシ配下で必ず誤るため）。
    """
    return MachineInfo(
        machine_id=resolve_machine_id(env),
        mainsail_url=_mainsail_url(env),
        fb_start=_fb_start(env),
        api_version=API_VERSION,
    )


@router.post("/control/acquire")
def post_acquire(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """操作権を取得する（423: 他クライアントが保持中）."""
    return _control_response(lease.claim(identity), identity)


@router.post("/control/release")
def post_release(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """自分の操作権を解放する（冪等。非保持者の呼び出しは無効果）."""
    return _control_response(lease.release(identity), identity)


@router.post("/control/takeover")
def post_takeover(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """保持者を問わず操作権を奪取する（行き詰まりから抜け出す手段なのでゲートしない）.

    実行中のジョブには一切触らない（指示を出す権利の移転であって、走っている処理の移転ではない）。
    """
    return _control_response(lease.takeover(identity), identity)


@router.post("/control/name")
def post_name(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """表示名の変更をリースへ反映する（保持者でなければ現在の状態を返すだけ）.

    表示名はリクエストのヘッダ / cookie が正なので body を取らない。保持者による
    `claim` は取得ではなく表示名の更新として働く。
    """
    info = lease.snapshot()
    if info.key == identity.key:
        info = lease.claim(identity)
    return _control_response(info, identity)
