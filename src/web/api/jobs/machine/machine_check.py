"""装置診断ジョブ（読み取りだけで、装置は動かさない）."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from pcbasm.machine.diagnostics import (
    check_audio,
    check_camera,
    check_klipper,
    save_checks,
)
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult

STATUS_TIMEOUT = 10.0


def register(catalog: JobCatalog, checks_file: Path) -> None:
    """``checks_file`` に直近の結果を残す装置診断ジョブを登録する（立ち上げ状況が読む）."""
    catalog.register(
        JobDefinition(
            name="machine_check",
            label="装置診断",
            run=partial(_run_machine_check, checks_file=checks_file),
            uses_machine=False,
        )
    )


def _run_machine_check(ctx: JobContext, *, checks_file: Path) -> JobResult:
    """全項目を確かめてログに並べ、1 つでも不合格ならジョブを失敗にする."""
    ctx.progress("診断")
    checks = [
        *check_klipper(ctx.klipper(STATUS_TIMEOUT), ctx.machine),
        check_camera(ctx.open_camera, ctx.machine.camera.calibration_file),
        check_audio(ctx.devices.audio.list_devices(), ctx.machine.audio.device),
    ]
    for check in checks:
        ctx.log(f"{'ok' if check.ok else 'NG'}  {check.name}: {check.detail}")
    save_checks(checks_file, checks)
    failed = [check.name for check in checks if not check.ok]
    if failed:
        raise RuntimeError(f"{len(failed)} 項目が不合格です: {'、'.join(failed)}")
    ctx.progress("完了", 100.0)
    return JobResult(summary=f"全 {len(checks)} 項目 ok")
