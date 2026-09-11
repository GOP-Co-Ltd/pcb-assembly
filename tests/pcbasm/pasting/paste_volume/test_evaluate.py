"""校正を session に当てて誤差を出す経路の公開契約.

校正を作った session そのものに当てるのはフィットの確認であって汎化ではない。ここで
固めるのは集計の意味であって、係数や誤差の絶対値ではない。

主基準が総体積誤差であること、条件不一致で止まらないこと、不採用の切り分けが残ること。
"""

import json
import shutil
from pathlib import Path

import attrs
import pytest

from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.calibration import (
    PasteVolumeCalibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.evaluate import (
    condition_mismatch,
    evaluate_collected_session,
    evaluate_session,
    evaluation_document,
    evaluation_lines,
)
from pcbasm.pasting.paste_volume.fit import fit_session
from tests.helpers import build_paste_volume_session

STEM = "plate-47.5x20-20260909T145923.452+0900"


@pytest.fixture
def session(tmp_path: Path) -> DatasetSession:
    root = build_paste_volume_session(tmp_path / STEM)
    loaded, error = DatasetSession.load(root)
    assert error is None, error
    assert loaded is not None
    return loaded


@pytest.fixture
def calibration(session: DatasetSession) -> PasteVolumeCalibration:
    fit, error = fit_session(session)
    assert error is None, error
    assert fit is not None
    return fit.calibration


class TestEvaluateSession:
    """セルごとの推定と、session 単位の集計."""

    def test_evaluates_every_cell(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, error = evaluate_session(session, calibration)

        assert error is None, error
        assert evaluation is not None
        assert len(evaluation.cells) == len(session.cells())

    def test_names_the_session_and_the_calibration(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert evaluation.session == STEM
        assert evaluation.calibration_label == calibration.label

    def test_accepts_the_dispensed_cells_and_rejects_the_blank(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        """Blank は真値 0 なので補正の材料にならない（例外にはしない）."""
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert evaluation.accepted_count == len(session.metadata.samples)
        assert dict(evaluation.rejected_reasons) == {"no_deposit_detected": 1}

    def test_reports_no_blank_false_positive_and_no_detection_failure(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert evaluation.blank_false_positive_count == 0
        assert evaluation.detection_failure_count == 0

    def test_tells_a_blank_apart_from_a_detection_failure(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        """どちらも no_deposit_detected なので、件数で切り分けられる必要がある.

        blank は真値 0 で正常、検出失敗は回帰条件（計画書の達成条件）。
        """
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert evaluation.blank_count == 1
        assert evaluation.dispensed_count == len(session.metadata.samples)
        assert dict(evaluation.rejected_reasons)["no_deposit_detected"] == 1
        assert evaluation.detection_failure_count == 0

    def test_counts_a_dispensed_cell_that_could_not_be_detected(
        self, tmp_path: Path, calibration: PasteVolumeCalibration
    ):
        """塗布したのに写っていないセルは検出失敗として数える."""
        root = build_paste_volume_session(tmp_path / "plate-b")
        # 塗布セル 1 の post を pre と同じにして「写っていない」状態を作る
        for number in (0, 1):
            shutil.copyfile(
                root / "pre" / f"000001.{number:02d}.png",
                root / "post" / f"000001.{number:02d}.png",
            )
        loaded, _ = DatasetSession.load(root)
        assert loaded is not None

        evaluation, error = evaluate_session(loaded, calibration)

        assert error is None, error
        assert evaluation is not None
        assert evaluation.detection_failure_count == 1
        assert evaluation.blank_false_positive_count == 0

    def test_total_volume_error_is_the_main_criterion(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        """フィット元の session に当てているので、総体積誤差はほぼ 0 になる."""
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert evaluation.measured_total_ul > 0.0
        assert abs(evaluation.total_relative_error) < 0.05

    def test_keeps_point_errors_as_a_reference(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        assert 0.0 <= evaluation.point_relative_mae <= evaluation.point_relative_max

    def test_reports_a_session_that_cannot_be_measured(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        (session.root / "post" / "000001.00.png").unlink()

        evaluation, error = evaluate_session(session, calibration)

        assert evaluation is None
        assert error is not None


class TestConditionMismatch:
    """条件が違っても評価は止めず、食い違いを並べて返す."""

    def test_reports_nothing_when_the_conditions_agree(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        assert condition_mismatch(session, calibration) == ()

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("paste_id", "other-paste", "ペースト"),
            ("nozzle_diameter_mm", 0.5, "ノズル径"),
            ("paste_height_mm", 0.4, "塗布高さ"),
            ("pixel_per_mm", 10.0, "撮影スケール"),
        ],
    )
    def test_names_each_condition_that_differs(
        self,
        session: DatasetSession,
        calibration: PasteVolumeCalibration,
        field: str,
        value: object,
        expected: str,
    ):
        changed = attrs.evolve(
            calibration,
            conditions=attrs.evolve(calibration.conditions, **{field: value}),
        )

        mismatches = condition_mismatch(session, changed)

        assert len(mismatches) == 1
        assert mismatches[0].startswith(expected)

    def test_evaluation_continues_despite_a_mismatch(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        """条件不一致で断ると、条件不一致を確かめること自体ができなくなる."""
        changed = attrs.evolve(
            calibration,
            conditions=attrs.evolve(calibration.conditions, paste_id="other-paste"),
        )

        evaluation, error = evaluate_session(session, changed)

        assert error is None, error
        assert evaluation is not None
        assert evaluation.condition_mismatch != ()
        assert evaluation.accepted_count > 0


class TestEvaluateCollectedSession:
    """Path だけを持つ呼び出し側（収集ジョブ）の入口."""

    def test_loads_the_session_and_the_calibration_from_paths(
        self,
        tmp_path: Path,
        session: DatasetSession,
        calibration: PasteVolumeCalibration,
    ):
        path = tmp_path / "saved.paste-volume.json"
        write_calibration(path, calibration)

        evaluation, error = evaluate_collected_session(session.root, path)

        assert error is None, error
        assert evaluation is not None
        assert evaluation.session == STEM

    def test_reports_a_missing_calibration(
        self, tmp_path: Path, session: DatasetSession
    ):
        evaluation, error = evaluate_collected_session(
            session.root, tmp_path / "absent.paste-volume.json"
        )

        assert evaluation is None
        assert error is not None

    def test_reports_a_missing_session(self, tmp_path: Path, calibration):
        path = tmp_path / "saved.paste-volume.json"
        write_calibration(path, calibration)

        evaluation, error = evaluate_collected_session(tmp_path / "absent", path)

        assert evaluation is None
        assert error is not None


class TestEvaluationOutputs:
    """表示文字列と artifact document はサーバー側で組み立てる."""

    def test_lines_carry_the_total_volume_error(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        lines = evaluation_lines(evaluation)

        assert any("総体積誤差" in line for line in lines)

    def test_lines_carry_each_condition_mismatch(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        changed = attrs.evolve(
            calibration,
            conditions=attrs.evolve(calibration.conditions, paste_id="other-paste"),
        )
        evaluation, _ = evaluate_session(session, changed)
        assert evaluation is not None

        lines = evaluation_lines(evaluation)

        assert any("条件不一致" in line for line in lines)

    def test_document_is_json_serialisable(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        document = evaluation_document(evaluation)
        restored = json.loads(json.dumps(document, ensure_ascii=False))

        assert restored["session"] == STEM
        assert len(restored["cells"]) == len(evaluation.cells)

    def test_document_keeps_the_rejection_reason_per_cell(
        self, session: DatasetSession, calibration: PasteVolumeCalibration
    ):
        evaluation, _ = evaluate_session(session, calibration)
        assert evaluation is not None

        document = evaluation_document(evaluation)
        blank = [cell for cell in document["cells"] if cell["blank"]]

        assert len(blank) == 1
        assert blank[0]["rejection_reason"] == "no_deposit_detected"
