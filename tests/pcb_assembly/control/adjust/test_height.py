"""HeightTransformMeasurer/HeightPointsMeasurerのテスト."""

from unittest.mock import MagicMock

import pytest

from pcb_assembly.control.adjust import HeightPointsMeasurer, HeightTransformMeasurer
from pcb_assembly.geometry import HeightPoints


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
        return MagicMock()

    @pytest.fixture
    def mock_stage(self):
        stage = MagicMock()
        stage.max_velocity = 100.0
        stage.to_gcode.return_value = MagicMock()
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

    def test_measure_returns_height_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """measureがHeightPointsを返すことを確認."""
        mock_probe_executor.probe.return_value = -1.0

        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(3, 3),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        assert isinstance(result, HeightPoints)

    def test_measure_records_grid_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """計測点がgrid_size通りに記録されることを確認."""
        mock_probe_executor.probe.return_value = -1.0

        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(3, 4),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        assert len(result.points) == 3 * 4

    def test_measure_records_probe_values(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """プローブの計測値がHeightPointsに記録されることを確認."""
        z_values = [-1.0, -1.5, -2.0, -2.5]
        mock_probe_executor.probe.side_effect = z_values

        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            grid_size=(2, 2),
            inset=5.0,
        )
        result = measurer.measure(
            outline=mock_outline, board_to_machine=mock_board_to_machine
        )

        zs = [p.z for p in result.points]
        assert zs == pytest.approx(z_values)

    def test_measure_records_board_coordinates(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_outline,
        mock_board_to_machine,
    ):
        """Board座標（x, y）がinset分内側であることを確認."""
        mock_probe_executor.probe.return_value = -1.0

        measurer = HeightPointsMeasurer(
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
        xs = sorted({p.x for p in result.points})
        ys = sorted({p.y for p in result.points})
        assert xs == pytest.approx([5.0, 45.0])
        assert ys == pytest.approx([5.0, 35.0])
