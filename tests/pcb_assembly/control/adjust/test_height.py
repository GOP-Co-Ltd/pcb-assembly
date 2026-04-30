"""HeightPointsMeasurerのテスト."""

from unittest.mock import MagicMock

import pytest
from shapely.geometry import Polygon

from pcb_assembly.control.adjust import HeightPointsMeasurer
from pcb_assembly.geometry import HeightPlane
from pcb_assembly.pcb import Copper, Layer

_SAMPLING_KWARGS = {"min_radius": 1.5, "min_samples": 3, "max_samples": 9}


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
        large_copper,
    ):
        """measureがHeightPlaneを返すことを確認."""
        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_SAMPLING_KWARGS,
        )
        result = measurer.measure(
            coppers=[large_copper], board_to_machine=mock_board_to_machine
        )

        assert isinstance(result, HeightPlane)

    def test_measure_probe_call_count_matches_points(
        self,
        mock_probe_executor,
        mock_klipper,
        mock_stage,
        mock_board_to_machine,
        large_copper,
    ):
        """probe呼び出し回数が返却点数と一致することを確認."""
        measurer = HeightPointsMeasurer(
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
        measurer = HeightPointsMeasurer(
            probe_executor=mock_probe_executor,
            klipper=mock_klipper,
            stage=mock_stage,
            **_SAMPLING_KWARGS,
        )
        with pytest.raises(ValueError):
            measurer.measure(coppers=[tiny], board_to_machine=mock_board_to_machine)
