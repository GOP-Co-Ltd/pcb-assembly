"""ツールヘッドオフセット計測結果."""

import json
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter
from shapely import Point as ShapelyPoint, Polygon

from pcbasm.geometry import Point2d

_converter = make_converter()
_converter.register_unstructure_hook(Point2d, lambda p: {"x": p.x, "y": p.y})
_converter.register_structure_hook(Point2d, lambda d, _: Point2d(x=d["x"], y=d["y"]))


@attrs.frozen
class ToolheadOffsetSample:
    """1計測点のツールヘッドオフセット.

    Attributes:
        board_position: 基板上の計測位置
        dispense_position: ペースト吐出時のステージ位置
        camera_position: カメラが検出した位置
        offset: ツールヘッドXYオフセット (mm)
    """

    board_position: Point2d
    dispense_position: Point2d
    camera_position: Point2d
    offset: Point2d

    @classmethod
    def from_positions(
        cls,
        *,
        board_position: Point2d,
        dispense_position: Point2d,
        camera_position: Point2d,
    ) -> Self:
        """吐出位置と検出位置から1点分のオフセットを算出する."""
        return cls(
            board_position=board_position,
            dispense_position=dispense_position,
            camera_position=camera_position,
            offset=dispense_position - camera_position,
        )


@attrs.frozen
class ToolheadOffsetResult:
    """複数点から計測したツールヘッドオフセット結果.

    Attributes:
        offset: 全計測点の平均ツールヘッドXYオフセット (mm)
        standard_deviation: 各軸の母標準偏差 (mm)
        samples: 各計測点の結果
        tolerance: 収束許容誤差 (mm)
        point_spacing: 計測点を配置した格子間隔 (mm)
        edge_margin: ペースト外縁から基板外周までのmargin (mm)
        calibrated_at: 計測日時
    """

    offset: Point2d
    standard_deviation: Point2d
    samples: tuple[ToolheadOffsetSample, ...]
    tolerance: float
    point_spacing: float
    edge_margin: float
    calibrated_at: datetime

    @classmethod
    def measure(
        cls,
        samples: Sequence[ToolheadOffsetSample],
        *,
        tolerance: float,
        point_spacing: float,
        edge_margin: float,
        calibrated_at: datetime,
    ) -> Self:
        """各計測点のオフセットから平均と母標準偏差を算出する."""
        measured_samples = tuple(samples)
        if not measured_samples:
            raise ValueError("ツールヘッドオフセットの計測点がありません")

        count = len(measured_samples)
        mean = Point2d(
            x=sum(sample.offset.x for sample in measured_samples) / count,
            y=sum(sample.offset.y for sample in measured_samples) / count,
        )
        standard_deviation = Point2d(
            x=math.sqrt(
                sum((sample.offset.x - mean.x) ** 2 for sample in measured_samples)
                / count
            ),
            y=math.sqrt(
                sum((sample.offset.y - mean.y) ** 2 for sample in measured_samples)
                / count
            ),
        )
        return cls(
            offset=mean,
            standard_deviation=standard_deviation,
            samples=measured_samples,
            tolerance=tolerance,
            point_spacing=point_spacing,
            edge_margin=edge_margin,
            calibrated_at=calibrated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def plan_toolhead_offset_points(
    outline: Polygon,
    *,
    point_count: int,
    point_spacing: float,
    edge_margin: float,
    paste_diameter_max: float,
) -> tuple[Point2d, ...]:
    """基板の安全領域内へツールヘッドオフセット計測点を格子配置する.

    格子は安全領域のbbox中心を基準とし、中心が領域外なら領域内の代表点を
    基準にする。中心に近い候補を必要数選び、移動順は行ごとのsnake順にする。
    """
    if (
        isinstance(point_count, bool)
        or not isinstance(point_count, int)
        or point_count < 1
    ):
        raise ValueError("point_countは1以上の整数である必要があります")
    if not math.isfinite(point_spacing) or point_spacing <= 0:
        raise ValueError("point_spacingは正の有限値である必要があります")
    if not math.isfinite(edge_margin) or edge_margin < 0:
        raise ValueError("edge_marginは0以上の有限値である必要があります")
    if not math.isfinite(paste_diameter_max) or paste_diameter_max <= 0:
        raise ValueError("paste_diameter_maxは正の有限値である必要があります")
    if outline.is_empty:
        raise ValueError("基板外形が空のため計測点を配置できません")
    if not outline.is_valid:
        raise ValueError("基板外形が不正なため計測点を配置できません")

    clearance = edge_margin + paste_diameter_max / 2
    safe_area = outline.buffer(-clearance)
    if safe_area.is_empty:
        raise ValueError(
            "基板外形に安全領域を確保できません"
            f"（margin={edge_margin:g} mm, 最大直径={paste_diameter_max:g} mm）"
        )

    min_x, min_y, max_x, max_y = safe_area.bounds
    center = ShapelyPoint((min_x + max_x) / 2, (min_y + max_y) / 2)
    anchor = center if safe_area.covers(center) else safe_area.representative_point()

    min_column = math.ceil((min_x - anchor.x) / point_spacing)
    max_column = math.floor((max_x - anchor.x) / point_spacing)
    min_row = math.ceil((min_y - anchor.y) / point_spacing)
    max_row = math.floor((max_y - anchor.y) / point_spacing)

    candidates: list[tuple[int, int, Point2d]] = []
    for row in range(min_row, max_row + 1):
        y = anchor.y + row * point_spacing
        for column in range(min_column, max_column + 1):
            x = anchor.x + column * point_spacing
            if safe_area.covers(ShapelyPoint(x, y)):
                candidates.append((column, row, Point2d(x=x, y=y)))

    if len(candidates) < point_count:
        raise ValueError(
            "基板の安全領域に指定数の計測点を配置できません"
            f"（必要 {point_count} 点 / 配置可能 {len(candidates)} 点、"
            f" 間隔={point_spacing:g} mm）"
        )

    selected = sorted(
        candidates,
        key=lambda candidate: (
            (candidate[2].x - anchor.x) ** 2 + (candidate[2].y - anchor.y) ** 2,
            candidate[1],
            candidate[0],
        ),
    )[:point_count]

    rows: dict[int, list[tuple[int, Point2d]]] = {}
    for column, row, point in selected:
        rows.setdefault(row, []).append((column, point))

    planned: list[Point2d] = []
    for row_index, row in enumerate(sorted(rows)):
        row_points = sorted(rows[row], reverse=row_index % 2 == 1)
        planned.extend(point for _, point in row_points)
    return tuple(planned)
