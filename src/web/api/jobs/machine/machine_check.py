"""装置診断ジョブ（読み取りだけで、装置は動かさない）."""

from __future__ import annotations

from pcbasm.machine.diagnostics import check_audio, check_camera, check_klipper
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext, JobResult

STATUS_TIMEOUT = 10.0


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="machine_check",
            label="装置診断",
            run=_run_machine_check,
            uses_machine=False,
        )
    )


def _run_machine_check(ctx: JobContext) -> JobResult:
    """全項目を確かめてログに並べ、1 つでも不合格ならジョブを失敗にする."""
    ctx.progress("診断")
    player = ctx.audio_player
    checks = [
        *check_klipper(ctx.klipper(STATUS_TIMEOUT), ctx.machine),
        check_camera(ctx.open_camera, ctx.machine.camera.calibration_file),
        check_audio(
            player.list_devices() if player is not None else (),
            ctx.machine.audio.device,
        ),
    ]
    for check in checks:
        ctx.log(f"{'ok' if check.ok else 'NG'}  {check.name}: {check.detail}")
    failed = [check.name for check in checks if not check.ok]
    if failed:
        raise RuntimeError(f"{len(failed)} 項目が不合格です: {'、'.join(failed)}")
    ctx.progress("完了", 100.0)
    return JobResult(summary=f"全 {len(checks)} 項目 ok")
