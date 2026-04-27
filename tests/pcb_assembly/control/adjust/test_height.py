"""HeightTransformMeasurer/HeightPointsMeasurerのテスト."""

from unittest.mock import MagicMock

import pytest
from shapely.geometry import Polygon

from pcb_assembly.config import Probe as ProbeConfig
from pcb_assembly.control.adjust import HeightPointsMeasurer, HeightTransformMeasurer
from pcb_assembly.geometry import HeightPoints
from pcb_assembly.pcb import Copper, Layer


class TestHeightTransformMeasurer:
    """HeightTransformMeasurerのテスト."""

    @pytest.fixture
    def mock_klipper(self):
        klipper = MagicMock()
        return klipper

    @pytest.fixture
    def mock_probe_executor(self):
        probe_executor = MagicMock()
        return probe_executor

    @pytest.fixture
    def mock_stage(self):
        stage = MagicMock()
        stage.max_velocity = 100.0
        stage.to_gcode.return_value = MagicMock()  # Returns GCode-like object
        return stage

    @pytest.fixture
    def mock_outline(self):
        outline = MagicMock()
        outline.width = 50.0
        outline.height = 40.0
        return outline

    @pytest.fixture
    def mock_board_to_machine(self):
        """Identity transform (board coords = machine coords)."""
        transform = MagicMock()
        transform.apply.side_effect = lambda pt: pt
        return transform

    def test_measure_grid_dimensions(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """HeightMapのグリッドサイズが指定通りであることを確認."""
        mock_probe_executor.probe.return_value = -1.0

        measurer = HeightTransformMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(3, 4),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        assert result.rows == 3
        assert result.cols == 4

    def test_measure_records_probe_values(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """プローブの計測値がHeightMapに記録されることを確認."""
        # Return different Z values for each probe
        z_values = [-1.0, -1.5, -2.0, -2.5]
        mock_probe_executor.probe.side_effect = z_values

        measurer = HeightTransformMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(2, 2),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        assert result.z_values[0, 0] == pytest.approx(-1.0)
        assert result.z_values[0, 1] == pytest.approx(-1.5)
        assert result.z_values[1, 0] == pytest.approx(-2.0)
        assert result.z_values[1, 1] == pytest.approx(-2.5)

    def test_measure_height_map_bounds(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """HeightMapのBoard座標範囲がinset分内側であることを確認."""
        mock_probe_executor.probe.return_value = -1.0

        measurer = HeightTransformMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(2, 2),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        # outline.width=50, height=40, inset=5
        assert result.x_min == pytest.approx(5.0)
        assert result.x_max == pytest.approx(45.0)
        assert result.y_min == pytest.approx(5.0)
        assert result.y_max == pytest.approx(35.0)


class TestHeightPointsMeasurer:
    """HeightPointsMeasurerのテスト."""

    @pytest.fixture
    def mock_klipper(self):
        return MagicMock()

    @pytest.fixture
    def mock_probe_executor(self):
        executor = MagicMock()
        executor.probe.return_value = -1.0
        return executor

    @pytest.fixture
    def mock_stage(self):
        stage = MagicMock()
        stage.max_velocity = 100.0
        stage.to_gcode.return_value = MagicMock()
        return stage

    @pytest.fixture
    def mock_board_to_machine(self):
        """Identity transform (board coords = machine coords)."""
        transform = MagicMock()
        transform.apply.side_effect = lambda pt: pt
        return transform

    @pytest.fixture
    def probe_config(self):
        return ProbeConfig(
            servo_name="servo",
            revolution_distance=10.0,
            down_distance=5.0,
            min_radius=1.5,
            min_samples=3,
            max_samples=9,
        )

    @pytest.fixture
    def large_copper(self):
        """十分な候補点が得られる大きな矩形銅箔."""
        polygon = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
        return Copper(layer=Layer.TOP, polygon=polygon)

    def test_measure_returns_height_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        probe_config,
        large_copper,
    ):
        """measureがHeightPointsを返すことを確認."""
        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            probe_config=probe_config,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
        )

        assert isinstance(result, HeightPoints)

    def test_measure_probe_call_count_within_bounds(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        probe_config,
        large_copper,
    ):
        """probeの呼び出し回数がmin_samples〜max_samplesの範囲内であることを確認."""
        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            probe_config=probe_config,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
        )

        assert (
            probe_config.min_samples <= len(result.points) <= probe_config.max_samples
        )
        assert mock_probe_executor.probe.call_count == len(result.points)

    def test_measure_raises_when_candidates_insufficient(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        probe_config,
    ):
        """min_samplesに満たない銅箔でValueErrorとなることを確認."""
        # 1辺0.5mmの小さな矩形は min_radius=1.5 のbufferで消える
        tiny = Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]),
        )
        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            probe_config=probe_config,
        )
        with pytest.raises(ValueError):
            measurer.measure(coppers=[tiny], board_to_machine=mock_board_to_machine)
