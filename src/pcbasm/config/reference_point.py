"""[reference_point] 節：基板の 4 隅にある基準点マーカーの概略位置."""

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import as_validator, validate_positive_number

_OFFSETS = "コーナーオフセット"


@attrs.frozen
class CornerOffsetsConfig:
    """各コーナーにおけるボード端から基準点マーカーへのオフセット.

    Attributes:
        top_left: 左上コーナーのオフセット [x, y] (mm)
        top_right: 右上コーナーのオフセット [x, y] (mm)
        bottom_left: 左下コーナーのオフセット [x, y] (mm)
        bottom_right: 右下コーナーのオフセット [x, y] (mm)
    """

    top_left: tuple[float, float] = attrs.field(
        metadata=setting(label="左上 [x, y]", unit="mm", group=_OFFSETS, order=5)
    )
    top_right: tuple[float, float] = attrs.field(
        metadata=setting(label="右上 [x, y]", unit="mm", group=_OFFSETS, order=6)
    )
    bottom_left: tuple[float, float] = attrs.field(
        metadata=setting(label="左下 [x, y]", unit="mm", group=_OFFSETS, order=7)
    )
    bottom_right: tuple[float, float] = attrs.field(
        metadata=setting(label="右下 [x, y]", unit="mm", group=_OFFSETS, order=8)
    )


@attrs.frozen
class ReferencePointConfig:
    """基準点の設定.

    x, y は左上基準点マーカーへ移動するための概略マシン座標。
    offsets は各ボードコーナーから対応する基準点マーカーへの PCB 座標系ベクトル。
    ほかの隅の概略位置は :class:`~pcbasm.align.board_frame.BoardTransformMeasurer` が求める。
    """

    x: float = attrs.field(
        metadata=setting(label="基準点 X", unit="mm", group="位置", order=1)
    )
    y: float = attrs.field(
        metadata=setting(label="基準点 Y", unit="mm", group="位置", order=2)
    )
    target_diameter: float = attrs.field(
        metadata=setting(label="基準点マーカー直径", unit="mm", group="位置", order=3)
    )
    offsets: CornerOffsetsConfig
    # 検出する円の直径の許容誤差。プレビューとジョブが同じ値で同じ円を見る
    diameter_tolerance_mm: float = attrs.field(
        default=0.5,
        validator=as_validator(validate_positive_number),
        metadata=setting(
            label="基準点マーカー直径の許容誤差", unit="mm", group="位置", order=4
        ),
    )
