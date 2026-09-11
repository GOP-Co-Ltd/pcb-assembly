"""校正ファイル（schema v1）の DTO・strict parse・永続化の公開契約.

on-disk の出典は ``data/testing/schemas/paste_volume_calibration_v1.json``。
検出ハイパラと係数は不可分（違うハイパラで測った直径に係数を当てても意味がない）
なので、同じ document へ一緒に持つことを固める。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_KIND,
    CALIBRATION_SCHEMA_VERSION,
    CALIBRATION_SUFFIX,
    CalibrationConditions,
    PasteVolumeCalibration,
    auto_calibration_path,
    calibration_filename,
    calibration_path,
    default_calibration_label,
    list_calibrations,
    load_calibration,
    parse_calibration,
    write_calibration,
)
from tests.helpers import TESTING_DATA_DIR

PIN = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"


def _document() -> dict:
    return json.loads(PIN.read_text(encoding="utf-8"))


def _parsed() -> PasteVolumeCalibration:
    calibration, error = parse_calibration(_document())

    assert error is None, error
    assert calibration is not None
    return calibration


class TestCalibrationOnDiskShape:
    """ピン fixture の形と、実装が期待するキー集合の一致."""

    def test_fixture_declares_kind_and_current_version(self):
        payload = _document()

        assert payload["kind"] == CALIBRATION_KIND
        assert payload["schema_version"] == CALIBRATION_SCHEMA_VERSION
        assert CALIBRATION_SCHEMA_VERSION == 1

    def test_top_level_sections(self):
        assert set(_document()) == {
            "kind",
            "schema_version",
            "created_at",
            "label",
            "conditions",
            "detection",
            "model",
            "source",
            "diagnostics",
        }

    def test_conditions_carry_the_paste_nozzle_and_height(self):
        conditions = _document()["conditions"]

        assert set(conditions) == {
            "paste_id",
            "paste_lot",
            "density_mg_per_ul",
            "nozzle_diameter_mm",
            "paste_height_mm",
            "machine_id",
            "pixel_per_mm",
            "crop_size_px",
            "crop_size_mm",
        }

    def test_detection_hyperparameters_travel_with_the_model(self):
        """違うハイパラで測った直径に係数を当てても意味がないので同じ document に置く."""
        payload = _document()

        assert payload["detection"]["kind"] == "diameter_otsu_v1"
        assert payload["model"]["kind"] == "cubic_through_origin"

    def test_source_records_how_the_labels_were_made(self):
        """``rotation_allocated`` は点ごとの真値を持たない。達成条件の根拠を残す."""
        assert _document()["source"]["label_kind"] == "rotation_allocated"


class TestParseCalibration:
    """Strict parse（未知 key と暗黙の型変換を拒否）."""

    def test_restores_the_on_disk_document(self):
        calibration = _parsed()

        assert calibration.label == "S3X70-E150DN / n0.30 / h0.20"
        assert calibration.conditions.paste_id == "S3X70-E150DN"
        assert calibration.conditions.paste_lot is None
        assert calibration.detection.min_contrast == 20.0
        assert calibration.model.cubic_ul_per_mm3 == 0.24327

    def test_roundtrips_through_the_document(self):
        assert _parsed().to_dict() == _document()

    @pytest.mark.parametrize("version", [0, 2, "1", None])
    def test_rejects_other_schema_versions_without_migrating(self, version: object):
        payload = _document()
        if version is None:
            del payload["schema_version"]
        else:
            payload["schema_version"] = version

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None
        assert "schema_version" in error

    def test_rejects_a_foreign_kind(self):
        payload = _document()
        payload["kind"] = "pcbasm-paste-volume-dataset"

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    @pytest.mark.parametrize(
        "section", [None, "conditions", "detection", "model", "source", "diagnostics"]
    )
    def test_rejects_unknown_keys(self, section: str | None):
        payload = _document()
        target = payload if section is None else payload[section]
        target["surprise"] = 1

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    def test_rejects_an_int_where_a_float_belongs(self):
        payload = _document()
        payload["model"]["cubic_ul_per_mm3"] = 1

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    def test_rejects_a_non_finite_coefficient(self):
        payload = _document()
        payload["model"]["cubic_ul_per_mm3"] = float("nan")

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    @pytest.mark.parametrize(
        "key",
        [
            "min_contrast",
            "contrast_percentile",
            "threshold_floor_ratio",
            "open_kernel_px",
            "min_area_px",
        ],
    )
    def test_rejects_a_detection_key_that_is_missing(self, key: str):
        """検出ハイパラの欠落を既定値で埋めない.

        埋めてしまうと :class:`DotDetectionSpec` の既定値を将来変えたときに、キーを
        欠いた既存ファイルの意味が静かに変わる。検出ハイパラは校正と不可分。
        """
        payload = _document()
        del payload["detection"][key]

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None
        assert key in error

    def test_rejects_a_detection_section_that_is_not_an_object(self):
        payload = _document()
        payload["detection"] = {"kind": "diameter_otsu_v1"}

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    def test_rejects_an_invalid_detection_spec(self):
        payload = _document()
        payload["detection"]["open_kernel_px"] = 4

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None

    def test_rejects_an_inverted_model_range(self):
        payload = _document()
        payload["model"]["diameter_min_mm"] = 2.0

        calibration, error = parse_calibration(payload)

        assert calibration is None
        assert error is not None


class TestCalibrationFiles:
    """ファイルの読み書きと列挙."""

    def test_writes_and_loads_back(self, tmp_path: Path):
        path = tmp_path / f"sample{CALIBRATION_SUFFIX}"

        write_calibration(path, _parsed())
        loaded, error = load_calibration(path)

        assert error is None, error
        assert loaded == _parsed()

    def test_written_file_is_valid_json_with_a_trailing_newline(self, tmp_path: Path):
        path = tmp_path / f"sample{CALIBRATION_SUFFIX}"

        write_calibration(path, _parsed())

        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert json.loads(text) == _document()

    def test_load_reports_a_missing_file(self, tmp_path: Path):
        calibration, error = load_calibration(tmp_path / "absent.json")

        assert calibration is None
        assert error is not None

    def test_load_reports_broken_json(self, tmp_path: Path):
        path = tmp_path / f"broken{CALIBRATION_SUFFIX}"
        path.write_text("{", encoding="utf-8")

        calibration, error = load_calibration(path)

        assert calibration is None
        assert error is not None

    def test_lists_only_calibration_files_in_name_order(self, tmp_path: Path):
        second = tmp_path / f"b{CALIBRATION_SUFFIX}"
        first = tmp_path / f"a{CALIBRATION_SUFFIX}"
        for path in (second, first):
            write_calibration(path, _parsed())
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        (tmp_path / "other.json").write_text("{}", encoding="utf-8")

        assert list_calibrations(tmp_path) == (first, second)

    def test_lists_nothing_when_the_directory_is_missing(self, tmp_path: Path):
        assert list_calibrations(tmp_path / "absent") == ()


class TestCalibrationFilename:
    """保存名の組み立て（人が読める名前 + 衝突しない時刻）."""

    def test_uses_the_suffix_and_keeps_the_label_readable(self):
        name = calibration_filename("S3X70-E150DN / n0.30")

        assert name.endswith(CALIBRATION_SUFFIX)
        assert "s3x70-e150dn" in name

    def test_replaces_characters_that_are_awkward_in_a_path(self):
        name = calibration_filename("a/b c:d")

        assert "/" not in name.removesuffix(CALIBRATION_SUFFIX)
        assert " " not in name

    def test_falls_back_when_the_label_has_nothing_usable(self):
        name = calibration_filename("///")

        assert name.endswith(CALIBRATION_SUFFIX)
        assert len(name) > len(CALIBRATION_SUFFIX)


class TestCalibrationPath:
    """運転者が入力した保存名を、保存先の直下に閉じる."""

    @pytest.mark.parametrize(
        "name",
        [
            "../../../tmp/pwn.paste-volume.json",
            "/etc/pwn.paste-volume.json",
            "sub/dir/x.paste-volume.json",
            "..",
            "/",
        ],
    )
    def test_never_escapes_the_root(self, tmp_path: Path, name: str):
        """WebUI のテキスト欄から任意 path へ書けてはいけない."""
        path = calibration_path(tmp_path, name)

        assert path.parent == tmp_path
        assert path.resolve().is_relative_to(tmp_path.resolve())

    def test_keeps_a_name_that_already_carries_the_suffix(self, tmp_path: Path):
        """付け直しではなく上書きの意図なので、時刻を足さない."""
        path = calibration_path(tmp_path, f"s3x70-n030{CALIBRATION_SUFFIX}")

        assert path.name == f"s3x70-n030{CALIBRATION_SUFFIX}"

    def test_adds_a_timestamp_to_a_bare_name(self, tmp_path: Path):
        path = calibration_path(
            tmp_path, "S3X70 / n0.30", datetime(2026, 9, 10, 12, 0, 0)
        )

        assert path.name.startswith("s3x70")
        assert "20260910T120000" in path.name
        assert path.name.endswith(CALIBRATION_SUFFIX)

    def test_falls_back_to_a_default_stem_when_nothing_survives(self, tmp_path: Path):
        path = calibration_path(tmp_path, f"///{CALIBRATION_SUFFIX}")

        assert path.name == f"calibration{CALIBRATION_SUFFIX}"


class TestDefaultCalibrationLabel:
    """条件と生成時刻から組む既定の表示名（保存済み校正の移行でも同じ関数を使う）."""

    @staticmethod
    def _conditions() -> CalibrationConditions:
        return CalibrationConditions(
            paste_id="S3X70-E150DN",
            paste_lot=None,
            density_mg_per_ul=3.78,
            nozzle_diameter_mm=0.3,
            paste_height_mm=0.2,
            machine_id="m1",
            pixel_per_mm=28.678,
            crop_size_px=53,
            crop_size_mm=1.8,
        )

    def test_carries_the_conditions_and_the_local_time(self):
        moment = datetime(2026, 9, 10, 7, 18, 47, tzinfo=UTC)

        label = default_calibration_label(self._conditions(), moment)

        local = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert label == f"S3X70-E150DN / n0.30 / h0.20 / {local}"

    def test_two_runs_of_the_same_conditions_differ(self):
        conditions = self._conditions()

        first = default_calibration_label(
            conditions, datetime(2026, 9, 10, 7, 18, 47, tzinfo=UTC)
        )
        second = default_calibration_label(
            conditions, datetime(2026, 9, 10, 7, 45, 9, tzinfo=UTC)
        )

        assert first != second


class TestAutoCalibrationPath:
    """自動命名の保存先（ラベルが既に生成時刻を含む）."""

    LABEL = "S3X70-E150DN / n0.30 / h0.20 / 2026-09-10 16:45:09"

    def test_folds_the_label_into_the_file_name(self, tmp_path: Path):
        path = auto_calibration_path(tmp_path, self.LABEL)

        assert path.parent == tmp_path
        assert path.name == (
            f"s3x70-e150dn-n0.30-h0.20-2026-09-10-16-45-09{CALIBRATION_SUFFIX}"
        )

    def test_does_not_add_a_second_timestamp(self, tmp_path: Path):
        """ラベル側の時刻と保存名側の時刻で 2 度入ると読みにくい."""
        auto = auto_calibration_path(tmp_path, self.LABEL).name
        dated = calibration_path(
            tmp_path, self.LABEL, datetime(2026, 9, 10, 16, 45, 9)
        ).name

        assert auto.count("2026") == 1
        assert dated.count("2026") == 2

    def test_two_labels_from_the_same_conditions_do_not_collide(self, tmp_path: Path):
        later = "S3X70-E150DN / n0.30 / h0.20 / 2026-09-10 17:01:00"

        assert auto_calibration_path(tmp_path, self.LABEL) != auto_calibration_path(
            tmp_path, later
        )

    def test_never_escapes_the_root(self, tmp_path: Path):
        path = auto_calibration_path(tmp_path, "../../../tmp/pwn")

        assert path.parent == tmp_path
        assert path.resolve().is_relative_to(tmp_path.resolve())

    def test_the_written_file_is_found_by_the_listing(self, tmp_path: Path):
        """保存できたのに一覧へ出てこない、が起きない."""
        path = calibration_path(tmp_path, "sub/dir/x.paste-volume.json")

        write_calibration(path, _parsed())

        assert list_calibrations(tmp_path) == (path,)
