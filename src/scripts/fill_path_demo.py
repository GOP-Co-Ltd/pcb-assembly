#!/usr/bin/env python3
"""ポリゴン塗りつぶしパスのデモスクリプト.

geometry の純粋プリミティブ（generate_spiral_path / generate_linear_path）
を複数のサンプルポリゴンへ適用し、ポリゴンの概形とパスをプロットして PNG 画像として保存する。
"""

from collections.abc import Callable
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from shapely import Polygon

from pcb_assembly.geometry import (
    Point2d,
    generate_linear_path,
    generate_spiral_path,
)

OUTPUT_PATH = Path(__file__).parent.parent.parent / "data" / "fill_path_demo.png"


def _plot_fill_path(
    ax: Axes,
    title: str,
    polygon: Polygon,
    path: list[Point2d],
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
    ax.plot(xs, ys, color="blue", linewidth=1.0)

    # 一定間隔で矢印を追加
    step = max(1, len(xs) // 15)
    for j in range(0, len(xs) - 1, step):
        dx = xs[j + 1] - xs[j]
        dy = ys[j + 1] - ys[j]
        length = (dx**2 + dy**2) ** 0.5
        if length > 0:
            mid_x = (xs[j] + xs[j + 1]) / 2
            mid_y = (ys[j] + ys[j + 1]) / 2
            ax.annotate(
                "",
                xy=(mid_x + dx / length * 0.2, mid_y + dy / length * 0.2),
                xytext=(
                    mid_x - dx / length * 0.2,
                    mid_y - dy / length * 0.2,
                ),
                arrowprops={"arrowstyle": "->", "color": "blue", "lw": 1.0},
            )

    ax.set_title(title)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)


def main() -> None:
    samples: list[tuple[str, Polygon, Callable[[Polygon], list[Point2d]]]] = [
        (
            "Spiral - rectangle",
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            lambda p: generate_spiral_path(p, line_spacing=0.8, initial_inset=0.4),
        ),
        (
            "Spiral - large rectangle",
            Polygon([(0, 0), (12, 0), (12, 8), (0, 8)]),
            lambda p: generate_spiral_path(p, line_spacing=0.8, initial_inset=0.4),
        ),
        (
            "Spiral - L-shape",
            Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)]),
            lambda p: generate_spiral_path(p, line_spacing=0.8, initial_inset=0.4),
        ),
        (
            "Spiral - triangle",
            Polygon([(4, 0), (8, 6), (0, 6)]),
            lambda p: generate_spiral_path(p, line_spacing=0.8, initial_inset=0.4),
        ),
        (
            "Spiral - dumbbell (split)",
            Polygon(
                [
                    (0, 0),
                    (4, 0),
                    (4, 1.4),
                    (6, 1.4),
                    (6, 0),
                    (10, 0),
                    (10, 4),
                    (6, 4),
                    (6, 2.6),
                    (4, 2.6),
                    (4, 4),
                    (0, 4),
                ]
            ),
            lambda p: generate_spiral_path(p, line_spacing=0.8, initial_inset=0.4),
        ),
        (
            "Linear - narrow",
            Polygon([(0, 0), (0.6, 0), (0.6, 5), (0, 5)]),
            lambda p: generate_linear_path(p, end_inset=0.3),
        ),
    ]

    fig, axes = plt.subplots(2, 3, figsize=(15, 10))
    fig.suptitle("Fill Path Demo", fontsize=16)

    for ax, (title, polygon, build_path) in zip(axes.flat, samples):
        path = build_path(polygon)
        _plot_fill_path(ax, title, polygon, path)

    plt.tight_layout()
    plt.savefig(OUTPUT_PATH, dpi=150)
    print(f"保存: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
