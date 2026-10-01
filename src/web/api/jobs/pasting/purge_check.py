"""初回パージが実際に出たかを画像で確かめ、出ていなければノズルを掃除してやり直す.

ノズル先端で固まったペーストは、指令どおり押し出しても何も出ない状態を作る。
そのまま pad を塗り始めると基板を 1 枚無駄にするので、パージした点を塗布前後で
撮り、ペーストが写らなければ先端を掃除してもう一度だけパージする。

判定は塗布量データセットと同じ :func:`~pcbasm.pasting.paste_volume.detect.measure_dot`
で行うが、見るのは「写ったか」だけで体積は推定しない。校正ファイルを必要としないので、
流量キャリブレーションを使わない機体でも同じように確かめられる。

**確かめられなくてもジョブを失敗させない。** カメラが撮影位置へ届かない・撮影や計測に
失敗した、のいずれも確かめずに先へ進む。写らなかったと断定できたときだけ掃除し、
掃除しても写らなければジョブを失敗させる。
"""

from __future__ import annotations

from pcbasm.config import NozzleClean
from pcbasm.geometry import Point2d, Transform
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.capture import PointCapturer
from pcbasm.pasting.initial_purge import ResolvedInitialPurge
from pcbasm.pasting.nozzle_clean import clean_nozzle
from pcbasm.pasting.paste_volume.detect import measure_dot
from pcbasm.pasting.session import PasteSession
from pcbasm.vision.crop import RectCrop, crop_pixel_size
from web.api.jobs.context import JobContext

# パージ点を切り出す一辺 [mm]。流量キャリブレーションの crop と同じ広さを既定にする。
# 広く取るほど感度が下がる（blank ガードが crop 全体の 99 パーセンタイルなので、
# ドットが crop 面積の 1 % ほどを占めないと検出できない）。ここは「写ったか」しか
# 見ないので、大きなパージが crop からはみ出して直径が頭打ちになっても困らない。
PURGE_CROP_SIZE_MM = 2.0


def purge_with_cleaning(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    purge: ResolvedInitialPurge,
    *,
    nozzle_clean: NozzleClean | None,
) -> None:
    """初回パージを実行し、写らなければ掃除してもう一度だけパージする.

    掃除とやり直しは 1 回だけ。2 回目も写らなければ詰まりが解けていないとみなして
    例外にする。クリーニング位置が未記録なら、こすりは飛ばしてパージのやり直しだけを
    行う（詰まりはパージだけで抜けることもあり、位置の教示を塗布の前提にしない）。

    Args:
        ctx: ジョブ文脈（進捗・ログ・中断）
        session: 計測済みの塗布セッション
        correction: 位置合わせと高さ面の補正
        applicator: 塗布に使うディスペンサー（有効化済み）
        purge: パージ点と量
        nozzle_clean: クリーニング設定（未記録なら ``None``）

    Raises:
        ValueError: 掃除してパージし直しても塗布が写らない場合
    """
    point_correction = correction.alignment.correction_for(purge.point)
    capturer, error = _make_capturer(ctx, session, purge.point, point_correction)
    if capturer is None:
        ctx.log(f"パージ確認: {error}ので確かめずに進みます")
        _purge(ctx, session, correction, applicator, purge)
        return

    ctx.checkpoint()
    baseline, error = capturer.capture(purge.point, correction=point_correction)
    if baseline is None:
        ctx.log(f"パージ確認: 塗布前の撮影に失敗したので確かめません: {error}")
        _purge(ctx, session, correction, applicator, purge)
        return

    # None は「確かめられなかった」。写らなかったと断定できたときだけ掃除する
    if (
        _purge_and_detect(
            ctx, session, correction, applicator, capturer, purge, baseline
        )
        is not False
    ):
        return

    ctx.log("パージ確認: ペーストが写りません。ノズルをクリーニングしてやり直します")
    performed = _clean(ctx, session, applicator, nozzle_clean)

    ctx.progress("パージやり直し")
    # やり直しも 1 回目と同じ塗布前画像と比べる。2 回目の直前を基準にすると、1 回目が
    # 実は出ていて検出だけ失敗した場合に増分しか見えず、正常なノズルを詰まりと断じる
    if (
        _purge_and_detect(
            ctx, session, correction, applicator, capturer, purge, baseline
        )
        is False
    ):
        raise ValueError(
            f"{performed}の後もパージが写りません。"
            "ノズルの詰まりとペースト残量を確認してください"
        )


def _make_capturer(
    ctx: JobContext,
    session: PasteSession,
    point: Point2d,
    point_correction: Transform | None,
) -> tuple[PointCapturer | None, str | None]:
    """パージ点の撮影器を組む（撮れないなら理由を返す）."""
    crop_size_px, error = crop_pixel_size(
        PURGE_CROP_SIZE_MM, session.calibration.pixel_per_mm
    )
    if crop_size_px is None:
        return None, f"crop 寸法を決められない（{error}）"
    if error := _camera_reach_error(session, point, point_correction):
        return None, error
    return PointCapturer(session, crop_size_px=crop_size_px, frame_sink=ctx.frame), None


def _camera_reach_error(
    session: PasteSession, point: Point2d, point_correction: Transform | None
) -> str | None:
    """カメラを撮影位置へ置けるか（置けないなら理由、置けるなら ``None``）.

    カメラはツールヘッドオフセットぶんノズルとずれる。

    ノズルが届くパージ点でもカメラが届かない機体構成があり得る。

    撮影で可動域外の例外を出して塗布ジョブを失敗させるより、確かめずに進むほうがよい。
    """
    target = session.camera_point_target(point, correction=point_correction)
    limits = session.stage.limits
    axes = [("x", target.x, limits.x), ("y", target.y, limits.y)]
    # ピント Z が未記録のキャリブレーションでは Z を動かさないので見なくてよい
    if (focus_z := session.calibration.z_position) is not None:
        axes.append(("z", focus_z, limits.z))
    outside = [f"{name}={value}" for name, value, scalar in axes if value not in scalar]
    if outside:
        return f"カメラを撮影位置へ置けない（可動域外: {', '.join(outside)}）"
    return None


def _purge_and_detect(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    capturer: PointCapturer,
    purge: ResolvedInitialPurge,
    baseline: RectCrop,
) -> bool | None:
    """パージして塗布後を撮り、``baseline`` との差分で写ったかを見る.

    確かめられなかった（撮影・計測に失敗した）ときは ``None`` を返す。
    """
    _purge(ctx, session, correction, applicator, purge)

    ctx.checkpoint()
    post, error = capturer.capture(
        purge.point, correction=correction.alignment.correction_for(purge.point)
    )
    if post is None:
        ctx.log(f"パージ確認: 塗布後の撮影に失敗したので確かめません: {error}")
        return None

    measurement, error = measure_dot(
        baseline.image, post.image, pixel_per_mm=session.calibration.pixel_per_mm
    )
    if measurement is None:
        ctx.log(f"パージ確認: 計測できないので確かめません: {error}")
        return None
    ctx.log(
        f"パージ確認: 直径 {measurement.diameter_mm:.3f} mm"
        if measurement.detected
        else "パージ確認: ペーストが写りません"
    )
    return measurement.detected


def _purge(
    ctx: JobContext,
    session: PasteSession,
    correction: PasteCorrection,
    applicator: PasteApplicator,
    purge: ResolvedInitialPurge,
) -> None:
    """パージ点へ点塗布する.

    パージは pad ではなく座標なので、その点を覆う成功領域から内挿した補正を使う。
    """
    ctx.checkpoint()
    applicator.deposit_at(
        purge.point,
        amount_ul=purge.amount_ul,
        transform=session.point_transform(purge.point, correction),
    )


def _clean(
    ctx: JobContext,
    session: PasteSession,
    applicator: PasteApplicator,
    nozzle_clean: NozzleClean | None,
) -> str:
    """ノズル先端を掃除する（実施した処置の名前を返す）.

    直前の :meth:`PasteApplicator.deposit_at` がリトラクトして終わっているので、掃除の
    パージがその引き込みを埋めるだけで終わらないよう prime してから行い、基板へ移る間の
    垂れを止めるためにまた引き戻す。掃除しないなら引き込んだままでよく、続くパージの
    :meth:`PasteApplicator.deposit_at` が自分で prime する。
    """
    if nozzle_clean is None:
        ctx.log("ノズルクリーニング: 位置が未記録のためこすらずにパージし直します")
        return "パージのやり直し"
    ctx.progress("ノズルクリーニング")
    ctx.checkpoint()
    applicator.prime()
    clean_nozzle(session.klipper, session.stage, applicator, nozzle_clean, log=ctx.log)
    applicator.retract()
    return "クリーニングとパージのやり直し"
