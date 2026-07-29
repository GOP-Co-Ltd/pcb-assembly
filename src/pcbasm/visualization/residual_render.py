"""カメラ校正の残差レポートの PNG レンダリング."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.quiver import Quiver

from pcbasm.vision import (
    CalibrationQuality,
    CheckerboardView,
    ImageArray,
    ResidualReport,
    residual_field,
)

# 最大残差ベクトルの描画長 [px]（残差は数 px なので誇張しないと見えない）
_ARROW_LENGTH_PX = 60.0


def render_scan_residuals(
    quality: CalibrationQuality,
    before_views: Sequence[CheckerboardView],
    after_views: Sequence[CheckerboardView],
    output_path: Path,
) -> None:
    """歪み補正の前後の残差を quiver 2 面 + 半径プロファイルで PNG 保存する.

    上段 2 面は補正前・補正後の残差ベクトル場で、**カラースケールと矢印の縮尺を
    共通化**してあるので目で直接比較できる。下段は半径帯ごとの残差 RMS の折れ線で、
    レンズ歪みなら周辺ほど大きくなる（そうならない場合はボードの傾きを疑う）。

    Args:
        quality: 校正の品質指標（半径帯と pixel/mm の出所）
        before_views: 生コーナーの視点
        after_views: 歪み補正後コーナーの視点
        output_path: 出力する PNG のパス
    """
    before_points, before_vectors = residual_field(before_views)
    after_points, after_vectors = residual_field(after_views)
    before_um = _magnitudes_um(before_vectors, quality.before)
    after_um = _magnitudes_um(after_vectors, quality.after)

    limit_um = max(float(before_um.max()), float(after_um.max()))
    # 矢印縮尺は px 空間で決める（両面で共通にして見た目の長さを比較可能にする）
    arrow_scale = (
        max(
            float(np.linalg.norm(before_vectors, axis=1).max()),
            float(np.linalg.norm(after_vectors, axis=1).max()),
        )
        / _ARROW_LENGTH_PX
    )

    # colorbar が 2 面にまたがるので tight_layout ではなく constrained layout を使う
    fig = plt.figure(figsize=(14, 10), layout="constrained")
    grid = fig.add_gridspec(2, 2, height_ratios=(2.0, 1.0))
    ax_before = fig.add_subplot(grid[0, 0])
    ax_after = fig.add_subplot(grid[0, 1])
    ax_profile = fig.add_subplot(grid[1, :])

    image_size = before_views[0].image_size
    quiver = _draw_quiver(
        ax_before,
        f"Before undistort (RMS {quality.before.rms_um:.1f} um)",
        image_size,
        before_points,
        before_vectors,
        before_um,
        limit_um=limit_um,
        arrow_scale=arrow_scale,
    )
    _draw_quiver(
        ax_after,
        f"After undistort (RMS {quality.after.rms_um:.1f} um)",
        image_size,
        after_points,
        after_vectors,
        after_um,
        limit_um=limit_um,
        arrow_scale=arrow_scale,
    )
    fig.colorbar(quiver, ax=[ax_before, ax_after], label="Residual [um]")

    _draw_radial_profile(ax_profile, quality)

    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _magnitudes_um(vectors: ImageArray, report: ResidualReport) -> ImageArray:
    """残差ベクトル (N,2) [px] の大きさを um で返す."""
    return np.linalg.norm(vectors, axis=1) / report.pixel_per_mm * 1000.0


def _draw_quiver(
    ax: Axes,
    title: str,
    image_size: tuple[int, int],
    points: ImageArray,
    vectors: ImageArray,
    magnitudes_um: ImageArray,
    *,
    limit_um: float,
    arrow_scale: float,
) -> Quiver:
    """残差ベクトル場を画像座標系で描画し、カラーマップ付きの quiver を返す."""
    width, height = image_size
    quiver = ax.quiver(
        points[:, 0],
        points[:, 1],
        vectors[:, 0],
        vectors[:, 1],
        magnitudes_um,
        angles="xy",
        scale_units="xy",
        scale=arrow_scale if arrow_scale > 0.0 else 1.0,
        cmap="viridis",
        clim=(0.0, limit_um if limit_um > 0.0 else 1.0),
        width=0.003,
    )
    ax.set_title(title)
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)  # 画像座標系（y 下向き）
    ax.set_aspect("equal")
    ax.set_xlabel("X [px]")
    ax.set_ylabel("Y [px]")
    return quiver


def _draw_radial_profile(ax: Axes, quality: CalibrationQuality) -> None:
    """半径帯ごとの残差 RMS を折れ線で描画する.

    サンプルが入らなかった帯は rms 0.0 を報告するので点を打たない（打つと残差が そこで急に良くなったように見える）。
    """
    for report, label, color in (
        (quality.before, "before", "#cc4444"),
        (quality.after, "after", "#3377cc"),
    ):
        buckets = [bucket for bucket in report.buckets if bucket.sample_count]
        ax.plot(
            [bucket.radius_px[1] for bucket in buckets],
            [bucket.rms_um for bucket in buckets],
            marker="o",
            color=color,
            label=label,
        )
    ax.set_xlabel("Image radius (bucket upper bound) [px]")
    ax.set_ylabel("Residual RMS [um]")
    ax.set_title("Residual RMS vs image radius")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left")
