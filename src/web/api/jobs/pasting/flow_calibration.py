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

例外は「どの測定点にも塗布が写らない」場合だけ。これは補正の失敗ではなくノズルが
詰まっている状態なので、先端を掃除してパージし直し、もう一度だけ測る。それでも写ら
なければ塗布へ進まずに落とす（:func:`run_flow_calibration_with_cleaning`）。
"""

from __future__ import annotations

import time

import attrs

from pcbasm.config import NozzleClean
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.capture import PointCapturer
from pcbasm.pasting.initial_purge import ResolvedInitialPurge
from pcbasm.pasting.nozzle_clean import clean_nozzle
from pcbasm.pasting.paste_volume.estimator import (
    PasteVolumePrediction,
    load_diameter_estimator,
)
from pcbasm.pasting.paste_volume.runtime import (
    FlowCalibrationOutcome,
    FlowCalibrationPlan,
    correct_rotations_per_ul,
    no_deposit_detected,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.vision.crop import RectCrop, crop_pixel_size
from web.api.jobs.context import JobContext

# 静定待ちを刻む間隔 [秒]。この粒度で中断を拾い、残り時間をログへ出す
_SETTLE_TICK_SEC = 1.0


@attrs.frozen
class FlowCalibrationRun:
    """1 回ぶんの実行結果.

    Attributes:
        outcome: 補正値（補正できなければ ``None``）
        no_deposit: 全測定点で塗布が写らなかったか（ノズル詰まりの疑い）。
            測れなかった場合（校正が読めない・撮影に失敗した）は ``False``。
            測れていないことは「塗れていない」ことの証拠にならない
    """

    outcome: FlowCalibrationOutcome | None
    no_deposit: bool


_NOT_MEASURED = FlowCalibrationRun(outcome=None, no_deposit=False)


def run_flow_calibration(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    plan: FlowCalibrationPlan,
) -> FlowCalibrationRun:
    """測定点を塗って撮り、``rotations_per_ul`` の補正値を求める.

    撮影は収集ジョブと同じ 3 パス（全点 pre → 全点塗布 → 全点 post）で行う。
    塗ってすぐ撮ると点ごとにペーストの落ち着き時間が変わる。

    校正を作ったときと同じ並びに揃える。

    塗布パスと塗布後パスの間に ``settle_seconds`` の静定待ちを置く。
    点数が少ないと塗布パスがすぐ終わるので、待たないと広がりきる前の円を測る。
    """
    path = ctx.paste_volume_calibration_dir / plan.calibration_file
    estimator, error = load_diameter_estimator(path, reliable_range_only=True)
    if estimator is None:
        ctx.log(f"流量キャリブレーション: 校正を読めないので補正しません: {error}")
        return _NOT_MEASURED

    pixel_per_mm = session.calibration.pixel_per_mm
    crop_size_px, error = crop_pixel_size(plan.crop_size_mm, pixel_per_mm)
    if crop_size_px is None:
        ctx.log(f"流量キャリブレーション: crop 寸法を決められません: {error}")
        return _NOT_MEASURED

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
        f"crop {crop_size_px} px / 静定待ち {plan.settle_seconds:.1f} s / "
        f"校正 {plan.calibration_file}"
    )
    capturer = PointCapturer(session, crop_size_px=crop_size_px, frame_sink=ctx.frame)

    pre = _capture_all(ctx, capturer, correction, plan, phase="塗布前")
    if pre is None:
        return _NOT_MEASURED

    for index, point in enumerate(plan.points):
        ctx.checkpoint()
        applicator.deposit_at(
            point,
            amount_ul=plan.amount_ul,
            transform=session.point_transform(point, correction),
        )
        ctx.log(f"流量キャリブレーション: 点{index} を塗布しました")

    _wait_to_settle(ctx, plan.settle_seconds)

    post = _capture_all(ctx, capturer, correction, plan, phase="塗布後")
    if post is None:
        return _NOT_MEASURED

    predictions = [
        estimator.predict(before.image, after.image, pixel_per_mm=pixel_per_mm)
        for before, after in zip(pre, post, strict=True)
    ]
    _log_predictions(ctx, predictions)
    no_deposit = no_deposit_detected(predictions)

    outcome, error = correct_rotations_per_ul(
        predictions,
        amount_ul=plan.amount_ul,
        rotations_per_ul=applicator.rotations_per_ul,
    )
    if outcome is None:
        ctx.log(f"流量キャリブレーション: 補正しません: {error}")
        return FlowCalibrationRun(outcome=None, no_deposit=no_deposit)
    ctx.log(summary_line(outcome))
    if outcome.clamped:
        ctx.log(
            "流量キャリブレーション: 補正量が 1/3〜3 倍の上限に当たりました。"
            "校正の条件やノズルの状態を確認してください"
        )
    return FlowCalibrationRun(outcome=outcome, no_deposit=no_deposit)


def run_flow_calibration_with_cleaning(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    plan: FlowCalibrationPlan,
    *,
    nozzle_clean: NozzleClean | None,
    purge: ResolvedInitialPurge | None,
) -> FlowCalibrationOutcome | None:
    """流量を測り、塗布が 1 点も写らなければ掃除とパージをして 1 回だけやり直す.

    全点で塗布が写らないのはノズルが詰まっている状態なので、先端をクリーニングして
    パージし直してからもう一度測る。やり直しは 1 回だけで、それでも写らなければ
    詰まりが解けていないとみなして例外にする。写らないまま pad を塗っても基板を
    1 枚無駄にするだけなので、ここで止める。

    クリーニング位置が未記録なら、こすりは飛ばしてパージのやり直しだけを行う。
    詰まりはパージだけで抜けることもあり、位置の教示を塗布の前提にはしない。

    やり直しは 1 回目と同じ測定点へ塗る。基板側で決めた点以外に塗ってよい場所が無い
    ためで、1 回目が実際には吐出できていた（検出だけ失敗した）場合、2 回目の推定は
    増分ぶんになり過小に出る。補正量は 1/3〜3 倍で頭打ちになるので暴れはしないが、
    実機では 2 回目のログを見て判断する。

    Args:
        ctx: ジョブ文脈（進捗・ログ・中断）
        session: 計測済みの塗布セッション
        correction: 位置合わせと高さ面の補正
        applicator: 塗布に使うディスペンサー（有効化済み）
        plan: 測定点と条件
        nozzle_clean: クリーニング設定（未記録なら ``None``）
        purge: やり直し時のパージ（無効なら ``None``）

    Returns:
        補正値（補正できなければ ``None``）

    Raises:
        ValueError: クリーニングとパージの後も塗布を検出できない場合
    """
    run = run_flow_calibration(ctx, session, correction, applicator, plan)
    if not run.no_deposit:
        return run.outcome

    ctx.log(
        "流量キャリブレーション: どの測定点にも塗布が写りません。"
        "ノズルをクリーニングしてやり直します"
    )
    performed = _clean_and_purge(
        ctx, session, correction, applicator, nozzle_clean=nozzle_clean, purge=purge
    )

    run = run_flow_calibration(ctx, session, correction, applicator, plan)
    if run.no_deposit:
        raise ValueError(
            (
                f"{performed}の後も"
                if performed
                else "クリーニング位置もパージも未設定で"
            )
            + "塗布を検出できません。ノズルの詰まりとペースト残量を確認してください"
        )
    return run.outcome


def _clean_and_purge(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    *,
    nozzle_clean: NozzleClean | None,
    purge: ResolvedInitialPurge | None,
) -> str:
    """ノズル先端を掃除し、パージし直す（実施した処置の名前を返す）.

    ここへ来る時点で直前の :meth:`PasteApplicator.deposit_at` がリトラクトして終わって
    いる。掃除のパージはその引き込みを埋めるだけで終わらないよう prime してから行い、
    基板へ移る間の垂れを止めるためにまた引き戻す。掃除しないなら引き込んだままでよく、
    続くパージの :meth:`PasteApplicator.deposit_at` が自分で prime する。
    """
    performed: list[str] = []
    if nozzle_clean is not None:
        ctx.progress("ノズルクリーニング")
        ctx.checkpoint()
        applicator.prime()
        clean_nozzle(
            session.klipper, session.stage, applicator, nozzle_clean, log=ctx.log
        )
        applicator.retract()
        performed.append("クリーニング")
    else:
        ctx.log("ノズルクリーニング: 位置が未記録のためこすらずに進みます")

    if purge is not None:
        ctx.progress("パージやり直し")
        ctx.checkpoint()
        applicator.deposit_at(
            purge.point,
            amount_ul=purge.amount_ul,
            transform=session.point_transform(purge.point, correction),
        )
        ctx.log(f"パージやり直し: {purge.label} に {purge.amount_ul:.3f} uL")
        performed.append("パージ")
    else:
        ctx.log("パージやり直し: 初回パージが無効なので行いません")

    return "と".join(performed) if performed else ""


def summary_line(outcome: FlowCalibrationOutcome) -> str:
    """補正結果の 1 行表示（ログと JobResult.summary で共用）."""
    return (
        f"流量キャリブレーション: {outcome.accepted_count} 点採用 / "
        f"推定 {outcome.estimated_ul:.3f} uL / 指令 {outcome.commanded_ul:.3f} uL / "
        f"比 {outcome.ratio:.3f} / rotations_per_ul "
        f"{outcome.previous_rotations_per_ul:.4f} → {outcome.rotations_per_ul:.4f}"
        + ("（上限で頭打ち）" if outcome.clamped else "")
    )


def _wait_to_settle(ctx: JobContext, seconds: float) -> None:
    """ペーストが広がりきるまで待つ.

    塗り終えてすぐ撮ると、広がる前の小さい円を測ることになる。

    待ちは中断できるよう刻んで進める。
    """
    if seconds <= 0:
        return
    ctx.progress(f"流量キャリブレーション: 静定待ち {seconds:.0f} 秒")
    ctx.log(f"流量キャリブレーション: ペーストの静定を {seconds:.1f} 秒待ちます")
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        ctx.checkpoint()
        time.sleep(min(_SETTLE_TICK_SEC, remaining))
    ctx.checkpoint()


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
