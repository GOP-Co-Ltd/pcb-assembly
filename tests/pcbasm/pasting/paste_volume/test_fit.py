"""1 session を計測して校正へ組み立てる経路の公開契約.

実素材 ``data/testing/paste-volume/`` の pre/post を、schema v3 の metadata から
参照する session として組み立てて通す。合成画像では銅板テクスチャと照明ムラが
再現できず、blank ガードの効きを確かめられない。

教師体積は指令量比で配分したラベルなので点ごとの真値ではない。ここで固めるのは
「blank が 0 に落ちること」「検出失敗が出ないこと」「診断の意味」であって、
係数そのものではない。
"""

from pathlib import Path

import pytest

from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.detect import DotDetectionSpec
from pcbasm.pasting.paste_volume.fit import fit_session, measure_session
from tests.helpers import (
    PASTE_VOLUME_DISPENSED,
    PASTE_VOLUME_PIXEL_PER_MM,
    build_paste_volume_session,
)

DISPENSED = PASTE_VOLUME_DISPENSED
PIXEL_PER_MM = PASTE_VOLUME_PIXEL_PER_MM


@pytest.fixture
def session(tmp_path: Path) -> DatasetSession:
    root = build_paste_volume_session(
        tmp_path / "plate-47.5x20-20260909T145923.452+0900"
    )
    loaded, error = DatasetSession.load(root)
    assert error is None, error
    assert loaded is not None
    return loaded


class TestMeasureSession:
    """全セルの計測と、その正常系・異常系の境目."""

    def test_measures_every_cell_in_index_order(self, session: DatasetSession):
        cells, error = measure_session(session)

        assert error is None
        assert cells is not None
        assert [cell.index for cell in cells] == [1, 2, 3, 4, 5, 6, 7]

    def test_marks_the_blank_cell(self, session: DatasetSession):
        cells, _ = measure_session(session)
        assert cells is not None

        assert [cell.blank for cell in cells] == [False] * 6 + [True]

    def test_gives_the_blank_cell_a_zero_diameter(self, session: DatasetSession):
        cells, _ = measure_session(session)
        assert cells is not None

        assert cells[-1].diameter.diameter_mm == 0.0

    def test_detects_every_dispensed_cell(self, session: DatasetSession):
        cells, _ = measure_session(session)
        assert cells is not None

        dispensed = [cell for cell in cells if not cell.blank]
        assert all(cell.diameter.detected_view_count == 2 for cell in dispensed)

    def test_orders_diameters_by_commanded_volume(self, session: DatasetSession):
        cells, _ = measure_session(session)
        assert cells is not None

        diameters = [cell.diameter.diameter_mm for cell in cells if not cell.blank]
        assert diameters[0] < diameters[2] < diameters[4]

    def test_rejects_an_invalid_detection_spec(self, session: DatasetSession):
        cells, error = measure_session(
            session, spec=DotDetectionSpec(contrast_percentile=0.0)
        )

        assert cells is None
        assert error is not None

    def test_names_the_cell_when_an_image_is_missing(self, session: DatasetSession):
        (session.root / "post" / "000001.00.png").unlink()

        cells, error = measure_session(session)

        assert cells is None
        assert error is not None
        assert "セル1" in error


class TestFitSession:
    """校正ファイルの組み立てと診断."""

    def test_builds_a_calibration_that_validates(self, session: DatasetSession):
        fit, error = fit_session(session)

        assert error is None, error
        assert fit is not None
        assert fit.calibration.validate() is None

    def test_carries_the_conditions_from_the_metadata(self, session: DatasetSession):
        fit, _ = fit_session(session)
        assert fit is not None

        conditions = fit.calibration.conditions
        assert conditions.paste_id == session.metadata.paste.paste_id
        assert conditions.nozzle_diameter_mm == session.metadata.nozzle.diameter_mm
        assert conditions.pixel_per_mm == PIXEL_PER_MM

    def test_records_the_source_session(self, session: DatasetSession):
        fit, _ = fit_session(session)
        assert fit is not None

        source = fit.calibration.source
        assert source.session == session.label
        assert source.label_kind == "rotation_allocated"
        assert source.sample_count == len(DISPENSED)
        assert source.blank_count == 1
        assert len(source.metadata_sha256) == 64

    def test_embeds_the_detection_spec_used_for_the_fit(self, session: DatasetSession):
        spec = DotDetectionSpec(min_contrast=25.0)

        fit, error = fit_session(session, spec=spec)

        assert error is None, error
        assert fit is not None
        assert fit.calibration.detection == spec

    def test_reports_no_blank_false_positive_and_no_detection_failure(
        self, session: DatasetSession
    ):
        fit, _ = fit_session(session)
        assert fit is not None

        diagnostics = fit.calibration.diagnostics
        assert diagnostics.blank_false_positive_count == 0
        assert diagnostics.detection_failure_count == 0

    def test_keeps_the_model_monotonic_over_the_covered_range(
        self, session: DatasetSession
    ):
        fit, _ = fit_session(session)
        assert fit is not None

        assert fit.calibration.diagnostics.monotonic_in_range is True

    def test_predicts_one_volume_per_cell(self, session: DatasetSession):
        fit, _ = fit_session(session)
        assert fit is not None

        assert len(fit.predicted_volumes_ul) == len(fit.cells)

    def test_predicts_zero_for_the_blank_cell(self, session: DatasetSession):
        fit, _ = fit_session(session)
        assert fit is not None

        assert fit.predicted_volumes_ul[-1] == 0.0

    def test_keeps_the_total_volume_error_small(self, session: DatasetSession):
        fit, _ = fit_session(session)
        assert fit is not None

        assert abs(fit.calibration.diagnostics.total_relative_error) < 0.05

    def test_uses_the_default_label_built_from_the_conditions(
        self, session: DatasetSession
    ):
        fit, _ = fit_session(session)
        assert fit is not None

        assert fit.calibration.label == "paste-1 / n0.34 / h0.20"

    def test_keeps_an_explicit_label(self, session: DatasetSession):
        fit, _ = fit_session(session, label="S3X70 / n0.30 / h0.20")
        assert fit is not None

        assert fit.calibration.label == "S3X70 / n0.30 / h0.20"

    def test_rejects_a_blank_false_positive_by_default(self, tmp_path: Path):
        root = build_paste_volume_session(tmp_path / "plate-a", blank_material="large")
        loaded, _ = DatasetSession.load(root)
        assert loaded is not None

        fit, error = fit_session(loaded)

        assert fit is None
        assert error is not None
        assert "blank" in error

    def test_can_continue_past_a_blank_false_positive_on_request(self, tmp_path: Path):
        root = build_paste_volume_session(tmp_path / "plate-a", blank_material="large")
        loaded, _ = DatasetSession.load(root)
        assert loaded is not None

        fit, error = fit_session(loaded, require_blank_zero=False)

        assert error is None, error
        assert fit is not None
        assert fit.calibration.diagnostics.blank_false_positive_count == 1
