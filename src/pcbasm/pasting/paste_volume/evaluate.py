"""校正と収集 session を突き合わせ、推定と実測の誤差を出す.

校正を作った session とは別の session に当てて、はじめて汎化を測ったことになる。
校正ジョブが返す診断はフィットの当てはまりであって、汎化性能ではない。

主基準は **session 総体積の相対誤差**。教師ラベル ``rotation_allocated`` は総質量を
指令回転数比で配分した値で、点ごとの真値を持たないため、点ごとの誤差は参考値に留める。

校正の条件（ペースト・ノズル径・塗布高さ・撮影スケール）が session と違っても評価は
止めない。止めると条件不一致を確かめること自体ができなくなるので、``condition_mismatch``
に載せて続行する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import attrs
import numpy as np

from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.calibration import (
    PasteVolumeCalibration,
    load_calibration,
)
from pcbasm.pasting.paste_volume.estimator import (
    DiameterVolumeEstimator,
    PasteVolumePrediction,
)
from pcbasm.pasting.paste_volume.fit import CellMeasurement, measure_session


@attrs.frozen
class CellEvaluation:
    """1 セルの推定と実測.

    Attributes:
        index: セル番号
        blank: blank セルか
        diameter_mm: 集約後の検出直径 [mm]
        measured_volume_ul: 教師体積 [µL]
        prediction: 推定結果（不採用の理由もここに入る）
    """

    index: int
    blank: bool
    diameter_mm: float
    measured_volume_ul: float
    prediction: PasteVolumePrediction


@attrs.frozen
class PasteVolumeEvaluation:
    """1 session ぶんの評価レポート.

    Attributes:
        session: 評価した session directory 名
        calibration_label: 当てた校正の表示名
        condition_mismatch: 一致しなかった条件の説明（無ければ空）
        cells: セルごとの推定と実測
        accepted_count: 採用できたセル数
        rejected_reasons: 不採用の理由ごとの件数
        blank_false_positive_count: blank で直径が 0 にならなかった数
        detection_failure_count: 塗布したのに全 view で検出できなかったセル数
        measured_total_ul: 教師体積の合計 [µL]
        predicted_total_ul: 推定体積の合計 [µL]（不採用は 0 として足す）
        total_relative_error: 総体積の相対誤差（主基準）
        point_relative_mae: 採用セルの点ごと相対誤差の平均絶対値（参考値）
        point_relative_max: 同じく最大（参考値）
    """

    session: str
    calibration_label: str
    condition_mismatch: tuple[str, ...]
    cells: tuple[CellEvaluation, ...]
    accepted_count: int
    rejected_reasons: tuple[tuple[str, int], ...]
    blank_false_positive_count: int
    detection_failure_count: int
    measured_total_ul: float
    predicted_total_ul: float
    total_relative_error: float
    point_relative_mae: float
    point_relative_max: float

    @property
    def blank_count(self) -> int:
        """Blank セル数（真値 0 なので採用の分母に入らない）."""
        return sum(1 for cell in self.cells if cell.blank)

    @property
    def dispensed_count(self) -> int:
        """塗布セル数（採用率の分母）."""
        return len(self.cells) - self.blank_count


def evaluate_session(
    session: DatasetSession, calibration: PasteVolumeCalibration
) -> tuple[PasteVolumeEvaluation | None, str | None]:
    """校正を session に当てて誤差レポートを作る.

    Args:
        session: 評価対象の完成 session
        calibration: 当てる校正

    Returns:
        ``(レポート, None)`` または ``(None, 理由)``
    """
    cells, error = measure_session(session, spec=calibration.detection)
    if cells is None:
        return None, error

    estimator = DiameterVolumeEstimator(calibration)
    evaluations = tuple(
        CellEvaluation(
            index=cell.index,
            blank=cell.blank,
            diameter_mm=cell.diameter.diameter_mm,
            measured_volume_ul=cell.measured_volume_ul,
            prediction=estimator.predict_diameter(cell.diameter),
        )
        for cell in cells
    )
    return _report(session, calibration, cells, evaluations), None


def evaluate_collected_session(
    session_root: Path, calibration_path: Path
) -> tuple[PasteVolumeEvaluation | None, str | None]:
    """Session directory と校正ファイルの path から評価する.

    収集ジョブのように「path しか持っていない」呼び出し側が、session の読み込みと
    校正の読み込みを個別に組み立てずに済むようにする。
    """
    session, error = DatasetSession.load(session_root)
    if session is None:
        return None, error
    calibration, error = load_calibration(calibration_path)
    if calibration is None:
        return None, error
    return evaluate_session(session, calibration)


def evaluation_lines(evaluation: PasteVolumeEvaluation) -> tuple[str, ...]:
    """レポートをログ 1 行ずつへ整える（表示文字列はサーバー側で組む）."""
    lines = [
        f"校正 {evaluation.calibration_label} で {evaluation.session} を検証しました",
        f"採用 {evaluation.accepted_count} / {evaluation.dispensed_count} 塗布セル "
        f"（blank {evaluation.blank_count} 点は対象外）",
        f"blank誤検出 {evaluation.blank_false_positive_count} 件 / "
        f"検出失敗 {evaluation.detection_failure_count} 件",
        f"実測 {evaluation.measured_total_ul:.6f} uL / "
        f"推定 {evaluation.predicted_total_ul:.6f} uL / "
        f"総体積誤差 {evaluation.total_relative_error * 100:+.2f}%",
        f"点ごと相対誤差 平均 {evaluation.point_relative_mae * 100:.1f}% / "
        f"最大 {evaluation.point_relative_max * 100:.1f}%",
    ]
    for reason, count in evaluation.rejected_reasons:
        lines.append(f"不採用 {reason}: {count} 件")
    for mismatch in evaluation.condition_mismatch:
        lines.append(f"条件不一致（評価は続行）: {mismatch}")
    return tuple(lines)


def evaluation_document(evaluation: PasteVolumeEvaluation) -> dict[str, Any]:
    """レポートを JSON 互換 dict へ変換する（artifact 用）."""
    return {
        "session": evaluation.session,
        "calibration_label": evaluation.calibration_label,
        "condition_mismatch": list(evaluation.condition_mismatch),
        "accepted_count": evaluation.accepted_count,
        "cell_count": len(evaluation.cells),
        "rejected_reasons": dict(evaluation.rejected_reasons),
        "blank_false_positive_count": evaluation.blank_false_positive_count,
        "detection_failure_count": evaluation.detection_failure_count,
        "dispensed_count": evaluation.dispensed_count,
        "blank_count": evaluation.blank_count,
        "measured_total_ul": evaluation.measured_total_ul,
        "predicted_total_ul": evaluation.predicted_total_ul,
        "total_relative_error": evaluation.total_relative_error,
        "point_relative_mae": evaluation.point_relative_mae,
        "point_relative_max": evaluation.point_relative_max,
        "cells": [
            {
                "index": cell.index,
                "blank": cell.blank,
                "diameter_mm": cell.diameter_mm,
                "measured_volume_ul": cell.measured_volume_ul,
                "predicted_volume_ul": cell.prediction.mean_volume_ul,
                "accepted": cell.prediction.accepted,
                "rejection_reason": cell.prediction.rejection_reason,
            }
            for cell in evaluation.cells
        ],
    }


def _report(
    session: DatasetSession,
    calibration: PasteVolumeCalibration,
    cells: tuple[CellMeasurement, ...],
    evaluations: tuple[CellEvaluation, ...],
) -> PasteVolumeEvaluation:
    """セルごとの評価を session 単位のレポートへ集計する."""
    measured_total = sum(cell.measured_volume_ul for cell in cells)
    predicted_total = sum(
        evaluation.prediction.mean_volume_ul for evaluation in evaluations
    )
    relative_errors = [
        (evaluation.prediction.mean_volume_ul - evaluation.measured_volume_ul)
        / evaluation.measured_volume_ul
        for evaluation in evaluations
        if evaluation.prediction.accepted and evaluation.measured_volume_ul > 0.0
    ]
    errors = np.array(relative_errors, dtype=np.float64)
    return PasteVolumeEvaluation(
        session=session.label,
        calibration_label=calibration.label,
        condition_mismatch=condition_mismatch(session, calibration),
        cells=evaluations,
        accepted_count=sum(1 for item in evaluations if item.prediction.accepted),
        rejected_reasons=_rejection_counts(evaluations),
        blank_false_positive_count=sum(
            1 for item in evaluations if item.blank and item.diameter_mm > 0.0
        ),
        detection_failure_count=sum(
            1 for item in evaluations if not item.blank and item.diameter_mm <= 0.0
        ),
        measured_total_ul=measured_total,
        predicted_total_ul=predicted_total,
        total_relative_error=(
            (predicted_total - measured_total) / measured_total
            if measured_total > 0.0
            else 0.0
        ),
        point_relative_mae=float(np.abs(errors).mean()) if errors.size else 0.0,
        point_relative_max=float(np.abs(errors).max()) if errors.size else 0.0,
    )


def condition_mismatch(
    session: DatasetSession, calibration: PasteVolumeCalibration
) -> tuple[str, ...]:
    """校正の条件と session の条件が食い違う点を並べる（評価は止めない）."""
    metadata = session.metadata
    return calibration.conditions.mismatches(
        paste_id=metadata.paste.paste_id,
        nozzle_diameter_mm=metadata.nozzle.diameter_mm,
        paste_height_mm=metadata.config.paste_height_mm,
        pixel_per_mm=metadata.camera.pixel_per_mm,
    )


def _rejection_counts(
    evaluations: tuple[CellEvaluation, ...],
) -> tuple[tuple[str, int], ...]:
    """不採用の理由ごとの件数を、件数の多い順に並べる."""
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        reason = evaluation.prediction.rejection_reason
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


__all__ = [
    "CellEvaluation",
    "PasteVolumeEvaluation",
    "condition_mismatch",
    "evaluate_collected_session",
    "evaluate_session",
    "evaluation_document",
    "evaluation_lines",
]
