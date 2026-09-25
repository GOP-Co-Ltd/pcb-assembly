"""Paste pad の fill path（塗布経路）可視化 PNG レンダリング."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon
from shapely import Polygon as ShapelyPolygon
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from pcbasm.geometry import Point2d
from pcbasm.pcb import Layer, Outline, PadList

from .patches import polygon_with_holes_patch

# fill path 描画の配色（凡例とパス描画で共有）
_HALO_COLOR = "#3399ff"
_CENTER_COLOR = "#cce6ff"
_START_COLOR = "#ff3333"


def render_fill_paths(
    outline: Outline,
    pads: PadList,
    paths: list[list[list[Point2d]]],
    nozzle_diameter: float,
    bead_width_factor: float,
    overlap: float,
    boundary_margin: float,
    layer: Layer,
    output_path: Path,
) -> None:
    """Outline・paste pad・fill path を1枚の PNG に重ね描きして保存する.

    ``paths`` は ``pads`` と同じ並びで、各要素は対応 pad の成分別塗布経路
    （``list[list[Point2d]]``）。パスが構築できなかった pad には空リストが入る。
    halo / 被覆率はビード幅 ``w = nozzle_diameter * bead_width_factor`` で描く。

    ``overlap`` と ``boundary_margin`` はタイトルに表示するだけで、描画には使わない。

    入力は基板座標 [mm] で、図の Y 軸は下向き（KiCad と同じ見た目）。
    """
    bead_width = nozzle_diameter * bead_width_factor
    fig, ax = plt.subplots(figsize=(12, 10))
    ax.set_aspect("equal")
    ax.set_facecolor("#2a2a2a")

    # PCB outline (白破線)
    outline_coords = list(outline.polygon.exterior.coords)
    ax.add_patch(
        MplPolygon(
            outline_coords,
            closed=True,
            facecolor="none",
            edgecolor="#ffffff",
            linewidth=1.5,
            linestyle="--",
        )
    )

    # paste pad 多角形 (top: 緑 / bottom: 赤)
    if layer == Layer.TOP:
        pad_facecolor, pad_edgecolor = "#00aa00", "#00ff00"
    else:
        pad_facecolor, pad_edgecolor = "#aa0000", "#ff0000"

    for pad in pads:
        ax.add_patch(
            MplPolygon(
                list(pad.polygon.exterior.coords),
                closed=True,
                facecolor=pad_facecolor,
                edgecolor=pad_edgecolor,
                linewidth=0.5,
                alpha=0.8,
            )
        )

    # 各 pad の fill path（成分単位で描画）
    for pad, components in zip(pads, paths):
        for component in components:
            _draw_fill_path(ax, component, bead_width)
        _annotate_coverage(ax, pad.polygon, components, bead_width)

    # 軸設定
    ax.autoscale()
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.invert_yaxis()  # KiCadと同じ座標系

    title = (
        f"Board: {outline.width:.1f}x{outline.height:.1f}mm, "
        f"Pads ({layer.value}): {len(pads)}, "
        f"Nozzle: {nozzle_diameter:.2f}mm\n"
        f"bead×{bead_width_factor:.2f}  overlap={overlap:.2f}  "
        f"margin={boundary_margin:.2f}mm"
    )
    ax.set_title(title, color="white")

    legend_elements: list[Artist] = [
        Line2D(
            [0],
            [0],
            color="#ffffff",
            linewidth=1.5,
            linestyle="--",
            label="Board Outline",
        ),
        Patch(
            facecolor=pad_facecolor,
            edgecolor=pad_edgecolor,
            label=f"{layer.value} Paste Pad",
        ),
        Patch(
            facecolor=_HALO_COLOR,
            edgecolor=_HALO_COLOR,
            alpha=0.3,
            label="Nozzle Coverage",
        ),
        Line2D([0], [0], color=_CENTER_COLOR, linewidth=0.6, label="Fill Path"),
        Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor=_START_COLOR,
            markeredgecolor=_START_COLOR,
            markersize=4,
            linestyle="None",
            label="Path Start",
        ),
    ]
    ax.legend(
        handles=legend_elements,
        loc="upper right",
        facecolor="#404040",
        labelcolor="white",
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, facecolor="#1a1a1a")
    plt.close(fig)


def _draw_fill_path(ax: Axes, path: list[Point2d], bead_width: float) -> None:
    """1本の fill path を ax に描画する.

    - halo（ビード塗布幅 ``bead_width`` の半透明領域）
    - 中心線
    - 始点マーカー
    - 始点→次点の方向矢印

    1点パスは halo / 矢印を描けないため始点マーカーのみ描画する。
    空パスは何も描かない。
    """
    if not path:
        return

    if len(path) == 1:
        _plot_start_marker(ax, path[0])
        return

    # ビード塗布幅 halo: shapely.buffer で mm 単位ポリゴン化
    coords = [(p.x, p.y) for p in path]
    halo = LineString(coords).buffer(bead_width / 2)
    if not halo.is_empty and halo.geom_type == "Polygon":
        ax.add_patch(
            polygon_with_holes_patch(
                halo,
                facecolor=_HALO_COLOR,
                edgecolor=_HALO_COLOR,
                alpha=0.3,
                linewidth=0.0,
            )
        )

    # 中心線 (細い青実線)
    xs = [p.x for p in path]
    ys = [p.y for p in path]
    ax.plot(xs, ys, "-", color=_CENTER_COLOR, linewidth=0.6)

    # 始点 (赤丸)
    _plot_start_marker(ax, path[0])

    # 方向矢印: 始点 -> 次点
    start, nxt = path[0], path[1]
    ax.annotate(
        "",
        xy=(nxt.x, nxt.y),
        xytext=(start.x, start.y),
        arrowprops={
            "arrowstyle": "->",
            "color": _START_COLOR,
            "lw": 0.8,
        },
    )


def _annotate_coverage(
    ax: Axes,
    polygon: ShapelyPolygon,
    components: list[list[Point2d]],
    bead_width: float,
) -> None:
    """成分別パスのビード幅 buffer による被覆率を pad 中心に注記する.

    被覆率 = ``union(path.buffer(w/2)).area / polygon.area``（w = bead_width）。
    パスが空、または面積0の場合は注記しない。
    """
    if polygon.area <= 0:
        return

    halos = []
    for component in components:
        if len(component) >= 2:
            halos.append(
                LineString([(p.x, p.y) for p in component]).buffer(bead_width / 2)
            )
        elif len(component) == 1:
            halos.append(Point(component[0].x, component[0].y).buffer(bead_width / 2))
    if not halos:
        return

    covered = unary_union(halos).intersection(polygon).area
    ratio = covered / polygon.area
    rep = polygon.representative_point()
    ax.annotate(
        f"{ratio * 100:.0f}%",
        xy=(rep.x, rep.y),
        color="white",
        fontsize=5,
        ha="center",
        va="center",
    )


def _plot_start_marker(ax: Axes, point: Point2d) -> None:
    """パス始点の赤丸マーカーを描く."""
    ax.plot(
        point.x,
        point.y,
        "o",
        color=_START_COLOR,
        markersize=4,
        markeredgecolor=_START_COLOR,
    )
