"""XYステージのグリッドキャリブレーションと座標変換."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Self, cast

import attrs
import numpy as np
import numpy.typing as npt

from pcbasm.config import Machine
from pcbasm.geometry import Path as GeometryPath, Point2d, Point3d

JSON_VERSION = 1
_EPSILON = 1e-8

type _Array = npt.NDArray[np.float64]


@attrs.frozen
class XYCalibrationGrid:
    """既知寸法の穴グリッド."""

    hole_diameter_mm: float = attrs.field(converter=float)
    spacing_mm: float = attrs.field(converter=float)
    rows: int
    columns: int

    def __attrs_post_init__(self) -> None:
        values = (self.hole_diameter_mm, self.spacing_mm)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("穴径とグリッド間隔は正の有限値である必要があります")
        if self.hole_diameter_mm >= self.spacing_mm:
            raise ValueError("穴径はグリッド間隔未満である必要があります")
        if (
            isinstance(self.rows, bool)
            or not isinstance(self.rows, int)
            or self.rows < 2
        ):
            raise ValueError("グリッド行数は2以上である必要があります")
        if (
            isinstance(self.columns, bool)
            or not isinstance(self.columns, int)
            or self.columns < 2
        ):
            raise ValueError("グリッド列数は2以上である必要があります")

    def points(self) -> tuple[Point2d, ...]:
        """左上からrow-major順にローカル座標を返す."""
        return tuple(
            Point2d(column * self.spacing_mm, row * self.spacing_mm)
            for row in range(self.rows)
            for column in range(self.columns)
        )

    def detection_roi_size(self, pixel_per_mm: float) -> tuple[int, int]:
        """1グリッド間隔を一辺とする正方形の検出ROIをpixelで返す."""
        if not math.isfinite(pixel_per_mm) or pixel_per_mm <= 0:
            raise ValueError("pixel_per_mmは正の有限値である必要があります")
        side = max(1, round(self.spacing_mm * pixel_per_mm))
        return side, side


@attrs.frozen
class XYCalibrationTransform:
    """Logical XYとraw Klipper XYの可逆変換."""

    _grid: XYCalibrationGrid
    _rotation: tuple[tuple[float, float], tuple[float, float]]
    _translation: tuple[float, float]
    _global_affine: tuple[tuple[float, float, float], tuple[float, float, float]]
    _residuals: tuple[tuple[float, float], ...]

    @classmethod
    def identity(cls) -> Self:
        """恒等変換を返す."""
        grid = XYCalibrationGrid(0.5, 1.0, 2, 2)
        return cls(
            grid,
            ((1.0, 0.0), (0.0, 1.0)),
            (0.0, 0.0),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0),) * 4,
        )

    @classmethod
    def from_result(cls, result: XYCalibrationResult) -> Self:
        """計測結果から変換を構築する."""
        local = _points_array(result.grid.points())
        raw = _points_array(result.raw_points)
        rotation, translation = _fit_rigid(local, raw)
        logical = local @ rotation.T + translation
        design = np.column_stack((logical, np.ones(len(logical))))
        affine_t, _, rank, _ = np.linalg.lstsq(design, raw, rcond=None)
        if rank < 3:
            raise ValueError("XYキャリブレーションのaffineを推定できません")
        affine = affine_t.T
        predicted = design @ affine_t
        residuals = raw - predicted
        transform = cls(
            result.grid,
            _matrix_tuple(rotation),
            _point_tuple(translation),
            _affine_tuple(cast(_Array, affine)),
            tuple(_point_tuple(value) for value in residuals),
        )
        transform._validate_regions()
        return transform

    @classmethod
    def from_data(cls, grid: XYCalibrationGrid, payload: dict[str, Any]) -> Self:
        try:
            rotation = np.asarray(payload["rotation"], dtype=np.float64)
            translation = np.asarray(payload["translation"], dtype=np.float64)
            global_affine = np.asarray(payload["global_affine"], dtype=np.float64)
            residuals = np.asarray(payload["residuals"], dtype=np.float64)
            arrays = (rotation, translation, global_affine, residuals)
            expected_shapes = (
                (2, 2),
                (2,),
                (2, 3),
                (grid.rows * grid.columns, 2),
            )
            if any(
                array.shape != shape or not np.isfinite(array).all()
                for array, shape in zip(arrays, expected_shapes, strict=True)
            ):
                raise ValueError
            transform = cls(
                grid,
                _matrix_tuple(rotation),
                _point_tuple(translation),
                _affine_tuple(global_affine),
                tuple(_point_tuple(row) for row in residuals),
            )
            transform._validate_regions()
            return transform
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("XYキャリブレーション変換係数が不正です") from exc

    def to_data(self) -> dict[str, Any]:
        return {
            "rotation": self._rotation,
            "translation": self._translation,
            "global_affine": self._global_affine,
            "residuals": self._residuals,
        }

    @property
    def is_identity(self) -> bool:
        """恒等変換かどうかを返す."""
        return self == type(self).identity()

    def apply(self, point: Point2d) -> Point2d:
        """Logical座標をraw Klipper座標へ変換する."""
        local = self._logical_to_local(point)
        matrix, offset = self._local_region_affine(local)
        raw = matrix @ local + offset
        return Point2d(float(raw[0]), float(raw[1]))

    def inverse(self, point: Point2d) -> Point2d:
        """Raw Klipper座標をlogical座標へ解析的に逆変換する."""
        raw = np.array((point.x, point.y), dtype=float)
        for matrix, offset, accepts in self._inverse_regions():
            determinant = float(np.linalg.det(matrix))
            if abs(determinant) <= _EPSILON:
                continue
            local = cast(_Array, np.linalg.solve(matrix, raw - offset))
            if accepts(local):
                return self._local_to_logical(local)
        raise ValueError(f"XYキャリブレーションを逆変換できません: {point}")

    def transform_path(self, path: GeometryPath) -> GeometryPath:
        """残差meshの境界で分割してPathをraw座標へ変換する."""
        if not path.points or self.is_identity:
            return path
        result = [self._transform_point3d(path.points[0])]
        for start, end in zip(path.points, path.points[1:]):
            for ratio in self._segment_breaks(start.to2d(), end.to2d()):
                point = start + (end - start) * ratio
                result.append(self._transform_point3d(point))
        return GeometryPath(tuple(result))

    def _transform_point3d(self, point: Point3d) -> Point3d:
        transformed = self.apply(point.to2d())
        return Point3d(transformed.x, transformed.y, point.z)

    def _logical_to_local(self, point: Point2d) -> _Array:
        rotation = np.asarray(self._rotation)
        translation = np.asarray(self._translation)
        return rotation.T @ (np.array((point.x, point.y)) - translation)

    def _local_to_logical(self, local: _Array) -> Point2d:
        logical = np.asarray(self._rotation) @ local + np.asarray(self._translation)
        return Point2d(float(logical[0]), float(logical[1]))

    def _global_local_affine(self) -> tuple[_Array, _Array]:
        affine = np.asarray(self._global_affine)
        rotation = np.asarray(self._rotation)
        translation = np.asarray(self._translation)
        return affine[:, :2] @ rotation, affine[:, :2] @ translation + affine[:, 2]

    def _residual(self, row: int, column: int) -> _Array:
        return np.asarray(self._residuals[row * self._grid.columns + column])

    def _local_region_affine(self, local: _Array) -> tuple[_Array, _Array]:
        residual_matrix, residual_offset = self._residual_region_affine(local)
        global_matrix, global_offset = self._global_local_affine()
        return global_matrix + residual_matrix, global_offset + residual_offset

    def _residual_region_affine(self, local: _Array) -> tuple[_Array, _Array]:
        spacing = self._grid.spacing_mm
        width = (self._grid.columns - 1) * spacing
        height = (self._grid.rows - 1) * spacing
        x, y = float(local[0]), float(local[1])
        clamped_x = min(max(x, 0.0), width)
        clamped_y = min(max(y, 0.0), height)
        column = min(int(clamped_x / spacing), self._grid.columns - 2)
        row = min(int(clamped_y / spacing), self._grid.rows - 2)

        # 外側ではclampされた軸方向の残差勾配を0にする。
        if x < 0.0 or x > width or y < 0.0 or y > height:
            return self._edge_residual_affine(x, y, row, column)

        fx = (clamped_x - column * spacing) / spacing
        fy = (clamped_y - row * spacing) / spacing
        if fy <= fx:
            vertices = ((row, column), (row, column + 1), (row + 1, column + 1))
        else:
            vertices = ((row, column), (row + 1, column + 1), (row + 1, column))
        return self._triangle_residual_affine(vertices)

    def _triangle_residual_affine(
        self, vertices: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]
    ) -> tuple[_Array, _Array]:
        coordinates = np.array(
            [
                (column * self._grid.spacing_mm, row * self._grid.spacing_mm, 1.0)
                for row, column in vertices
            ]
        )
        values = np.array([self._residual(row, column) for row, column in vertices])
        coefficients = cast(_Array, np.linalg.solve(coordinates, values))
        return coefficients[:2].T, coefficients[2]

    def _edge_residual_affine(
        self, x: float, y: float, row: int, column: int
    ) -> tuple[_Array, _Array]:
        spacing = self._grid.spacing_mm
        max_x = (self._grid.columns - 1) * spacing
        max_y = (self._grid.rows - 1) * spacing
        if x < 0.0 or x > max_x:
            edge_column = 0 if x < 0.0 else self._grid.columns - 1
            if y < 0.0 or y > max_y:
                edge_row = 0 if y < 0.0 else self._grid.rows - 1
                return np.zeros((2, 2), dtype=np.float64), self._residual(
                    edge_row, edge_column
                )
            start = self._residual(row, edge_column)
            slope = (self._residual(row + 1, edge_column) - start) / spacing
            matrix = np.column_stack((np.zeros(2), slope))
            return matrix, start - slope * row * spacing
        edge_row = 0 if y < 0.0 else self._grid.rows - 1
        start = self._residual(edge_row, column)
        slope = (self._residual(edge_row, column + 1) - start) / spacing
        matrix = np.column_stack((slope, np.zeros(2)))
        return matrix, start - slope * column * spacing

    def _inverse_regions(
        self,
    ) -> Iterator[tuple[_Array, _Array, Callable[[_Array], bool]]]:
        spacing = self._grid.spacing_mm
        max_x = (self._grid.columns - 1) * spacing
        max_y = (self._grid.rows - 1) * spacing
        tolerance = 1e-7

        # 内部三角形。
        for row in range(self._grid.rows - 1):
            for column in range(self._grid.columns - 1):
                x0, y0 = column * spacing, row * spacing
                for lower in (True, False):
                    sample = np.array(
                        (
                            x0 + (0.75 if lower else 0.25) * spacing,
                            y0 + (0.25 if lower else 0.75) * spacing,
                        )
                    )
                    matrix, offset = self._local_region_affine(sample)

                    yield (
                        matrix,
                        offset,
                        _triangle_acceptor(x0, y0, spacing, lower, tolerance),
                    )

        # 4辺の半無限strip。
        for row in range(self._grid.rows - 1):
            y0 = row * spacing
            for left in (True, False):
                sample = np.array((-1.0 if left else max_x + 1.0, y0 + spacing / 2))
                matrix, offset = self._local_region_affine(sample)

                yield (
                    matrix,
                    offset,
                    _vertical_acceptor(y0, spacing, left, max_x, tolerance),
                )
        for column in range(self._grid.columns - 1):
            x0 = column * spacing
            for top in (True, False):
                sample = np.array((x0 + spacing / 2, -1.0 if top else max_y + 1.0))
                matrix, offset = self._local_region_affine(sample)

                yield (
                    matrix,
                    offset,
                    _horizontal_acceptor(x0, spacing, top, max_y, tolerance),
                )

        # 4隅のquadrant。
        for left in (True, False):
            for top in (True, False):
                sample = np.array(
                    (-1.0 if left else max_x + 1.0, -1.0 if top else max_y + 1.0)
                )
                matrix, offset = self._local_region_affine(sample)

                yield (
                    matrix,
                    offset,
                    _corner_acceptor(left, top, max_x, max_y, tolerance),
                )

    def _validate_regions(self) -> None:
        determinants = [
            float(np.linalg.det(matrix)) for matrix, _, _ in self._inverse_regions()
        ]
        if not determinants or any(
            not math.isfinite(value) or value <= _EPSILON for value in determinants
        ):
            raise ValueError("XYキャリブレーションmeshがfoldしています")

    def _segment_breaks(self, start: Point2d, end: Point2d) -> tuple[float, ...]:
        p0 = self._logical_to_local(start)
        p1 = self._logical_to_local(end)
        delta = p1 - p0
        ratios = {1.0}
        spacing = self._grid.spacing_mm
        for axis, count in ((0, self._grid.columns), (1, self._grid.rows)):
            if abs(float(delta[axis])) <= _EPSILON:
                continue
            for index in range(count):
                ratio = (index * spacing - p0[axis]) / delta[axis]
                if _EPSILON < ratio < 1.0 - _EPSILON:
                    ratios.add(float(ratio))
        # 各セルの固定対角線（TL→BR）との交点。
        diagonal_delta = float(delta[1] - delta[0])
        if abs(diagonal_delta) > _EPSILON:
            for row in range(self._grid.rows - 1):
                for column in range(self._grid.columns - 1):
                    constant = (row - column) * spacing
                    ratio = (constant - (p0[1] - p0[0])) / diagonal_delta
                    if not _EPSILON < ratio < 1.0 - _EPSILON:
                        continue
                    point = p0 + ratio * delta
                    x0, y0 = column * spacing, row * spacing
                    if (
                        x0 - _EPSILON <= point[0] <= x0 + spacing + _EPSILON
                        and y0 - _EPSILON <= point[1] <= y0 + spacing + _EPSILON
                    ):
                        ratios.add(float(ratio))
        return tuple(sorted(ratios))


@attrs.frozen
class XYCalibrationResult:
    """XYキャリブレーションの再現可能な計測結果."""

    grid: XYCalibrationGrid
    logical_points: tuple[Point2d, ...]
    raw_points: tuple[Point2d, ...]
    verification_errors: tuple[Point2d, ...] = ()
    calibrated_at: str = attrs.field(factory=lambda: datetime.now().isoformat())
    _fitted_transform: XYCalibrationTransform | None = attrs.field(
        default=None, init=False, repr=False, eq=False
    )

    def __attrs_post_init__(self) -> None:
        expected = self.grid.rows * self.grid.columns
        if len(self.logical_points) != expected or len(self.raw_points) != expected:
            raise ValueError("計測点数がグリッド点数と一致しません")
        if self.verification_errors and len(self.verification_errors) != expected:
            raise ValueError("検証誤差数がグリッド点数と一致しません")
        values = (
            value
            for point in (
                *self.logical_points,
                *self.raw_points,
                *self.verification_errors,
            )
            for value in (point.x, point.y)
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("XYキャリブレーションの点は有限値である必要があります")

    @classmethod
    def fit(
        cls,
        grid: XYCalibrationGrid,
        raw_points: tuple[Point2d, ...] | list[Point2d],
        verification_errors: tuple[Point2d, ...] | list[Point2d] = (),
        calibrated_at: str | None = None,
    ) -> Self:
        """raw計測点を剛体配置へ正規化して結果を構築する."""
        raw = tuple(raw_points)
        if len(raw) != grid.rows * grid.columns:
            raise ValueError("raw計測点数がグリッド点数と一致しません")
        local = _points_array(grid.points())
        raw_array = _points_array(raw)
        if not np.isfinite(raw_array).all():
            raise ValueError("raw計測点は有限値である必要があります")
        rotation, translation = _fit_rigid(local, raw_array)
        logical_array = local @ rotation.T + translation
        result = cls(
            grid=grid,
            logical_points=tuple(
                Point2d(*map(float, point)) for point in logical_array
            ),
            raw_points=raw,
            verification_errors=tuple(verification_errors),
            calibrated_at=calibrated_at or datetime.now().isoformat(),
        )
        object.__setattr__(
            result, "_fitted_transform", XYCalibrationTransform.from_result(result)
        )
        return result

    @property
    def rms_error(self) -> float:
        """検証誤差のRMS [mm]."""
        if not self.verification_errors:
            return 0.0
        return math.sqrt(
            sum(point.x**2 + point.y**2 for point in self.verification_errors)
            / len(self.verification_errors)
        )

    @property
    def max_error(self) -> float:
        """検証誤差の最大ノルム [mm]."""
        return max((point.norm for point in self.verification_errors), default=0.0)

    @property
    def transform(self) -> XYCalibrationTransform:
        """結果から座標変換を構築する."""
        return self._fitted_transform or XYCalibrationTransform.from_result(self)

    def save(self, path: Path) -> None:
        """Versioned JSONをatomic保存する."""
        payload = {
            "version": JSON_VERSION,
            "calibrated_at": self.calibrated_at,
            "grid": attrs.asdict(self.grid),
            "logical_points": [[point.x, point.y] for point in self.logical_points],
            "raw_points": [[point.x, point.y] for point in self.raw_points],
            "verification_errors": [
                [point.x, point.y] for point in self.verification_errors
            ],
            "transform": self.transform.to_data(),
            "rms_error_mm": self.rms_error,
            "max_error_mm": self.max_error,
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> Self:
        """Versioned JSONを読み込む。不正データはValueErrorにする."""
        try:
            payload: Any = json.loads(path.read_text())
            if not isinstance(payload, dict) or payload.get("version") != JSON_VERSION:
                raise ValueError("未対応のXYキャリブレーションJSONです")
            grid = XYCalibrationGrid(**payload["grid"])
            logical = tuple(
                Point2d(float(x), float(y)) for x, y in payload["logical_points"]
            )
            raw = tuple(Point2d(float(x), float(y)) for x, y in payload["raw_points"])
            errors = tuple(
                Point2d(float(x), float(y))
                for x, y in payload.get("verification_errors", ())
            )
            result = cls(
                grid=grid,
                logical_points=logical,
                raw_points=raw,
                verification_errors=errors,
                calibrated_at=str(payload["calibrated_at"]),
            )
            transform = XYCalibrationTransform.from_data(grid, payload["transform"])
            # 保存係数が監査用logical/raw点を実際に結ぶことも検証する。
            for logical, expected_raw in zip(
                result.logical_points, result.raw_points, strict=True
            ):
                actual_raw = transform.apply(logical)
                if (actual_raw - expected_raw).norm > 1e-6:
                    raise ValueError("保存係数と計測点が一致しません")
            object.__setattr__(result, "_fitted_transform", transform)
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"XYキャリブレーションJSONが不正です: {path}") from exc


def migrate_machine_xy_settings(
    machine: Machine,
    old_transform: XYCalibrationTransform,
    new_transform: XYCalibrationTransform,
) -> dict[str, float]:
    """既存のlogical設定が示す物理raw位置を新座標系で維持する."""

    def migrate(point: Point2d) -> Point2d:
        return new_transform.inverse(old_transform.apply(point))

    reference = machine.reference_point.to_point()
    migrated_reference = migrate(reference)
    values = {
        "reference_point.x": migrated_reference.x,
        "reference_point.y": migrated_reference.y,
    }
    cap = machine.nozzle_cap
    if cap is not None:
        migrated_cap = migrate(Point2d(cap.x, cap.y))
        values.update({"nozzle_cap.x": migrated_cap.x, "nozzle_cap.y": migrated_cap.y})
    if machine.machine_type == "paste":
        toolhead = machine.paste_dispenser.toolhead
        migrated_tip = migrate(reference + Point2d(toolhead.x, toolhead.y))
        offset = migrated_tip - migrated_reference
        values.update(
            {
                "paste_dispenser.toolhead.x": offset.x,
                "paste_dispenser.toolhead.y": offset.y,
            }
        )
    return values


def _fit_rigid(local: _Array, raw: _Array) -> tuple[_Array, _Array]:
    local_center = local.mean(axis=0)
    raw_center = raw.mean(axis=0)
    covariance = (local - local_center).T @ (raw - raw_center)
    u, _, vt = np.linalg.svd(covariance)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    translation = raw_center - rotation @ local_center
    return rotation, translation


def _points_array(points: tuple[Point2d, ...]) -> _Array:
    return np.array([(point.x, point.y) for point in points], dtype=float)


def _matrix_tuple(matrix: _Array) -> tuple[tuple[float, float], tuple[float, float]]:
    return (
        (float(matrix[0, 0]), float(matrix[0, 1])),
        (float(matrix[1, 0]), float(matrix[1, 1])),
    )


def _point_tuple(point: _Array) -> tuple[float, float]:
    return float(point[0]), float(point[1])


def _affine_tuple(
    matrix: _Array,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    return (
        (float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[0, 2])),
        (float(matrix[1, 0]), float(matrix[1, 1]), float(matrix[1, 2])),
    )


def _triangle_acceptor(
    x0: float, y0: float, spacing: float, lower: bool, tolerance: float
) -> Callable[[_Array], bool]:
    def accept(value: _Array) -> bool:
        fx = (value[0] - x0) / spacing
        fy = (value[1] - y0) / spacing
        in_cell = (
            -tolerance <= fx <= 1.0 + tolerance and -tolerance <= fy <= 1.0 + tolerance
        )
        return in_cell and (fy <= fx + tolerance if lower else fy >= fx - tolerance)

    return accept


def _vertical_acceptor(
    y0: float, spacing: float, left: bool, max_x: float, tolerance: float
) -> Callable[[_Array], bool]:
    def accept(value: _Array) -> bool:
        return y0 - tolerance <= value[1] <= y0 + spacing + tolerance and (
            value[0] <= tolerance if left else value[0] >= max_x - tolerance
        )

    return accept


def _horizontal_acceptor(
    x0: float, spacing: float, top: bool, max_y: float, tolerance: float
) -> Callable[[_Array], bool]:
    def accept(value: _Array) -> bool:
        return x0 - tolerance <= value[0] <= x0 + spacing + tolerance and (
            value[1] <= tolerance if top else value[1] >= max_y - tolerance
        )

    return accept


def _corner_acceptor(
    left: bool,
    top: bool,
    max_x: float,
    max_y: float,
    tolerance: float,
) -> Callable[[_Array], bool]:
    def accept(value: _Array) -> bool:
        x_ok = value[0] <= tolerance if left else value[0] >= max_x - tolerance
        y_ok = value[1] <= tolerance if top else value[1] >= max_y - tolerance
        return x_ok and y_ok

    return accept
