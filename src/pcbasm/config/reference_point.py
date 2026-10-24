"""[reference_point] 節：基板の 4 隅にある基準点マーカーの概略位置."""

import attrs


@attrs.frozen
class CornerOffsetsConfig:
    """各コーナーにおけるボード端から基準点マーカーへのオフセット.

    Attributes:
        top_left: 左上コーナーのオフセット [x, y] (mm)
        top_right: 右上コーナーのオフセット [x, y] (mm)
        bottom_left: 左下コーナーのオフセット [x, y] (mm)
        bottom_right: 右下コーナーのオフセット [x, y] (mm)
    """

    top_left: tuple[float, float]
    top_right: tuple[float, float]
    bottom_left: tuple[float, float]
    bottom_right: tuple[float, float]


@attrs.frozen
class ReferencePointConfig:
    """基準点の設定.

    x, y は左上基準点マーカーへ移動するための概略マシン座標。
    offsets は各ボードコーナーから対応する基準点マーカーへの PCB 座標系ベクトル。
    ほかの隅の概略位置は :class:`~pcbasm.posctrl.board.BoardTransformMeasurer` が求める。
    """

    x: float
    y: float
    target_diameter: float
    offsets: CornerOffsetsConfig
