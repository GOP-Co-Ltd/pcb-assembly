"""運転時流量キャリブレーションの計画と補正（装置に触れない純関数）.

はんだ塗布は ``machine.toml`` の ``rotations_per_ul`` で μL を回転数へ換算する。
この係数はペーストの粘度・温度・残量で動くので、塗布パスへ入る直前に基板上へ
既知量のドットを並べて塗り、塗布前後画像から推定した体積との比で係数を直す。

撮影と塗布そのものは web 層のジョブが行う。
ここにあるのは「どこに何点塗るか」と「推定結果をどう係数へ落とすか」だけで、
どちらも装置なしで検証できる。

1 点だけでは点ごとの吐出ばらつき（実測で相対 9〜11 %）がそのまま補正値に乗って
しまうので、既定は 3 点を合計体積で集約する。
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
# そこで装置が暴れないように要件書どおり 1/3〜3 倍で頭打ちにする。
MIN_CORRECTION_SCALE = 1.0 / 3.0
MAX_CORRECTION_SCALE = 3.0


@attrs.frozen
class FlowCalibrationPlan:
    """測定に使うドットの配置と条件.

    Attributes:
        points: 塗布する点（board 座標。設定した点から +X へ等間隔）
        amount_ul: 1 点あたりの指令塗布量 [μL]
        crop_size_mm: 塗布前後画像の一辺 [mm]
        calibration_file: 推定に使う校正ファイル名
    """

    points: tuple[Point2d, ...]
    amount_ul: float
    crop_size_mm: float
    calibration_file: str

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
    point: Point2d | None,
    outline: Polygon,
) -> tuple[FlowCalibrationPlan | None, str | None]:
    """測定点の並びを決める.

    機能が無効なときは ``(None, None)`` を返す。
    設定された点が基板外形の外にあるなど、有効なのに計画できないときだけ
    ``(None, 理由)`` を返す。

    Args:
        config: ``machine.toml`` の運転時キャリブレーション設定
        point: 基板ごとに設定した起点（未設定なら ``None``）
        outline: 基板外形

    Returns:
        ``(計画, None)`` / ``(None, None)``（無効）/ ``(None, 理由)``
    """
    if not config.enabled or point is None:
        return None, None
    error = validate_flow_calibration_point(point=point, outline=outline)
    if error is not None:
        return None, error
    # crop は点を中心に ±crop/2 を切り出すので、隣の点がこれより近いと隣のドットが
    # crop へ写り込み、最大連結成分が別のドットになる
    if config.point_count > 1 and config.point_pitch_mm <= config.crop_size_mm:
        return None, (
            "測定点の間隔は撮影crop寸法より大きくしてください: "
            f"{config.point_pitch_mm} mm <= {config.crop_size_mm} mm"
        )

    points = tuple(
        Point2d(point.x + config.point_pitch_mm * index, point.y)
        for index in range(config.point_count)
    )
    for candidate in points[1:]:
        if not outline.covers(Point(candidate.x, candidate.y)):
            return None, (
                "流量キャリブレーションの測定点が基板外形の外に出ます: "
                f"({candidate.x:.3f}, {candidate.y:.3f})"
            )
    return (
        FlowCalibrationPlan(
            points=points,
            amount_ul=config.amount_ul,
            crop_size_mm=config.crop_size_mm,
            calibration_file=config.calibration_file,
        ),
        None,
    )


def validate_flow_calibration_point(
    *, point: Point2d | None, outline: Polygon
) -> str | None:
    """起点そのものを検証する（不正なら日本語エラー文、正常・未設定なら ``None``）.

    測定点が何点並ぶかは ``machine.toml`` 側の設定で後から変わるので、ここでは
    起点だけを見る。
    並びが基板からはみ出すかは :func:`plan_flow_calibration` が判定する。
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
    点ごとの比を平均すると小さい点の相対誤差が効きすぎるので、合計どうしの比を使う。

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
