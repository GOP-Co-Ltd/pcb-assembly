#!/usr/bin/env python3
"""ポリゴン塗りつぶしパスのデモスクリプト.

複数のサンプルポリゴンに対して塗りつぶしパスを生成し、 ポリゴンの概形とパスをプロットしてPNG画像として保存する。
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from shapely import Polygon

from pcb_assembly.geometry.fill import generate_fill_path

OUTPUT_PATH = Path(__file__).parent.parent.parent / "data" / "fill_path_demo.png"


def _plot_fill_path(
    ax: Axes,
    title: str,
    polygon: Polygon,
    path: list,
) -> None:
    """1つのサブプロットにポリゴンとパスを描画する."""
    # ポリゴン外形（薄い塗りつぶし）
    poly_x, poly_y = polygon.exterior.xy
    ax.fill(poly_x, poly_y, alpha=0.15, color="gray")
    ax.plot(poly_x, poly_y, color="gray", linewidth=0.5, linestyle="--")

    if not path:
        ax.set_title(title)
        ax.set_aspect("equal")
        return

    xs = [p.x for p in path]
    ys = [p.y for p in path]

    # 周回パスとジグザグパスを分離（閉ループの終端で区切る）
    contour_end = 0
    i = 0
    while i < len(path):
        # 閉ループの終端を探す
        start = i
        for j in range(i + 1, len(path)):
            if path[j].x == path[start].x and path[j].y == path[start].y:
                contour_end = j + 1
                i = j + 1
                break
        else:
            break

    # 周回パス（青線）
    if contour_end > 0:
        ax.plot(
            xs[:contour_end],
            ys[:contour_end],
            color="blue",
            linewidth=1.5,
            label="contour",
        )

    # ジグザグパス（赤線 + 矢印）
    if contour_end < len(path):
        zx = xs[contour_end:]
        zy = ys[contour_end:]
        ax.plot(zx, zy, color="red", linewidth=1.0, label="zigzag")

        # 走査線ごとに矢印を追加（2点ずつペア）
        for j in range(0, len(zx) - 1, 2):
            mid_x = (zx[j] + zx[j + 1]) / 2
            mid_y = (zy[j] + zy[j + 1]) / 2
            dx = zx[j + 1] - zx[j]
            dy = zy[j + 1] - zy[j]
            length = np.sqrt(dx**2 + dy**2)
            if length > 0:
                ax.annotate(
                    "",
                    xy=(mid_x + dx / length * 0.2, mid_y + dy / length * 0.2),
                    xytext=(
                        mid_x - dx / length * 0.2,
                        mid_y - dy / length * 0.2,
                    ),
                    arrowprops={"arrowstyle": "->", "color": "red", "lw": 1.0},
                )

    ax.set_title(title)
    ax.set_aspect("equal")
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(True, alpha=0.3)


def main() -> None:
    samples = [
        (
            "1 perimeter",
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            {"line_spacing": 0.8},
        ),
        (
            "2 perimeters",
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            {"line_spacing": 0.8, "perimeters": 2},
        ),
        (
            "3 perimeters + inset",
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            {"line_spacing": 0.8, "perimeters": 3, "inset": 0.4},
        ),
        (
            "L-shape",
            Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)]),
            {"line_spacing": 0.8},
        ),
        (
            "Triangle (angle=45)",
            Polygon([(4, 0), (8, 6), (0, 6)]),
            {"line_spacing": 0.8, "angle": 45.0},
        ),
        (
            "2 perimeters + angle=30",
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            {"line_spacing": 0.8, "perimeters": 2, "angle": 30.0},
        ),
    ]

    fig, axes = plt.subplots(2, 3, figsize=(15, 10))
    fig.suptitle("Fill Path Demo", fontsize=16)

    for ax, (title, polygon, kwargs) in zip(axes.flat, samples):
        path = generate_fill_path(polygon, **kwargs)
        _plot_fill_path(ax, title, polygon, path)

    plt.tight_layout()
    plt.savefig(OUTPUT_PATH, dpi=150)
    print(f"保存: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
