"""操作権リースの API（取得 / 解放 / 奪取 / 表示名更新）と WS への変更通知.

取得系はいずれも `GET /api/state` の ``control`` / ``you`` と同じ形を返す。奪取
（takeover）は「保持者がタブを閉じ忘れたまま帰った」等の詰みからの脱出口なので
操作権でゲートしない（クールダウンも置かない。誤操作で握った人から取り戻せなく
なる方が有害）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from web.api.control import ClientIdentity, LeaseInfo
from web.api.dependencies import IdentityDep, LeaseDep
from web.api.models import ClientInfo, ControlStateResponse
from web.api.routers.common import control_payload

router = APIRouter(prefix="/api/control")


def control_changed_event(info: LeaseInfo) -> dict[str, Any]:
    """WS の control_changed イベントを組む（全 subscriber へ同一 payload で配る）.

    受け取ったクライアントは ``control.key`` を自分の ``you.key`` と比べて「自分が
    保持者か」を判定する（サーバは接続ごとに payload を作り分けない）。
    """
    return {
        "type": "control_changed",
        "control": control_payload(info).model_dump(mode="json"),
    }


def _response(info: LeaseInfo, identity: ClientIdentity) -> ControlStateResponse:
    return ControlStateResponse(
        control=control_payload(info), you=ClientInfo(key=identity.key)
    )


@router.post("/acquire")
def post_acquire(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """操作権を取得する（423: 他クライアントが保持中）."""
    return _response(lease.claim(identity), identity)


@router.post("/release")
def post_release(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """自分の操作権を解放する（冪等。非保持者の呼び出しは無効果）."""
    return _response(lease.release(identity), identity)


@router.post("/takeover")
def post_takeover(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """保持者を問わず操作権を奪取する（詰みからの脱出口なのでゲートしない）.

    実行中のジョブには一切触らない（指示を出す権利の移転であって、走っている処理の移転ではない）。
    """
    return _response(lease.takeover(identity), identity)


@router.post("/name")
def post_name(lease: LeaseDep, identity: IdentityDep) -> ControlStateResponse:
    """表示名の変更をリースへ反映する（保持者でなければ現在の状態を返すだけ）.

    表示名はリクエストのヘッダ / cookie が正なので body を取らない。保持者による
    `claim` は取得ではなく表示名と無操作タイマーの更新として働く。
    """
    info = lease.snapshot()
    if info.key == identity.key:
        info = lease.claim(identity)
    return _response(info, identity)
