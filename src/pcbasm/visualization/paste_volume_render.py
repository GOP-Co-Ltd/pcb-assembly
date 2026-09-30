"""直径ベース塗布量校正の診断図（散布＋フィット曲線、検出モンタージュ）."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import cv2
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes

from pcbasm.pasting.paste_volume.evaluate import PasteVolumeEvaluation
from pcbasm.pasting.paste_volume.fit import CellMeasurement
from pcbasm.pasting.paste_volume.model import CubicVolumeModel
from pcbasm.vision.image import ImageArray

_CURVE_SAMPLES = 200
_MONTAGE_ROWS = ("pre", "post", "mask")


def render_calibration_scatter(
    cells: Sequence[CellMeasurement],
    model: CubicVolumeModel,
    title: str,
    output_path: Path,
) -> None:
    """直径 vs 教師体積の散布へフィット曲線を重ねて PNG 保存する.

    blank セル（塗っていないセル）は別マーカーで描く。

    blank は教師体積 0・直径 0 に来るはずで、外れていれば誤検出と図で分かる。

    フィット曲線は校正の被覆域だけに描き、域外へ外挿しない。
    """
    fig, ax = plt.subplots(figsize=(7.0, 5.0))
    _draw_cells(ax, cells)
    _draw_curve(ax, model)

    ax.set_xlabel("Detected diameter [mm]")
    ax.set_ylabel("Allocated volume [uL]")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    ax.set_xlim(left=0.0)
    ax.set_ylim(bottom=0.0)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def render_evaluation_scatter(
    evaluation: PasteVolumeEvaluation, title: str, output_path: Path
) -> None:
    """実測体積 vs 推定体積の散布へ y=x を重ねて PNG 保存する.

    不採用セルは推定 0 として原点寄りに並ぶので、採用できなかったことが図で分かる。
    """
    fig, ax = plt.subplots(figsize=(6.0, 6.0))
    for accepted, marker, color, label in (
        (True, "o", "tab:blue", "Accepted"),
        (False, "x", "tab:red", "Rejected"),
    ):
        selected = [
            cell for cell in evaluation.cells if cell.prediction.accepted is accepted
        ]
        if not selected:
            continue
        ax.scatter(
            [cell.measured_volume_ul for cell in selected],
            [cell.prediction.mean_volume_ul for cell in selected],
            marker=marker,
            s=18,
            alpha=0.7,
            color=color,
            label=label,
        )

    limit = max(
        evaluation.measured_total_ul / max(len(evaluation.cells), 1) * 3.0,
        *(cell.measured_volume_ul for cell in evaluation.cells),
        *(cell.prediction.mean_volume_ul for cell in evaluation.cells),
    )
    ax.plot([0.0, limit], [0.0, limit], color="tab:gray", linewidth=1.0, label="y = x")
    ax.set_xlabel("Allocated volume [uL]")
    ax.set_ylabel("Estimated volume [uL]")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    ax.set_xlim(0.0, limit)
    ax.set_ylim(0.0, limit)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def render_detection_montage(
    panels: Sequence[tuple[str, ImageArray, ImageArray, ImageArray]],
    title: str,
    output_path: Path,
) -> None:
    """代表セルの pre / post / 2 値マスクを縦 3 段で並べて PNG 保存する.

    Args:
        panels: `(見出し, pre, post, mask)` の列。左から順に並ぶ
        title: 図のタイトル
        output_path: 保存先
    """
    if not panels:
        raise ValueError("モンタージュに並べるセルがありません")
    fig, axes = plt.subplots(
        len(_MONTAGE_ROWS),
        len(panels),
        figsize=(1.9 * len(panels), 2.1 * len(_MONTAGE_ROWS)),
        squeeze=False,
    )
    for column, (label, *images) in enumerate(panels):
        for row, (name, image) in enumerate(zip(_MONTAGE_ROWS, images, strict=True)):
            ax = axes[row][column]
            ax.imshow(_as_display(image), interpolation="nearest")
            ax.set_xticks([])
            ax.set_yticks([])
            if row == 0:
                ax.set_title(label, fontsize=9)
            if column == 0:
                ax.set_ylabel(name, fontsize=9)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _draw_cells(ax: Axes, cells: Sequence[CellMeasurement]) -> None:
    """塗布セルと blank セルを別マーカーで散布する."""
    for blank, marker, color, label in (
        (False, "o", "tab:blue", "Dispensed"),
        (True, "x", "tab:red", "Blank"),
    ):
        selected = [cell for cell in cells if cell.blank is blank]
        if not selected:
            continue
        ax.scatter(
            [cell.diameter.diameter_mm for cell in selected],
            [cell.measured_volume_ul for cell in selected],
            marker=marker,
            s=18,
            alpha=0.7,
            color=color,
            label=label,
        )


def _draw_curve(ax: Axes, model: CubicVolumeModel) -> None:
    """被覆域でフィット曲線を描く（域外は外挿しないので描かない）."""
    diameters = np.linspace(
        model.diameter_min_mm, model.diameter_max_mm, _CURVE_SAMPLES
    )
    ax.plot(
        diameters,
        [model.volume_ul(float(diameter)) for diameter in diameters],
        color="tab:green",
        linewidth=1.8,
        label="Cubic fit (covered range)",
    )


def _as_display(image: ImageArray) -> ImageArray:
    """OpenCV の BGR / 単チャネルを matplotlib が描ける形へ直す."""
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
