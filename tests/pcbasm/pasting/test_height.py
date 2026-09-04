"""HeightPlaneMeasurerのテスト."""

import pytest
from shapely.geometry import Point as ShapelyPoint, Polygon

from pcbasm.config import Probe
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    Compose,
    HeightPlane,
    Identity,
    Point3d,
    Scale,
    Shift,
    sample_points_in_polygons,
    sort_by_nearest,
)
from pcbasm.pasting.height import HeightPlaneMeasurer, plan_probe_points
from pcbasm.pcb import Copper, Layer

_SAMPLING_KWARGS = {"min_radius": 1.5, "min_samples": 3, "max_samples": 9}
_BOARD_EDGE_MARGIN = 2.5
_MEASURER_KWARGS = {
    **_SAMPLING_KWARGS,
    "board_edge_margin": _BOARD_EDGE_MARGIN,
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

    def test_measure_returns_height_plane(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """measureがHeightPlaneを返すことを確認."""
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper],
            board_to_machine=mock_board_to_machine,
            outline=large_copper.polygon,
        )

        assert isinstance(result, HeightPlane)

    def test_measure_probe_call_count_matches_sample_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """probe呼び出し回数が返却点数と一致することを確認."""
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper],
            board_to_machine=mock_board_to_machine,
            outline=large_copper.polygon,
        )

        assert mock_probe_executor.probe.call_count == len(result.points)

    def test_measure_raises_when_candidates_insufficient(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
    ):
        """min_samplesに満たない銅箔でValueErrorとなることを確認."""
        # 1辺0.5mmの小さな矩形は min_radius=1.5 のbufferで消える
        tiny = Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]),
        )
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )
        outline = Polygon([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])

        with pytest.raises(ValueError, match="min_samples"):
            measurer.measure(
                coppers=[tiny],
                board_to_machine=mock_board_to_machine,
                outline=outline,
            )

        mock_stage.move.assert_not_called()
        mock_probe_executor.probe.assert_not_called()
        mock_klipper.send_gcode.assert_not_called()

    def test_move_targets_match_recorded_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """Move コマンドが記録点と同じ座標(Identity変換)で発行されることを確認."""
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_MEASURER_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper],
            board_to_machine=mock_board_to_machine,
            outline=large_copper.polygon,
        )

        move_targets = sorted(
            (c.kwargs["x"], c.kwargs["y"]) for c in mock_stage.move.call_args_list
        )
        recorded = sorted((p.x, p.y) for p in result.points)
        assert move_targets == recorded

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
        config = Probe(
            min_radius=1.5, board_edge_margin=2.5, min_samples=6, max_samples=9
        )

        points = plan_probe_points([copper], copper.polygon, config=config)

        assert 6 <= len(points) <= 9
        safe = copper.polygon.buffer(-2.5)
        for point in points:
            assert safe.covers(ShapelyPoint(point.x, point.y))

    def test_points_match_measurer_plan_with_same_settings(self, mocker):
        copper = self._copper()
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        measurer = HeightPlaneMeasurer(
            probe_executor=mocker.Mock(),
            klipper=mocker.Mock(),
            stage=stage,
            min_radius=1.5,
            board_edge_margin=2.5,
            min_samples=6,
            max_samples=9,
        )
        config = Probe(
            min_radius=1.5, board_edge_margin=2.5, min_samples=6, max_samples=9
        )

        assert measurer.plan_points([copper], copper.polygon) == plan_probe_points(
            [copper], copper.polygon, config=config
        )

    def test_insufficient_candidates_raise_before_any_motion(self):
        tiny = Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]),
        )

        outline = Polygon([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])

        with pytest.raises(ValueError, match="min_samples"):
            plan_probe_points([tiny], outline, config=Probe(min_radius=1.5))
