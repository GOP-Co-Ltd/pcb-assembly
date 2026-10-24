"""`pcbasm.pasting.paste_volume.plot` の仕様テスト（PNG として読めることだけを確かめる）."""

import cv2
import numpy as np
import pytest

from pcbasm.pasting.paste_volume.aggregate import DotDiameter
from pcbasm.pasting.paste_volume.fit import CellMeasurement
from pcbasm.pasting.paste_volume.model import CubicVolumeModel
from pcbasm.pasting.paste_volume.plot import (
    render_calibration_scatter,
    render_detection_montage,
)


class TestPasteVolumeRender:
    """直径ベース校正の診断図（散布とモンタージュ）."""

    @staticmethod
    def _cells():
        def cell(index: int, diameter: float, volume: float, blank: bool = False):
            return CellMeasurement(
                index=index,
                blank=blank,
                measured_volume_ul=volume,
                diameter=DotDiameter(
                    diameter_mm=diameter,
                    view_count=2,
                    detected_view_count=0 if blank else 2,
                    view_diameters_mm=() if blank else (diameter, diameter),
                    spread_mm=0.0,
                ),
            )

        return (
            cell(1, 0.59, 0.05),
            cell(2, 0.76, 0.125),
            cell(3, 0.86, 0.20),
            cell(4, 0.0, 0.0, blank=True),
        )

    @staticmethod
    def _model():
        return CubicVolumeModel(
            cubic_ul_per_mm3=0.3,
            quadratic_ul_per_mm2=0.02,
            linear_ul_per_mm=0.01,
            diameter_min_mm=0.59,
            diameter_max_mm=0.86,
        )

    @pytest.mark.parametrize(
        "with_blank", [True, False], ids=["with-blank", "without-blank"]
    )
    def test_render_calibration_scatter_outputs_readable_png(
        self, tmp_path, with_blank: bool
    ):
        cells = self._cells()
        if not with_blank:
            cells = tuple(cell for cell in cells if not cell.blank)
        output = tmp_path / "scatter.png"

        render_calibration_scatter(
            cells, self._model(), "paste-1 / n0.30 / h0.20", output
        )

        image = cv2.imread(str(output))
        assert image is not None
        assert image.shape[0] > 100 and image.shape[1] > 100

    def test_render_detection_montage_outputs_readable_png(self, tmp_path):
        panel = np.zeros((53, 53, 3), dtype=np.uint8)
        mask = np.zeros((53, 53), dtype=np.uint8)
        output = tmp_path / "montage.png"

        render_detection_montage(
            [("small", panel, panel, mask), ("blank", panel, panel, mask)],
            "detection",
            output,
        )

        image = cv2.imread(str(output))
        assert image is not None
        assert image.shape[0] > 100 and image.shape[1] > 100

    def test_render_detection_montage_rejects_an_empty_panel_list(self, tmp_path):
        with pytest.raises(ValueError, match="モンタージュ"):
            render_detection_montage([], "detection", tmp_path / "montage.png")
