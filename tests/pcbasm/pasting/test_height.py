"""HeightPlaneMeasurerのテスト."""

import pytest
from shapely.geometry import Polygon

from pcbasm.gcode import GCode
from pcbasm.geometry import HeightPlane, Identity, Point2d
from pcbasm.pasting.height import HeightPlaneMeasurer
from pcbasm.pcb import Copper, Layer

_SAMPLING_KWARGS = {"min_radius": 1.5, "min_samples": 3, "max_samples": 9}


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
        # stage.move の戻り値は `+ gcode.wait(...)` で連結されるため実体の GCode を返す
        stage = mocker.Mock()
        stage.max_velocity = 100.0
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
            **_SAMPLING_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
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
            **_SAMPLING_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
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
            **_SAMPLING_KWARGS,
        )
        with pytest.raises(ValueError):
            measurer.measure(coppers=[tiny], board_to_machine=mock_board_to_machine)

    def test_probe_shift_offsets_recorded_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """probe_shiftを与えると記録点がその分ずれることを確認."""
        shift = (2.0, -3.0)

        def run(probe_shift):
            measurer = HeightPlaneMeasurer(
                probe_executor=mock_probe_executor,
                klipper=mock_klipper,
                stage=mock_stage,
                probe_shift=probe_shift,
                **_SAMPLING_KWARGS,
            )
            return measurer.measure(
                coppers=[large_copper], board_to_machine=mock_board_to_machine
            )

        # サンプリングは決定的なので base と shifted で同じ順序・点数になる
        base = run((0.0, 0.0))
        shifted = run(shift)

        assert [(p.x, p.y) for p in shifted.points] == [
            (p.x + shift[0], p.y + shift[1]) for p in base.points
        ]

    def test_probe_shift_applied_to_move_command(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """Move コマンドがシフト後の座標(=記録点, Identity逆変換)で発行されることを確認."""
        shift = (2.0, -3.0)
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            probe_shift=shift,
            **_SAMPLING_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
        )

        move_targets = sorted(
            (c.kwargs["x"], c.kwargs["y"]) for c in mock_stage.move.call_args_list
        )
        recorded = sorted((p.x, p.y) for p in result.points)
        assert move_targets == recorded

    def test_measure_passes_outline_to_sampling(
        self,
        mocker,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """outlineを指定するとsamplingへそのまま渡されることを確認."""
        outline = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
        sampler = mocker.patch(
            "pcbasm.pasting.height.sample_points_in_polygons",
            return_value=[
                Point2d(0.0, 0.0),
                Point2d(1.0, 0.0),
                Point2d(0.0, 1.0),
                Point2d(1.0, 1.0),
                Point2d(2.0, 0.0),
                Point2d(0.0, 2.0),
            ],
        )
        measurer = HeightPlaneMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_SAMPLING_KWARGS,
        )

        measurer.measure(
            coppers=[large_copper],
            board_to_machine=mock_board_to_machine,
            outline=outline,
        )

        assert sampler.call_args.kwargs["outline"] is outline
