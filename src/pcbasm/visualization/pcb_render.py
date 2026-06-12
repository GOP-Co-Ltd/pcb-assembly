"""PCB 全体図（アウトライン・銅箔・パッド・部品位置）の PNG レンダリング."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon

from pcbasm.pcb import ComponentList, CopperList, Layer, Outline, PadList

from .patches import polygon_with_holes_patch


def render_pcb(
    outline: Outline,
    pads: PadList,
    copper: CopperList,
    components: ComponentList,
    output_path: Path,
) -> None:
    """アウトライン・銅箔・パッド・部品位置を1枚のPNGに重ね描きして保存する."""
    fig, ax = plt.subplots(figsize=(12, 10))
    ax.set_aspect("equal")
    ax.set_facecolor("#2a2a2a")

    # アウトライン描画
    coords = list(outline.polygon.exterior.coords)
    outline_polygon = MplPolygon(
        coords,
        closed=True,
        facecolor="none",
        edgecolor="#ffffff",
        linewidth=1.5,
        linestyle="--",
    )
    ax.add_patch(outline_polygon)

    # 銅箔描画 (パッドより下に配置)
    for cu in copper:
        if cu.layer == Layer.TOP:
            facecolor = "#cc8844"
            edgecolor = "#cc8844"
        else:
            facecolor = "#884422"
            edgecolor = "#884422"

        ax.add_patch(
            polygon_with_holes_patch(
                cu.polygon,
                facecolor=facecolor,
                edgecolor=edgecolor,
                alpha=0.4,
                linewidth=0.3,
            )
        )

    # パッド描画
    for pad in pads:
        coords = list(pad.polygon.exterior.coords)

        if pad.layer == Layer.TOP:
            facecolor = "#00aa00"
            edgecolor = "#00ff00"
        else:
            facecolor = "#aa0000"
            edgecolor = "#ff0000"

        polygon = MplPolygon(
            coords,
            closed=True,
            facecolor=facecolor,
            edgecolor=edgecolor,
            linewidth=0.5,
            alpha=0.8,
        )
        ax.add_patch(polygon)

    # 部品位置描画
    for comp in components:
        if comp.layer == Layer.TOP:
            color = "#ffff00"
        else:
            color = "#00ffff"

        ax.plot(
            comp.position.x,
            comp.position.y,
            "+",
            color=color,
            markersize=8,
            markeredgewidth=1,
        )
        ax.annotate(
            comp.designator,
            (comp.position.x, comp.position.y),
            xytext=(3, 3),
            textcoords="offset points",
            fontsize=7,
            color=color,
        )

    # 軸設定
    ax.autoscale()
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.invert_yaxis()  # KiCadと同じ座標系

    # タイトル
    title = (
        f"Board: {outline.width:.1f}x{outline.height:.1f}mm, "
        f"Pads: {len(pads)}, Copper: {len(copper)}, Components: {len(components)}"
    )
    ax.set_title(title, color="white")

    # 凡例
    legend_elements: list[Artist] = [
        Line2D(
            [0],
            [0],
            color="#ffffff",
            linewidth=1.5,
            linestyle="--",
            label="Board Outline",
        ),
        Patch(facecolor="#00aa00", edgecolor="#00ff00", label="Top Pad"),
        Patch(facecolor="#aa0000", edgecolor="#ff0000", label="Bottom Pad"),
        Patch(
            facecolor="#cc8844",
            edgecolor="#cc8844",
            alpha=0.4,
            label="Top Copper",
        ),
        Patch(
            facecolor="#884422",
            edgecolor="#884422",
            alpha=0.4,
            label="Bottom Copper",
        ),
        Line2D(
            [0],
            [0],
            marker="+",
            color="w",
            markerfacecolor="#ffff00",
            markeredgecolor="#ffff00",
            markersize=8,
            linestyle="None",
            label="Top Component",
        ),
        Line2D(
            [0],
            [0],
            marker="+",
            color="w",
            markerfacecolor="#00ffff",
            markeredgecolor="#00ffff",
            markersize=8,
            linestyle="None",
            label="Bottom Component",
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
