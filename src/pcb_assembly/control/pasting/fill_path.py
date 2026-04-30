"""ペースト塗布用フィルパス生成."""

from __future__ import annotations

from shapely import Polygon

from pcb_assembly.geometry import Point2d, generate_linear_path, generate_spiral_path


def build_paste_fill_path(polygon: Polygon, nozzle_diameter: float) -> list[Point2d]:
    """ペーストフィルパスを生成する.

    ノズル径からマシン固有のヒューリスティクスで間隔・インセット・
    フォールバック判定を行い、内部の純粋幾何APIに委譲する。

    - line_spacing  = nozzle_diameter
    - initial_inset = nozzle_diameter / 2
    - polygon.buffer(-nozzle_diameter) が空 → 線形（end_inset = nozzle_diameter / 2）
    - それ以外 → 螺旋。数値誤差で空になった場合は線形にフォールバック

    Args:
        polygon: 塗布対象のポリゴン（mm単位）
        nozzle_diameter: ノズル内径 [mm]

    Returns:
        塗布パスの座標リスト（パスが構築できなければ空リスト）

    Raises:
        ValueError: nozzle_diameter が 0 以下の場合
    """
    if nozzle_diameter <= 0:
        raise ValueError(
            f"nozzle_diameterは正の値である必要があります: {nozzle_diameter}"
        )

    if polygon.is_empty or not polygon.is_valid:
        return []

    line_spacing = nozzle_diameter
    initial_inset = nozzle_diameter / 2

    if polygon.buffer(-nozzle_diameter).is_empty:
        return generate_linear_path(polygon, end_inset=initial_inset)

    spiral = generate_spiral_path(polygon, line_spacing, initial_inset)
    if spiral:
        return spiral
    return generate_linear_path(polygon, end_inset=initial_inset)
