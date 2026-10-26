"""ノズルクリーニングのテストジョブ（塗布ジョブと同じクリーニングを 1 回だけ行う）.

押し込み量とこすり幅は結果を見て追い込む値なので、塗布を 1 枚通さずに効き具合を確かめるために使う。
"""

from __future__ import annotations

from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.nozzle_clean import clean_nozzle, plan_nozzle_clean
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult

# パージ（retract_rate での押出）と十字往復の M400 待ちを含むので長め
CLEAN_TIMEOUT = 90.0


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="nozzle_clean_test",
            label="ノズルクリーニングのテスト",
            run=_run_nozzle_clean_test,
            uses_machine=True,
        )
    )


def _run_nozzle_clean_test(ctx: JobContext) -> JobResult:
    """記録済みの位置と動作設定で、prime → クリーニング → リトラクトを 1 回行う.

    塗布ジョブはリトラクトして終わったパージの後に prime して掃除するので、同じ順序にしないと同じ設定で出る量が食い違う。
    キャップへは終了時の駐機で戻る。
    """
    solder = ctx.solder
    clean = solder.nozzle.clean
    if clean.position is None:
        raise ValueError("ノズルクリーニング位置が未記録です")
    klipper = ctx.klipper(CLEAN_TIMEOUT)
    stage = XYZStage(klipper)
    # 可動域外ならここで落ちるので、装置は動かずパージも出ない
    plan = plan_nozzle_clean(stage, clean.position, clean)

    # 前回のテストは終了時の駐機で脱力しているので、続けて試せるようにホーミングし直す
    if not stage.is_homed():
        ctx.progress("ホーミング")
        ctx.log("全軸ホーミングを実行します")
        klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
    ctx.checkpoint()

    ctx.progress("クリーニング")
    with build_applicator(klipper, stage, solder) as applicator:
        applicator.prime()
        clean_nozzle(klipper, applicator, plan, log=ctx.log)
        applicator.retract()
    return JobResult(summary="クリーニングを 1 回行いました")
