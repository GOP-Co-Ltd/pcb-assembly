"""XY stage grid calibration public-contract tests."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from pcbasm.config import Machine
from pcbasm.geometry import Path as MotionPath, Point2d, Point3d
from pcbasm.xy_calibration import (
    XYCalibrationGrid,
    XYCalibrationResult,
    XYCalibrationTransform,
    migrate_machine_xy_settings,
)
from tests.helpers import copy_testing_config


def _distorted_raw_points(grid: XYCalibrationGrid) -> tuple[Point2d, ...]:
    """Return a smooth global distortion plus one local grid-node residual."""
    center_index = (grid.rows // 2) * grid.columns + grid.columns // 2
    points: list[Point2d] = []
    for index, point in enumerate(grid.points()):
        local_x = 0.18 if index == center_index else 0.0
        local_y = -0.12 if index == center_index else 0.0
        points.append(
            Point2d(
                x=2.0 + 1.012 * point.x + 0.004 * point.y + local_x,
                y=3.0 - 0.003 * point.x + 0.994 * point.y + local_y,
            )
        )
    return tuple(points)


def _assert_point(actual: Point2d, expected: Point2d, *, abs: float = 1e-8) -> None:
    assert actual.x == pytest.approx(expected.x, abs=abs)
    assert actual.y == pytest.approx(expected.y, abs=abs)


class TestXYCalibrationGrid:
    def test_points_are_row_major_at_the_configured_spacing(self):
        grid = XYCalibrationGrid(
            hole_diameter_mm=3.0,
            spacing_mm=10.0,
            rows=2,
            columns=3,
        )

        assert grid.points() == (
            Point2d(0.0, 0.0),
            Point2d(10.0, 0.0),
            Point2d(20.0, 0.0),
            Point2d(0.0, 10.0),
            Point2d(10.0, 10.0),
            Point2d(20.0, 10.0),
        )

    def test_detection_roi_is_one_grid_interval_square(self):
        grid = XYCalibrationGrid(3.0, 10.0, 5, 5)

        assert grid.detection_roi_size(pixel_per_mm=40.0) == (400, 400)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"hole_diameter_mm": 0.0},
            {"hole_diameter_mm": math.inf},
            {"spacing_mm": 0.0},
            {"spacing_mm": math.nan},
            {"rows": 1},
            {"columns": 1},
            {"hole_diameter_mm": 10.0},
        ],
    )
    def test_rejects_an_unmeasurable_grid(self, kwargs: dict[str, object]):
        values: dict[str, object] = {
            "hole_diameter_mm": 3.0,
            "spacing_mm": 10.0,
            "rows": 5,
            "columns": 5,
        }
        values.update(kwargs)

        with pytest.raises(ValueError):
            XYCalibrationGrid(**values)  # type: ignore[arg-type]


class TestXYCalibrationResult:
    @pytest.fixture
    def result(self) -> XYCalibrationResult:
        grid = XYCalibrationGrid(3.0, 10.0, 3, 3)
        return XYCalibrationResult.fit(
            grid,
            _distorted_raw_points(grid),
            verification_errors=(
                Point2d(0.01, 0.0),
                Point2d(0.0, 0.02),
                Point2d(0.018, 0.024),
            )
            * 3,
            calibrated_at="2026-08-03T12:34:00+00:00",
        )

    def test_transform_passes_through_every_measured_grid_node(
        self, result: XYCalibrationResult
    ):
        for logical, raw in zip(result.logical_points, result.raw_points, strict=True):
            _assert_point(result.transform.apply(logical), raw)

    def test_logical_grid_keeps_physical_spacing_and_right_angles(
        self, result: XYCalibrationResult
    ):
        origin = result.logical_points[0]
        x_step = result.logical_points[1] - origin
        y_step = result.logical_points[3] - origin

        assert x_step.norm == pytest.approx(result.grid.spacing_mm)
        assert y_step.norm == pytest.approx(result.grid.spacing_mm)
        assert x_step.x * y_step.x + x_step.y * y_step.y == pytest.approx(0.0)

    def test_local_residual_is_clamped_beyond_the_grid_boundary(
        self, result: XYCalibrationResult
    ):
        logical_array = np.array(
            [(point.x, point.y, 1.0) for point in result.logical_points]
        )
        raw_array = np.array([(point.x, point.y) for point in result.raw_points])
        global_affine, *_ = np.linalg.lstsq(logical_array, raw_array, rcond=None)
        boundary = result.logical_points[5]
        outward = result.logical_points[5] - result.logical_points[4]
        outside = boundary + outward * 0.5

        boundary_raw = result.transform.apply(boundary)
        outside_raw = result.transform.apply(outside)
        boundary_global = np.array((boundary.x, boundary.y, 1.0)) @ global_affine
        outside_global = np.array((outside.x, outside.y, 1.0)) @ global_affine
        boundary_residual = np.array((boundary_raw.x, boundary_raw.y)) - boundary_global
        outside_residual = np.array((outside_raw.x, outside_raw.y)) - outside_global

        assert outside_residual == pytest.approx(boundary_residual, abs=1e-8)

    def test_inverse_round_trip_inside_and_outside_the_grid(
        self, result: XYCalibrationResult
    ):
        samples = (
            result.logical_points[0],
            result.logical_points[4],
            Point2d(
                (result.logical_points[0].x + result.logical_points[4].x) / 2,
                (result.logical_points[0].y + result.logical_points[4].y) / 2,
            ),
            Point2d(
                result.logical_points[-1].x + 15.0,
                result.logical_points[-1].y + 8.0,
            ),
        )

        for logical in samples:
            raw = result.transform.apply(logical)
            _assert_point(result.transform.inverse(raw), logical, abs=1e-7)

    def test_metrics_are_derived_from_verification_errors(
        self, result: XYCalibrationResult
    ):
        assert result.rms_error == pytest.approx(
            math.sqrt((0.01**2 + 0.02**2 + 0.03**2) / 3)
        )
        assert result.max_error == pytest.approx(0.03)

    def test_transform_path_splits_mesh_crossings_and_preserves_z(
        self, result: XYCalibrationResult
    ):
        start = result.logical_points[3]
        end = result.logical_points[5]
        path = MotionPath([start.to3d(z=7.5), end.to3d(z=7.5)])

        transformed = result.transform.transform_path(path)

        assert len(transformed) > len(path)
        expected_start = result.transform.apply(start)
        expected_end = result.transform.apply(end)
        assert transformed[0] == Point3d(expected_start.x, expected_start.y, 7.5)
        assert transformed[-1] == Point3d(expected_end.x, expected_end.y, 7.5)
        assert all(point.z == 7.5 for point in transformed)

    def test_save_load_round_trip_rebuilds_the_same_mapping(
        self, result: XYCalibrationResult, tmp_path: Path
    ):
        path = tmp_path / "xy_calibration.json"

        result.save(path)
        loaded = XYCalibrationResult.load(path)

        assert loaded.grid == result.grid
        assert loaded.raw_points == result.raw_points
        assert loaded.verification_errors == result.verification_errors
        assert loaded.calibrated_at == result.calibrated_at
        for point in result.logical_points:
            _assert_point(loaded.transform.apply(point), result.transform.apply(point))

    def test_load_rejects_an_unknown_schema_version(
        self, result: XYCalibrationResult, tmp_path: Path
    ):
        path = tmp_path / "xy_calibration.json"
        result.save(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError):
            XYCalibrationResult.load(path)

    @pytest.mark.parametrize(
        "raw_points",
        [
            (Point2d(0.0, 0.0),),
            (
                Point2d(0.0, 0.0),
                Point2d(math.nan, 0.0),
                Point2d(0.0, 10.0),
                Point2d(10.0, 10.0),
            ),
            (
                Point2d(0.0, 0.0),
                Point2d(10.0, 0.0),
                Point2d(0.0, 10.0),
                Point2d(4.0, -1.0),
            ),
        ],
    )
    def test_fit_rejects_missing_non_finite_or_folded_measurements(
        self, raw_points: tuple[Point2d, ...]
    ):
        grid = XYCalibrationGrid(3.0, 10.0, 2, 2)

        with pytest.raises(ValueError):
            XYCalibrationResult.fit(grid, raw_points)


class TestXYCalibrationTransform:
    def test_identity_leaves_points_and_paths_unchanged(self):
        transform = XYCalibrationTransform.identity()
        point = Point2d(12.5, 34.5)
        path = MotionPath([Point3d(1.0, 2.0, 3.0), Point3d(4.0, 5.0, 6.0)])

        assert transform.apply(point) == point
        assert transform.inverse(point) == point
        assert transform.transform_path(path) == path


class TestMigrateMachineXYSettings:
    def test_preserves_physical_positions_and_rebases_toolhead_vector(
        self, tmp_path: Path
    ):
        config_dir = copy_testing_config(tmp_path)
        machine_path = config_dir / "machine.toml"
        machine_path.write_text(
            machine_path.read_text(encoding="utf-8")
            + "\n[nozzle_cap]\nx = 67.3\ny = 52.0\nz = -24.0\n",
            encoding="utf-8",
        )
        machine = Machine(machine_path)
        old_transform = XYCalibrationTransform.identity()
        grid = XYCalibrationGrid(3.0, 10.0, 3, 3)
        new_transform = XYCalibrationResult.fit(
            grid, _distorted_raw_points(grid)
        ).transform

        migrated = migrate_machine_xy_settings(machine, old_transform, new_transform)

        reference = Point2d(machine.reference_point.x, machine.reference_point.y)
        expected_reference = new_transform.inverse(old_transform.apply(reference))
        assert migrated["reference_point.x"] == pytest.approx(expected_reference.x)
        assert migrated["reference_point.y"] == pytest.approx(expected_reference.y)

        cap = machine.nozzle_cap
        assert cap is not None
        expected_cap = new_transform.inverse(old_transform.apply(Point2d(cap.x, cap.y)))
        assert migrated["nozzle_cap.x"] == pytest.approx(expected_cap.x)
        assert migrated["nozzle_cap.y"] == pytest.approx(expected_cap.y)

        toolhead = machine.paste_dispenser.toolhead
        old_tip = Point2d(reference.x + toolhead.x, reference.y + toolhead.y)
        expected_tip = new_transform.inverse(old_transform.apply(old_tip))
        assert migrated["paste_dispenser.toolhead.x"] == pytest.approx(
            expected_tip.x - expected_reference.x
        )
        assert migrated["paste_dispenser.toolhead.y"] == pytest.approx(
            expected_tip.y - expected_reference.y
        )
