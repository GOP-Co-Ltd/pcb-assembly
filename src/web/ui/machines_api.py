"""``GET /api/machines`` — マシン登録一覧の同期ダンプ.

`web.ui.pages` の ``/{tab}`` キャッチオールに食われないよう、アプリでは
`pages.router` より**先に**登録する（`web.ui.app.create_app`）。

mDNS で一覧が増減しても、再読み込みせずにドロップダウンを組み替えるために置く。

呼び出し元は ``static/js/machine_selector.js``。

frontend 自身の JSON は他に `web.ui.update_api` にもある。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from web.ui.machines import MachineEndpoint, MachineRegistry
from web.ui.models import MachinesResponse, MachineSummary

router = APIRouter(prefix="/api")


def _summary(endpoint: MachineEndpoint, current: str | None) -> MachineSummary:
    return MachineSummary(
        machine_id=endpoint.machine_id,
        # 表示名はサーバが組む（表示規則を JS に複製しない）
        label=endpoint.label,
        name=endpoint.name,
        host=endpoint.host,
        port=endpoint.port,
        machine_type=endpoint.machine_type,
        source=endpoint.source,
        current=endpoint.machine_id == current,
    )


@router.get("/machines")
def list_machines(request: Request, current: str | None = None) -> MachinesResponse:
    """既知マシンの一覧（静的登録順 → mDNS 発見順）.

    Args:
        request: レジストリを持つアプリへの参照
        current: 表示中のマシン（``/m/{machine_id}``）。一致する行の ``current`` が
            True になる。判定をサーバでやるのは、選択状態の真実をサーバ側に
            寄せるため（JS は返り値をそのまま反映する）

    Returns:
        マシン一覧
    """
    registry: MachineRegistry = request.app.state.registry
    return MachinesResponse(
        machines=[_summary(endpoint, current) for endpoint in registry.list()]
    )
