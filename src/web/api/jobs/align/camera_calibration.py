"""カメラキャリブレーションジョブ（チェッカーボードで pixel/mm を測り、校正ファイルを保存候補にする）."""

from __future__ import annotations

from datetime import datetime

import attrs

from pcbasm.hal import XYZStage
from pcbasm.vision import CheckerboardCalibrator
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import ApplyFile, ApplyPayload, JobResult

# Z 取得（best-effort）のタイムアウト [sec]
Z_QUERY_TIMEOUT = 5.0

_NO_FOCUS_Z_EFFECT = "この校正を適用すると、校正を使うジョブは開始時に止まります"


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="camera_calibration",
            label="カメラキャリブレーション",
            run=_run_camera_calibration,
            params=(
                ParamSpec(
                    "square_size", "チェッカーボードの1マス", "float", 1.5, unit="mm"
                ),
            ),
            persisted_params=("square_size",),
            uses_machine=True,
        )
    )


def _run_camera_calibration(ctx: JobContext) -> JobResult:
    """チェッカーボードで pixel/mm をキャリブレーションし JSON を保存候補にする."""
    square_size = float(ctx.params["square_size"])
    crop_size = ctx.machine.camera.crop.size
    calibrator = CheckerboardCalibrator(square_size, crop_size)

    with ctx.open_camera() as camera:
        ctx.progress("撮影待ち")
        while True:
            ctx.require("チェッカーボードを配置して撮影しますか?（いいえで中止）")
            image = camera.capture()
            ctx.frame(image)
            ctx.progress("検出")
            detection = calibrator.calibrate(image)
            if detection is not None:
                break
            ctx.log("チェッカーボードが検出できませんでした")
        result, vis = detection
        camera_name = camera.info.name

    ctx.frame(vis)
    ctx.log(f"pixel/mm: {result.pixel_per_mm:.2f}")
    ctx.log(f"1マスの距離の標準偏差: {result.std_distance_px:.2f} px")

    # Z 位置は best-effort（Klipper 不通でも pixel/mm は記録として残す）。
    # ただし焦点 Z の無い校正を適用すると、校正を使うジョブは開始時に止まる
    z: float | None = None
    try:
        z = XYZStage(ctx.klipper(Z_QUERY_TIMEOUT)).get_position().z
    except Exception as exc:
        ctx.log(
            f"Z 位置の取得に失敗しました（z_position なしで保存します。"
            f"{_NO_FOCUS_Z_EFFECT}）: {exc}"
        )
    result = attrs.evolve(result, z_position=z)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ジョブ固有の成果物ディレクトリ名も付け、同じ秒の再校正で現行ファイルを上書きしない。
    stem = f"{camera_name}_{timestamp}_{ctx.artifacts_dir.name}"
    filename = f"{stem}.json"
    png_name = f"{stem}.png"
    vis.save(ctx.artifacts_dir / png_name)
    result.save(ctx.artifacts_dir / filename)
    json_bytes = (ctx.artifacts_dir / filename).read_bytes()
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=(
            f"pixel/mm: {result.pixel_per_mm:.2f}"
            f" / σ: {result.std_distance_px:.2f} px"
            f" / Z: {z if z is not None else f'未取得（{_NO_FOCUS_Z_EFFECT}）'}"
        ),
        artifacts=(
            ctx.artifact("コーナー検出", png_name, "image"),
            ctx.artifact("キャリブレーション JSON", filename, "file"),
        ),
        apply=ApplyPayload(
            label=f"{filename} を保存し [camera].calibration_file に設定",
            values={"camera.calibration_file": filename},
            files=(ApplyFile(filename, json_bytes),),
        ),
    )
