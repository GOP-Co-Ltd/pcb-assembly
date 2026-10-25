"""装置の表示文字列（ステージ座標の書式）.

表示文字列はサーバー側で組み、JS とテンプレートはそのまま出す。
"""

from __future__ import annotations

from pcbasm.geometry import Point3d


def position_label(position: Point3d) -> str:
    """ステージ座標を小数 3 桁で書く（操作パネルの現在位置と、記録したキャップ位置）."""
    return f"X{position.x:.3f} Y{position.y:.3f} Z{position.z:.3f}"
