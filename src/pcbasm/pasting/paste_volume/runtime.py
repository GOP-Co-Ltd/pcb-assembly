"""運転時流量キャリブレーションの計画と補正（装置に触れない純関数）.

はんだ塗布は ``machine.toml`` の ``rotations_per_ul`` で μL を回転数へ換算する。
この係数はペーストの粘度・温度・残量で動くので、塗布パスへ入る直前に基板上へ
既知量のドットを並べて塗り、塗布前後画像から推定した体積との比で係数を直す。

撮影と塗布そのものは web 層のジョブが行う。
ここにあるのは「どこに何点塗るか」と「推定結果から係数をどう求めるか」だけで、
どちらも装置なしで検証できる。

1 点だけでは点ごとの吐出ばらつき（実測で相対 9〜11 %）がそのまま補正値に乗って
しまうので、複数点を合計体積で集約する。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs
from shapely import Point, Polygon

from pcbasm.config import FlowCalibration
from pcbasm.geometry import Point2d
from pcbasm.pasting.paste_volume.estimator import PasteVolumePrediction
from pcbasm.utils import is_finite_number

# 1 回の補正で許す ``rotations_per_ul`` の変化幅。
# 画像の取り違え・ノズル詰まり・校正の条件違いは極端な比として現れるので、
# 極端な係数で装置を動かさないよう要件書どおり 1/3〜3 倍で頭打ちにする。
MIN_CORRECTION_SCALE = 1.0 / 3.0
MAX_CORRECTION_SCALE = 3.0


@attrs.frozen
class FlowCalibrationPlan:
    """測定に使うドットの配置と条件.

    Attributes:
        points: 塗布する点（board 座標。基板設定で 1 点ずつ与えた並び）
        amount_ul: 1 点あたりの指令塗布量 [μL]
        crop_size_mm: 塗布前後画像の一辺 [mm]
        calibration_file: 推定に使う校正ファイル名
        settle_seconds: 全点を塗ってから塗布後の撮影に入るまでの待ち [秒]
    """

    points: tuple[Point2d, ...]
    amount_ul: float
    crop_size_mm: float
    calibration_file: str
    settle_seconds: float

    @property
    def total_commanded_ul(self) -> float:
        """全点の指令塗布量の合計 [μL]."""
        return self.amount_ul * len(self.points)


@attrs.frozen
class FlowCalibrationOutcome:
    """補正の算出結果.

    Attributes:
        commanded_ul: 採用した点の指令塗布量の合計 [μL]
        estimated_ul: 同じ点の推定塗布量の合計 [μL]
        ratio: 実塗布量 / 指令塗布量
        previous_rotations_per_ul: 補正前の係数 [rev/μL]
        rotations_per_ul: 補正後の係数 [rev/μL]
        clamped: 1/3〜3 倍の頭打ちに当たったか
        accepted_count: 集約へ入れた点の数
        rejections: 採用しなかった点の理由（入力順）
    """

    commanded_ul: float
    estimated_ul: float
    ratio: float
    previous_rotations_per_ul: float
    rotations_per_ul: float
    clamped: bool
    accepted_count: int
    rejections: tuple[str, ...]


def plan_flow_calibration(
    *,
    config: FlowCalibration,
    points: Sequence[Point2d],
    outline: Polygon,
) -> tuple[FlowCalibrationPlan | None, str | None]:
    """測定点の並びを検証して計画にまとめる.

    機能が無効なとき（校正ファイル未設定・測定点 0 個）は ``(None, None)`` を返す。
    設定された点が基板外形の外にあるなど、有効なのに計画できないときだけ
    ``(None, 理由)`` を返す。

    Args:
        config: ``machine.toml`` の運転時キャリブレーション設定
        points: 基板ごとに設定した測定位置（塗る順）
        outline: 基板外形

    Returns:
        ``(計画, None)`` / ``(None, None)``（無効）/ ``(None, 理由)``
    """
    if not config.enabled or not points:
        return None, None
    for point in points:
        error = validate_flow_calibration_point(point=point, outline=outline)
        if error is not None:
            return None, error
    error = validate_crop_separation(points=points, crop_size_mm=config.crop_size_mm)
    if error is not None:
        return None, error
    return (
        FlowCalibrationPlan(
            points=tuple(points),
            amount_ul=config.amount_ul,
            crop_size_mm=config.crop_size_mm,
            calibration_file=config.calibration_file,
            settle_seconds=config.settle_seconds,
        ),
        None,
    )


def validate_crop_separation(
    *, points: Sequence[Point2d], crop_size_mm: float
) -> str | None:
    """測定点の crop どうしが重ならないかを検証する.

    crop は点を中心に ±``crop_size_mm``/2 を切り出す。
    crop が重なるほど点が近いと隣のドットが写り込み、最大連結成分が別のドットになる。

    Returns:
        重なっていればその旨の日本語エラー文、問題なければ ``None``
    """
    if not is_finite_number(crop_size_mm) or crop_size_mm <= 0:
        return f"撮影crop寸法は正の有限値が必要です: {crop_size_mm!r}"
    for first in range(len(points)):
        for second in range(first + 1, len(points)):
            a, b = points[first], points[second]
            if crops_overlap(a, b, crop_size_mm=crop_size_mm):
                return (
                    f"測定位置 {first + 1} と {second + 1} の撮影範囲"
                    f"（{crop_size_mm} mm 角）が重なっています: "
                    f"({a.x:.3f}, {a.y:.3f}) / ({b.x:.3f}, {b.y:.3f})"
                )
    return None


def crops_overlap(first: Point2d, second: Point2d, *, crop_size_mm: float) -> bool:
    """2 点の撮影範囲（各点を中心とする ``crop_size_mm`` 角）が重なるか."""
    return (
        abs(first.x - second.x) < crop_size_mm
        and abs(first.y - second.y) < crop_size_mm
    )


def overlapping_crops(
    points: Sequence[Point2d], *, crop_size_mm: float
) -> tuple[bool, ...]:
    """点ごとに、他のどれかの撮影範囲と重なっているかを返す.

    どこが置き直しの対象かを図で示すためのもので、判定の出典をここ 1 か所に保つ。
    """
    if not is_finite_number(crop_size_mm) or crop_size_mm <= 0:
        return tuple(False for _ in points)
    return tuple(
        any(
            other_index != index
            and crops_overlap(point, other, crop_size_mm=crop_size_mm)
            for other_index, other in enumerate(points)
        )
        for index, point in enumerate(points)
    )


def validate_flow_calibration_point(
    *, point: Point2d | None, outline: Polygon
) -> str | None:
    """測定点 1 つを検証する（不正なら日本語エラー文、正常・未設定なら ``None``）.

    保存の入口はこれだけを見る。

    crop の重なりは ``crop_size_mm`` を後から変えるだけでも成立しなくなるので、
    保存時には拒否せず :func:`validate_crop_separation` が計画時に判定する。
    """
    if point is None:
        return None
    if not is_finite_number(point.x) or not is_finite_number(point.y):
        return (
            "流量キャリブレーション位置は有限な座標で指定してください: "
            f"({point.x}, {point.y})"
        )
    if not outline.covers(Point(point.x, point.y)):
        return (
            "流量キャリブレーション位置は基板外形の内側で指定してください: "
            f"({point.x:.3f}, {point.y:.3f})"
        )
    return None


def correct_rotations_per_ul(
    predictions: Sequence[PasteVolumePrediction],
    *,
    amount_ul: float,
    rotations_per_ul: float,
) -> tuple[FlowCalibrationOutcome | None, str | None]:
    """推定塗布量から ``rotations_per_ul`` の補正値を求める.

    採用できた点だけを合計体積で集約する。
    点ごとの比を平均すると小さい点の相対誤差の影響が大きくなりすぎるので、合計どうしの比を使う。

    算出:
        ratio = 採用点の推定量の合計 / (amount_ul × 採用点の数)
        補正後の係数 = rotations_per_ul / ratio を、補正前の 1/3〜3 倍に頭打ちする
    指令より多く出ていれば（ratio > 1）係数は小さくなる。

    Args:
        predictions: 点ごとの推定結果（入力順）
        amount_ul: 1 点あたりの指令塗布量 [μL]
        rotations_per_ul: 補正前の係数 [rev/μL]

    Returns:
        ``(結果, None)`` または ``(None, 補正しない理由)``
    """
    if not is_finite_number(amount_ul) or amount_ul <= 0:
        return None, f"指令塗布量は正の有限値が必要です: {amount_ul!r}"
    if not is_finite_number(rotations_per_ul) or rotations_per_ul <= 0:
        return None, f"rotations_per_ulは正の有限値が必要です: {rotations_per_ul!r}"

    accepted = [
        prediction.mean_volume_ul for prediction in predictions if prediction.accepted
    ]
    rejections = tuple(
        prediction.rejection_reason or "unknown"
        for prediction in predictions
        if not prediction.accepted
    )
    if not accepted:
        reasons = "、".join(rejections) if rejections else "測定点がありません"
        return None, f"採用できた測定点がありません: {reasons}"

    commanded = amount_ul * len(accepted)
    estimated = sum(accepted)
    if estimated <= 0:
        return None, f"推定塗布量の合計が0以下です: {estimated}"

    ratio = estimated / commanded
    corrected = rotations_per_ul / ratio
    lowest = rotations_per_ul * MIN_CORRECTION_SCALE
    highest = rotations_per_ul * MAX_CORRECTION_SCALE
    clamped = min(max(corrected, lowest), highest)
    return (
        FlowCalibrationOutcome(
            commanded_ul=commanded,
            estimated_ul=estimated,
            ratio=ratio,
            previous_rotations_per_ul=rotations_per_ul,
            rotations_per_ul=clamped,
            clamped=clamped != corrected,
            accepted_count=len(accepted),
            rejections=rejections,
        ),
        None,
    )
