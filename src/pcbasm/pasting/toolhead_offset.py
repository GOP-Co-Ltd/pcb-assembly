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

MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT = 5


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
        point_spacing: 計測点同士の最小間隔設定 (mm)
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
        if len(measured_samples) < MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT:
            raise ValueError(
                "ツールヘッドオフセットの有効な計測点が不足しています"
                f"（有効 {len(measured_samples)} 点 / "
                f"最低 {MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT} 点）"
            )

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

    @property
    def is_within_tolerance(self) -> bool:
        """各軸の標準偏差が位置合わせ許容誤差以内か返す."""
        return (
            self.standard_deviation.x <= self.tolerance
            and self.standard_deviation.y <= self.tolerance
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
    """基板の安全領域内を左上から走査してオフセット計測点を配置する.

    安全領域のbbox左上から細かく走査し、外形や穴によって領域外になる候補を
    飛ばしながら、採用済み点との距離が ``point_spacing`` 以上の点を必要数採る。
    そのため格子配置を優先しつつ、外形に合わせて半間隔ずれた点も利用できる。
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
    scan_step = point_spacing / 2
    column_count = math.floor((max_x - min_x) / scan_step + 1e-9) + 1
    row_count = math.floor((max_y - min_y) / scan_step + 1e-9) + 1
    minimum_distance_squared = point_spacing**2 * (1.0 - 1e-9)

    selected: list[Point2d] = []
    for row in range(row_count):
        y = min_y + row * scan_step
        for column in range(column_count):
            candidate = Point2d(x=min_x + column * scan_step, y=y)
            if not safe_area.covers(ShapelyPoint(candidate.x, candidate.y)):
                continue
            if any(
                (candidate.x - point.x) ** 2 + (candidate.y - point.y) ** 2
                < minimum_distance_squared
                for point in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) == point_count:
                return tuple(selected)

    raise ValueError(
        "基板の安全領域に指定数の計測点を配置できません"
        f"（必要 {point_count} 点 / 配置可能 {len(selected)} 点、"
        f" 最小間隔={point_spacing:g} mm）"
    )
