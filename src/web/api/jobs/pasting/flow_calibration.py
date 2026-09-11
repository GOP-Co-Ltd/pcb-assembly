"""はんだ塗布の塗布パス直前に走る運転時流量キャリブレーション.

既知量のドットを基板上へ並べて塗り、塗布前後画像から推定した体積との比で
``rotations_per_ul`` を補正する。
点の配置と補正の算出は :mod:`pcbasm.pasting.paste_volume.runtime` にあり、ここは
撮影・塗布・ログだけを担う。

**補正できないことでジョブを落とさない。** この時点で基板の位置合わせと高さ計測が
済んでおり、後から足した補正が失敗したせいで塗布そのものを取りやめるのは割に合わない。
校正ファイルが読めない・検出できない・被覆域の外だった、のいずれも警告に留めて
``machine.toml`` の係数のまま塗る。

位置合わせ成功領域が 1 つも無いような構造的な失敗はここでは握らない。

その状態では直後の pad 塗布も同じ例外で落ちるので、隠しても先へ進めない。
"""

from __future__ import annotations

from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.capture import PointCapturer
from pcbasm.pasting.paste_volume.estimator import (
    PasteVolumePrediction,
    load_diameter_estimator,
)
from pcbasm.pasting.paste_volume.runtime import (
    FlowCalibrationOutcome,
    FlowCalibrationPlan,
    correct_rotations_per_ul,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.vision.crop import RectCrop, crop_pixel_size
from web.api.jobs.context import JobContext


def run_flow_calibration(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    plan: FlowCalibrationPlan,
) -> FlowCalibrationOutcome | None:
    """測定点を塗って撮り、``rotations_per_ul`` の補正値を返す（不可なら ``None``）.

    撮影は収集ジョブと同じ 3 パス（全点 pre → 全点塗布 → 全点 post）で行う。
    塗ってすぐ撮ると点ごとにペーストの落ち着き時間が変わる。

    校正を作ったときと同じ並びに揃える。
    """
    path = ctx.paste_volume_calibration_dir / plan.calibration_file
    estimator, error = load_diameter_estimator(path, reliable_range_only=True)
    if estimator is None:
        ctx.log(f"流量キャリブレーション: 校正を読めないので補正しません: {error}")
        return None

    pixel_per_mm = session.calibration.pixel_per_mm
    crop_size_px, error = crop_pixel_size(plan.crop_size_mm, pixel_per_mm)
    if crop_size_px is None:
        ctx.log(f"流量キャリブレーション: crop 寸法を決められません: {error}")
        return None

    params = applicator.default_params
    for mismatch in estimator.calibration.conditions.mismatches(
        nozzle_diameter_mm=ctx.machine.paste_dispenser.nozzle_diameter,
        paste_height_mm=params.paste_height_mm,
        pixel_per_mm=pixel_per_mm,
        crop_size_mm=plan.crop_size_mm,
    ):
        ctx.log(f"流量キャリブレーション: 校正の条件と違います（続行）: {mismatch}")

    ctx.progress("流量キャリブレーション")
    ctx.log(
        f"流量キャリブレーション: {len(plan.points)} 点 x {plan.amount_ul:.3f} uL / "
        f"crop {crop_size_px} px / 校正 {plan.calibration_file}"
    )
    capturer = PointCapturer(session, crop_size_px=crop_size_px, frame_sink=ctx.frame)

    pre = _capture_all(ctx, capturer, correction, plan, phase="塗布前")
    if pre is None:
        return None

    for index, point in enumerate(plan.points):
        ctx.checkpoint()
        applicator.deposit_at(
            point,
            amount_ul=plan.amount_ul,
            transform=session.point_transform(point, correction),
        )
        ctx.log(f"流量キャリブレーション: 点{index} を塗布しました")

    post = _capture_all(ctx, capturer, correction, plan, phase="塗布後")
    if post is None:
        return None

    predictions = [
        estimator.predict(before.image, after.image, pixel_per_mm=pixel_per_mm)
        for before, after in zip(pre, post, strict=True)
    ]
    _log_predictions(ctx, predictions)

    outcome, error = correct_rotations_per_ul(
        predictions,
        amount_ul=plan.amount_ul,
        rotations_per_ul=applicator.rotations_per_ul,
    )
    if outcome is None:
        ctx.log(f"流量キャリブレーション: 補正しません: {error}")
        return None
    ctx.log(summary_line(outcome))
    if outcome.clamped:
        ctx.log(
            "流量キャリブレーション: 補正量が 1/3〜3 倍の上限に当たりました。"
            "校正の条件やノズルの状態を確認してください"
        )
    return outcome


def summary_line(outcome: FlowCalibrationOutcome) -> str:
    """補正結果の 1 行表示（ログと JobResult.summary で共用）."""
    return (
        f"流量キャリブレーション: {outcome.accepted_count} 点採用 / "
        f"推定 {outcome.estimated_ul:.3f} uL / 指令 {outcome.commanded_ul:.3f} uL / "
        f"比 {outcome.ratio:.3f} / rotations_per_ul "
        f"{outcome.previous_rotations_per_ul:.4f} → {outcome.rotations_per_ul:.4f}"
        + ("（上限で頭打ち）" if outcome.clamped else "")
    )


def _capture_all(
    ctx: JobContext,
    capturer: PointCapturer,
    correction: PasteCorrection,
    plan: FlowCalibrationPlan,
    *,
    phase: str,
) -> list[RectCrop] | None:
    """全測定点を 1 パスで撮る（1 点でも撮れなければ補正を諦める）."""
    crops: list[RectCrop] = []
    for index, point in enumerate(plan.points):
        ctx.checkpoint()
        crop, error = capturer.capture(
            point, correction=correction.alignment.correction_for(point)
        )
        if crop is None:
            ctx.log(
                f"流量キャリブレーション: 点{index} の{phase}撮影に失敗したので"
                f"補正しません: {error}"
            )
            return None
        crops.append(crop)
    return crops


def _log_predictions(ctx: JobContext, predictions: list[PasteVolumePrediction]) -> None:
    """点ごとの推定結果をログへ 1 行ずつ出す."""
    for index, prediction in enumerate(predictions):
        if prediction.accepted:
            ctx.log(
                f"流量キャリブレーション: 点{index} 推定 "
                f"{prediction.mean_volume_ul:.4f} uL"
            )
        else:
            ctx.log(
                f"流量キャリブレーション: 点{index} 不採用 "
                f"({prediction.rejection_reason})"
            )
