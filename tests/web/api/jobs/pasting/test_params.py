"""塗布ジョブのパラメータの受理・保存契約."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.backend import standalone_catalog
from web.api.jobs.runtime.catalog import JobCatalog


@pytest.fixture
def default(tmp_path: Path) -> JobCatalog:
    return standalone_catalog(tmp_path)


class TestPastingParams:
    """塗布ジョブのパラメータ宣言（計画書「ジョブ定義表」のピン）."""

    def test_paste_volume_calibration_requires_a_paste_id(self, default: JobCatalog):
        """ペースト製品 ID はデータセットの素性なので既定値を持たず必須."""
        definition = default.get("paste_volume_calibration")

        with pytest.raises(ValueError, match="paste_id"):
            default.validate_params(definition, {})

    def test_paste_volume_calibration_drives_loading_from_the_form(
        self, default: JobCatalog
    ):
        """塗布パス先頭のローディングは体積・回転とも既定値をフォームから受ける.

        ローディング欄が無いと、`loading_loop` の待ち受けへ運転者が応答できなくなる。
        """
        definition = default.get("paste_volume_calibration")
        params = {spec.name: spec for spec in definition.params}

        assert definition.loading is not None
        assert definition.loading.amount == "loading_amount"
        assert definition.accepts_commands is True
        for name, unit in (
            ("loading_amount", "uL"),
            ("loading_rotations", "rev"),
            ("loading_rate", "rev/s"),
            ("loading_accel", "rev/s^2"),
            ("loading_retract_rotations", "rev"),
        ):
            assert params[name].unit == unit, name
            assert params[name].default is not None, name

    def test_toolhead_offset_paste_diameter_min_stays_positive(
        self, default: JobCatalog
    ):
        """最小直径 0（下限なし）は受けない（未塗布板のテクスチャを拾うため）."""
        definition = default.get("toolhead_offset")
        params = {spec.name: spec for spec in definition.params}

        # 実素材の未塗布板は 0.2 mm 相当の小片まで残る
        assert params["paste_diameter_min"].minimum == 0.3

        with pytest.raises(ValueError) as exc_info:
            default.validate_params(definition, {"paste_diameter_min": 0.2})

        assert "paste_diameter_min" in str(exc_info.value)
        assert "0.3 以上" in str(exc_info.value)

    def test_toolhead_offset_rejects_point_count_below_five(self, default: JobCatalog):
        definition = default.get("toolhead_offset")

        with pytest.raises(ValueError) as exc_info:
            default.validate_params(definition, {"point_count": 4})

        assert "point_count" in str(exc_info.value)

    def test_toolhead_offset_persists_all_params(self, default: JobCatalog):
        """全 params が次回フォーム既定値として保存される（保存漏れを検知する）."""
        definition = default.get("toolhead_offset")

        assert set(definition.persisted_params) == {
            spec.name for spec in definition.params
        }

    def test_dispense_calibration_runtime_editable_split(self, default: JobCatalog):
        definition = default.get("dispense_calibration")
        params = {spec.name: spec for spec in definition.params}

        # 銅板は開始時に 1 回生成するため board 寸法と許容誤差はキャリブ後固定。
        for fixed in ("board_width", "board_height", "tolerance"):
            assert params[fixed].runtime_editable is False

        # 線設定・計量退避・②③ のスケジュールは実行中に変更可。
        assert definition.runtime_params == (
            "line_length",
            "line_count",
            "line_amount",
            "row_pitch",
            "removal_z_offset",
            "rate_min",
            "rate_max",
            "rate_divisions",
            "speed_min",
            "speed_max",
            "speed_divisions",
        )

    def test_loading_position_axes_are_optional_and_not_persisted(
        self, default: JobCatalog
    ):
        definition = default.get("loading")
        params = {spec.name: spec for spec in definition.params}

        for name in ("position_x", "position_y", "position_z"):
            assert params[name].value_type == "float"
            assert params[name].unit == "mm"
            assert params[name].optional is True
            assert name not in definition.persisted_params

        validated = default.validate_params(
            definition, {"position_x": 10, "position_z": 2.5}
        )
        assert validated["position_x"] == 10.0
        assert "position_y" not in validated
        assert validated["position_z"] == 2.5

    def test_paste_solder_interactive_loading_is_bool_defaulting_false(
        self, default: JobCatalog
    ):
        params = {spec.name: spec for spec in default.get("paste_solder").params}

        assert set(params) == {"tolerance", "amount", "interactive_loading"}
        assert params["interactive_loading"].value_type == "bool"
        assert params["interactive_loading"].default is False
