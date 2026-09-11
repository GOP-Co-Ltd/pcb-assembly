"""校正から塗布量を推定する具象推定器の公開契約.

抽象基底は作らない（直径以外の手法が来たら置き換わる）。推定できないことは例外に
せず ``accepted=False`` と理由文字列へ落とす。運転時キャリブレーションで 1 点の
失敗がジョブ全体を落とさないため。
"""

import json
from pathlib import Path

import attrs
import cv2
import pytest

from pcbasm.pasting.paste_volume.aggregate import DotDiameter
from pcbasm.pasting.paste_volume.calibration import (
    PasteVolumeCalibration,
    parse_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.estimator import (
    DiameterVolumeEstimator,
    PasteVolumePrediction,
    load_diameter_estimator,
)
from pcbasm.vision.image import ImageArray
from tests.helpers import TESTING_DATA_DIR

PIN = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"
MATERIAL_DIR = TESTING_DATA_DIR / "paste-volume"
PIXEL_PER_MM = 28.677782176153425


def _calibration() -> PasteVolumeCalibration:
    calibration, error = parse_calibration(json.loads(PIN.read_text(encoding="utf-8")))

    assert error is None, error
    assert calibration is not None
    return calibration


def _load(name: str, phase: str, view: int = 0) -> ImageArray:
    image = cv2.imread(str(MATERIAL_DIR / name / phase / f"{view:02d}.png"))
    assert image is not None
    return image


def _predict(name: str, view: int = 0) -> PasteVolumePrediction:
    return DiameterVolumeEstimator(_calibration()).predict(
        _load(name, "pre", view),
        _load(name, "post", view),
        pixel_per_mm=PIXEL_PER_MM,
    )


class TestPredictOnRealMaterial:
    """実素材での推定."""

    def test_accepts_a_dispensed_cell_and_returns_a_positive_volume(self):
        prediction = _predict("large")

        assert prediction.accepted is True
        assert prediction.rejection_reason is None
        assert prediction.mean_volume_ul > 0.0

    def test_volume_grows_with_the_deposit(self):
        volumes = [
            _predict(name).mean_volume_ul for name in ("small", "medium", "large")
        ]

        assert volumes == sorted(volumes)

    def test_estimated_volume_is_near_the_allocated_label(self):
        """校正が当てているのは教師ラベルであって、指令量ではない.

        ``label.kind = "rotation_allocated"`` は総質量を指令回転数比で配分した値で、
        出典 session では指令量の 0.7135 倍（``large`` の 0.200 µL に対し 0.1427 µL）。

        許容は校正の点ごと残差に相当する ±15% に留める。
        """
        prediction = _predict("large")

        assert prediction.mean_volume_ul == pytest.approx(0.1427, rel=0.15)

    def test_uncertainty_comes_from_the_calibration_residual(self):
        calibration = _calibration()
        prediction = _predict("large")

        expected = calibration.diagnostics.residual_relative_std
        assert prediction.relative_std == pytest.approx(expected)
        assert prediction.std_volume_ul == pytest.approx(
            prediction.mean_volume_ul * expected
        )


class TestPredictRejections:
    """推定できないケースは例外にせず理由文字列へ落とす."""

    def test_blank_cell_is_not_accepted(self):
        """真値 0 は「補正の材料にならない」ので不採用。例外にはしない."""
        prediction = _predict("blank")

        assert prediction.accepted is False
        assert prediction.rejection_reason == "no_deposit_detected"
        assert prediction.mean_volume_ul == 0.0

    def test_a_diameter_outside_the_calibrated_range_is_not_accepted(self):
        """被覆域の外は外挿になるので断る."""
        calibration = _calibration()
        narrowed = attrs.evolve(
            calibration,
            model=attrs.evolve(
                calibration.model, diameter_min_mm=0.90, diameter_max_mm=0.95
            ),
        )

        prediction = DiameterVolumeEstimator(narrowed).predict(
            _load("small", "pre"), _load("small", "post"), pixel_per_mm=PIXEL_PER_MM
        )

        assert prediction.accepted is False
        assert prediction.rejection_reason == "diameter_out_of_calibrated_range"

    def test_a_structurally_invalid_image_is_reported_with_its_reason(self):
        prediction = DiameterVolumeEstimator(_calibration()).predict(
            _load("large", "pre"),
            _load("large", "post")[:10, :10],
            pixel_per_mm=PIXEL_PER_MM,
        )

        assert prediction.accepted is False
        assert prediction.rejection_reason is not None
        assert prediction.rejection_reason.startswith("image_invalid:")

    def test_a_scale_far_from_the_calibration_is_judged_by_the_covered_range(self):
        """条件照合は評価側の責務。推定器はスケール差そのものでは断らない.

        断ってしまうと、条件不一致の session を評価すること自体ができなくなる。

        ここでは被覆域を外れたことが理由として出る（直径が倍になるため）。
        """
        prediction = DiameterVolumeEstimator(_calibration()).predict(
            _load("large", "pre"),
            _load("large", "post"),
            pixel_per_mm=PIXEL_PER_MM / 2.0,
        )

        assert prediction.accepted is False
        assert prediction.rejection_reason == "diameter_out_of_calibrated_range"


class TestPredictViews:
    """マルチ view は検出失敗のフォールバック."""

    def test_uses_every_view(self):
        estimator = DiameterVolumeEstimator(_calibration())

        prediction = estimator.predict_views(
            [
                (_load("large", "pre", view), _load("large", "post", view))
                for view in (0, 1)
            ],
            pixel_per_mm=PIXEL_PER_MM,
        )

        assert prediction.accepted is True
        assert prediction.mean_volume_ul > 0.0

    def test_one_unusable_view_does_not_lose_the_cell(self):
        """1 view が blank 相当でも、残りで推定できる（フォールバックの契約）."""
        estimator = DiameterVolumeEstimator(_calibration())

        prediction = estimator.predict_views(
            [
                (_load("blank", "pre"), _load("blank", "post")),
                (_load("large", "pre"), _load("large", "post")),
            ],
            pixel_per_mm=PIXEL_PER_MM,
        )

        assert prediction.accepted is True
        assert prediction.mean_volume_ul > 0.0

    def test_no_views_at_all_is_told_apart_from_an_empty_cell(self):
        """View を渡していないことと、塗布が写っていないことは原因が別."""
        prediction = DiameterVolumeEstimator(_calibration()).predict_views(
            [], pixel_per_mm=PIXEL_PER_MM
        )

        assert prediction.accepted is False
        assert prediction.rejection_reason == "no_views"

    def test_median_aggregation_matches_estimating_each_view(self):
        """``median(V(dᵢ)) == V(median(dᵢ))``。集約が推定器の内と外で一致する根拠.

        3 次モデルが被覆域で単調なので成り立つ。平均だと崩れる。
        """
        calibration = _calibration()
        estimator = DiameterVolumeEstimator(calibration)
        views = [
            (_load(name, "pre"), _load(name, "post"))
            for name in ("small", "medium", "large")
        ]

        aggregated = estimator.predict_views(views, pixel_per_mm=PIXEL_PER_MM)
        per_view = sorted(
            estimator.predict(pre, post, pixel_per_mm=PIXEL_PER_MM).mean_volume_ul
            for pre, post in views
        )

        assert aggregated.mean_volume_ul == pytest.approx(per_view[1])


class TestLoadDiameterEstimator:
    """ファイルから推定器を組む."""

    def test_loads_from_a_calibration_file(self, tmp_path: Path):
        path = tmp_path / "sample.paste-volume.json"
        write_calibration(path, _calibration())

        estimator, error = load_diameter_estimator(path)

        assert error is None, error
        assert isinstance(estimator, DiameterVolumeEstimator)

    def test_reports_a_missing_file(self, tmp_path: Path):
        estimator, error = load_diameter_estimator(tmp_path / "absent.json")

        assert estimator is None
        assert error is not None

    def test_reports_a_broken_calibration(self, tmp_path: Path):
        path = tmp_path / "broken.paste-volume.json"
        path.write_text("{}", encoding="utf-8")

        estimator, error = load_diameter_estimator(path)

        assert estimator is None
        assert error is not None


class TestReliableRangeOnly:
    """運転時補正向けに、被覆域の下端付近を採用しないモード.

    既定は被覆域そのものを使う（校正の生成・検証はこれまでどおり）。
    """

    @staticmethod
    def _estimator(*, reliable_range_only: bool) -> DiameterVolumeEstimator:
        return DiameterVolumeEstimator(
            _calibration(), reliable_range_only=reliable_range_only
        )

    @staticmethod
    def _diameter(value: float) -> DotDiameter:
        return DotDiameter(
            diameter_mm=value,
            view_count=1,
            detected_view_count=1,
            view_diameters_mm=(value,),
            spread_mm=0.0,
        )

    def test_defaults_to_the_whole_covered_range(self):
        model = _calibration().model
        just_inside = self._diameter(model.diameter_min_mm + 1e-6)

        prediction = DiameterVolumeEstimator(_calibration()).predict_diameter(
            just_inside
        )

        assert prediction.accepted is True

    def test_rejects_the_lower_edge_of_the_covered_range(self):
        model = _calibration().model
        just_inside = self._diameter(model.diameter_min_mm + 1e-6)

        prediction = self._estimator(reliable_range_only=True).predict_diameter(
            just_inside
        )

        assert prediction.accepted is False
        assert prediction.rejection_reason == "diameter_below_reliable_range"

    def test_accepts_diameters_inside_the_reliable_range(self):
        model = _calibration().model
        inside = self._diameter(
            (model.reliable_diameter_min_mm + model.diameter_max_mm) / 2
        )

        prediction = self._estimator(reliable_range_only=True).predict_diameter(inside)

        assert prediction.accepted is True
        assert prediction.mean_volume_ul > 0.0

    def test_still_rejects_above_the_upper_bound_with_the_covered_range_reason(self):
        model = _calibration().model
        above = self._diameter(model.diameter_max_mm + 0.1)

        prediction = self._estimator(reliable_range_only=True).predict_diameter(above)

        assert prediction.accepted is False
        assert prediction.rejection_reason == "diameter_out_of_calibrated_range"

    def test_a_blank_is_still_reported_as_no_deposit(self):
        blank = DotDiameter(
            diameter_mm=0.0,
            view_count=1,
            detected_view_count=0,
            view_diameters_mm=(0.0,),
            spread_mm=0.0,
        )

        prediction = self._estimator(reliable_range_only=True).predict_diameter(blank)

        assert prediction.accepted is False
        assert prediction.rejection_reason == "no_deposit_detected"

    def test_load_diameter_estimator_can_build_the_reliable_range_estimator(
        self, tmp_path: Path
    ):
        path = tmp_path / "cal.paste-volume.json"
        write_calibration(path, _calibration())

        estimator, error = load_diameter_estimator(path, reliable_range_only=True)

        assert error is None
        assert estimator is not None
        model = estimator.calibration.model
        assert (
            estimator.predict_diameter(
                self._diameter(model.diameter_min_mm + 1e-6)
            ).rejection_reason
            == "diameter_below_reliable_range"
        )
