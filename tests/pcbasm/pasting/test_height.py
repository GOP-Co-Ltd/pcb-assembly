"""pcbasm.pasting.height のテスト."""

import cv2
import numpy as np
import pytest
from shapely.geometry import Point as ShapelyPoint, Polygon

from pcbasm.board import Copper, Layer
from pcbasm.config.probe import ProbeConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    Compose,
    HeightPlane,
    Identity,
    Point2d,
    Point3d,
    Scale,
    Shift,
    sample_points_in_polygons,
    sort_by_nearest,
)
from pcbasm.kicad import load_board
from pcbasm.pasting.height import (
    HeightPlaneMeasurer,
    plan_probe_points,
    render_height_plane,
    render_planned_points,
)
from tests.support.paths import TESTING_DATA_DIR

_SAMPLING_KWARGS = {"min_radius": 1.5, "min_samples": 3, "max_samples": 9}
_BOARD_EDGE_MARGIN = 2.5
# TOP 銅箔ゾーンを持つ実 PCB（基板背景の銅箔描画を含む）
LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
_MEASURER_KWARGS = {
    **_SAMPLING_KWARGS,
    "board_edge_margin": _BOARD_EDGE_MARGIN,
    "settle_sec": 0.0,
}


class TestHeightPlaneMeasurer:
    """HeightPlaneMeasurerのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker):
        return mocker.Mock()

    @pytest.fixture
    def mock_probe_executor(self, mocker):
        executor = mocker.Mock()
        executor.probe.return_value = -1.0
        return executor

    @pytest.fixture
    def mock_stage(self, mocker):
        # stage.move の戻り値は `+ GCode.wait(...)` で連結されるため実体の GCode を返す
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.get_position.return_value = Point3d(0.0, 0.0, 0.0)
        stage.move.return_value = GCode()
        return stage

    @pytest.fixture
    def mock_board_to_machine(self):
        """Identity transform (board coords = machine coords)."""
        return Identity()

    @pytest.fixture
    def large_copper(self):
        """十分な候補点が得られる大きな矩形銅箔."""
        polygon = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
        return Copper(layer=Layer.TOP, polygon=polygon)

    def test_measure_records_machine_xy_probe_targets(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        large_copper,
    ):
        """返却 HeightPlane の XY は board ではなく実 probe 先の machine XY."""
        board_to_machine = Compose(
            [
                Scale(x=2.0, y=-1.0, z=1.0),
                Shift(x=100.0, y=30.0, z=0.0),
            ]
        )
        probe_zs = [-1.0 - 0.05 * i for i in range(_SAMPLING_KWARGS["max_samples"])]
        mock_probe_executor.probe.side_effect = probe_zs
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )

        result = measurer.measure(
            coppers=[large_copper],
            board_to_machine=board_to_machine,
            outline=large_copper.polygon,
        )

        move_targets = [
            (call.kwargs["x"], call.kwargs["y"])
            for call in mock_stage.move.call_args_list
        ]
        assert len(result.points) == len(move_targets)
        for point, move_target in zip(result.points, move_targets):
            assert (point.x, point.y) == pytest.approx(move_target)
        assert [p.z for p in result.points] == pytest.approx(
            probe_zs[: len(result.points)]
        )

    def test_measure_keeps_probe_points_inside_board_margin(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """返却点が基板outlineから設定距離以上内側に収まる."""
        outline = large_copper.polygon
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )

        result = measurer.measure(
            coppers=[large_copper],
            board_to_machine=mock_board_to_machine,
            outline=outline,
        )

        safe_outline = outline.buffer(-_BOARD_EDGE_MARGIN)
        for point in result.points:
            assert safe_outline.covers(ShapelyPoint(point.x, point.y))

    def test_measure_routes_probe_points_by_nearest_actual_move_targets(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        large_copper,
    ):
        """現在位置から変換後の実移動先が近くなる順にprobeする."""
        board_to_machine = Compose(
            [
                Scale(x=-1.0, y=1.0, z=1.0),
                Shift(x=100.0, y=20.0, z=0.0),
            ]
        )
        mock_stage.get_position.return_value = Point3d(105.0, 8.0, 0.0)
        board_points = sample_points_in_polygons(
            [large_copper.polygon],
            **_SAMPLING_KWARGS,
            outline=large_copper.polygon,
            outline_margin=_BOARD_EDGE_MARGIN,
        )

        def actual_move_target(board_point):
            machine_point = board_to_machine.apply(board_point)
            return Point3d(x=machine_point.x, y=machine_point.y, z=0.0)

        expected_board_points = sort_by_nearest(
            board_points,
            mock_stage.get_position.return_value,
            key=actual_move_target,
        )
        expected_move_targets = [
            (actual_move_target(p).x, actual_move_target(p).y)
            for p in expected_board_points
        ]
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )

        measurer.measure(
            coppers=[large_copper],
            board_to_machine=board_to_machine,
            outline=large_copper.polygon,
        )

        move_targets = [
            (call.kwargs["x"], call.kwargs["y"])
            for call in mock_stage.move.call_args_list
        ]
        assert move_targets == pytest.approx(expected_move_targets)


class TestPlanProbePoints:
    """装置を動かさない計測点計画（Machine の Probe 設定 / measurer 設定）."""

    def _copper(self) -> Copper:
        return Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]),
        )

    def test_points_stay_inside_copper_and_board_margin(self):
        copper = self._copper()
        config = ProbeConfig(
            min_radius=1.5, board_edge_margin=2.5, min_samples=6, max_samples=9
        )

        points = plan_probe_points([copper], copper.polygon, config=config)

        assert 6 <= len(points) <= 9
        safe = copper.polygon.buffer(-2.5)
        for point in points:
            assert safe.covers(ShapelyPoint(point.x, point.y))

    def test_insufficient_candidates_raise_before_any_motion(self):
        tiny = Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]),
        )

        outline = Polygon([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])

        with pytest.raises(ValueError, match="min_samples"):
            plan_probe_points([tiny], outline, config=ProbeConfig(min_radius=1.5))


def _spread_points_in_outline(pcb, fractions):
    """Outline bbox 内の指定比率位置に Point2d を置く（基板に依存しない配置）."""
    minx, miny, maxx, maxy = pcb.outline.polygon.bounds
    return [
        Point2d(minx + fx * (maxx - minx), miny + fy * (maxy - miny))
        for fx, fy in fractions
    ]


class TestHeightRender:
    """render_planned_points / render_height_plane（PNG として読めることだけを確かめる）."""

    def test_render_planned_points_outputs_readable_png(self, tmp_path):
        pcb = load_board(LED_BLINKER_PCB)
        # 凸包（薄線描画）が成立する 4 点
        points = _spread_points_in_outline(
            pcb, [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
        )
        output = tmp_path / "planned_points.png"

        render_planned_points(points, pcb, "Planned probe points", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_planned_points_accepts_fewer_than_hull_points(self, tmp_path):
        """凸包が成立しない 2 点でも描画できる（境界ケース）."""
        pcb = load_board(LED_BLINKER_PCB)
        points = _spread_points_in_outline(pcb, [(0.3, 0.5), (0.7, 0.5)])
        output = tmp_path / "planned_two.png"

        render_planned_points(points, pcb, "Planned (2 points)", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_height_plane_outputs_readable_png(self, tmp_path):
        pcb = load_board(LED_BLINKER_PCB)
        # 2 次曲面フィットに必要な非退化 6 点（z は緩い傾斜）
        fractions = [
            (0.1, 0.1),
            (0.9, 0.1),
            (0.1, 0.9),
            (0.9, 0.9),
            (0.5, 0.3),
            (0.3, 0.6),
        ]
        points = _spread_points_in_outline(pcb, fractions)
        height_plane = HeightPlane(
            tuple(Point3d(p.x, p.y, 0.01 * p.x + 0.02 * p.y) for p in points)
        )
        output = tmp_path / "height_plane.png"

        render_height_plane(height_plane, pcb, "Height Plane", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_height_plane_accepts_pcb_to_plane_frame(self, tmp_path):
        pcb = load_board(LED_BLINKER_PCB)
        pcb_to_plane = Shift(x=120.0, y=-80.0, z=0.0)
        fractions = [
            (0.1, 0.1),
            (0.9, 0.1),
            (0.1, 0.9),
            (0.9, 0.9),
            (0.5, 0.3),
            (0.3, 0.6),
        ]
        board_points = _spread_points_in_outline(pcb, fractions)
        plane_points = [pcb_to_plane.apply(p) for p in board_points]
        height_plane = HeightPlane(
            tuple(Point3d(p.x, p.y, 0.01 * p.x + 0.02 * p.y) for p in plane_points)
        )
        identity_output = tmp_path / "height_plane_identity_frame.png"
        plane_output = tmp_path / "height_plane_machine_frame.png"

        render_height_plane(height_plane, pcb, "Height Plane", identity_output)
        render_height_plane(
            height_plane,
            pcb,
            "Height Plane",
            plane_output,
            pcb_to_plane=pcb_to_plane,
        )

        identity_image = cv2.imread(str(identity_output))
        plane_image = cv2.imread(str(plane_output))
        assert identity_image is not None
        assert plane_image is not None
        assert plane_image.size > 0
        assert plane_image.shape == identity_image.shape
        diff = np.asarray(cv2.absdiff(plane_image, identity_image))
        assert float(diff.mean()) > 0.1
