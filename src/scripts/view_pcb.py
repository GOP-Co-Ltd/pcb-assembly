#!/usr/bin/env python3
"""PCB抽出データのビューアスクリプト."""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon

from pcb_assembly.pcb import ComponentList, Layer, Outline, PadList


def main() -> None:
    parser = argparse.ArgumentParser(description="PCB抽出データの可視化")
    parser.add_argument(
        "--outline",
        type=Path,
        default=None,
        help="アウトラインJSONファイル (*_outline.json)",
    )
    parser.add_argument(
        "--pads",
        "-p",
        type=Path,
        default=None,
        help="パッドJSONファイル (*_pads.json)",
    )
    parser.add_argument(
        "--components",
        "-c",
        type=Path,
        default=None,
        help="部品CSVファイル (*_pnp.csv)",
    )
    parser.add_argument(
        "--layer",
        "-l",
        choices=["top", "bottom", "both"],
        default="both",
        help="表示レイヤー",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="出力画像ファイル (指定しない場合はウィンドウ表示)",
    )
    args = parser.parse_args()

    if not args.outline and not args.pads and not args.components:
        parser.error("--outline, --pads, --components のいずれかを指定してください")

    outline = Outline.load(args.outline) if args.outline else None
    pads = PadList.load(args.pads) if args.pads else PadList()
    components = (
        ComponentList.load(args.components) if args.components else ComponentList()
    )

    # フィルタリング
    if args.layer == "top":
        pads = PadList([p for p in pads if p.layer == Layer.TOP])
        components = ComponentList([c for c in components if c.layer == Layer.TOP])
    elif args.layer == "bottom":
        pads = PadList([p for p in pads if p.layer == Layer.BOTTOM])
        components = ComponentList([c for c in components if c.layer == Layer.BOTTOM])

    if not outline and not pads and not components:
        print("表示するデータがありません")
        return

    fig, ax = plt.subplots(figsize=(12, 10))
    ax.set_aspect("equal")
    ax.set_facecolor("#2a2a2a")

    # アウトライン描画
    if outline:
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

        ax.plot(comp.x, comp.y, "+", color=color, markersize=8, markeredgewidth=1)
        ax.annotate(
            comp.designator,
            (comp.x, comp.y),
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
    title_parts = []
    if outline:
        title_parts.append(f"Board: {outline.width:.1f}x{outline.height:.1f}mm")
    if pads:
        title_parts.append(f"Pads: {len(pads)}")
    if components:
        title_parts.append(f"Components: {len(components)}")
    title = ", ".join(title_parts) + f" (Layer: {args.layer})"
    ax.set_title(title, color="white")

    # 凡例
    legend_elements: list[Artist] = []
    if outline:
        legend_elements.append(
            Line2D(
                [0],
                [0],
                color="#ffffff",
                linewidth=1.5,
                linestyle="--",
                label="Board Outline",
            )
        )
    if pads:
        legend_elements.extend(
            [
                Patch(facecolor="#00aa00", edgecolor="#00ff00", label="Top Pad"),
                Patch(facecolor="#aa0000", edgecolor="#ff0000", label="Bottom Pad"),
            ]
        )
    if components:
        legend_elements.extend(
            [
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
        )

    if legend_elements:
        ax.legend(
            handles=legend_elements,
            loc="upper right",
            facecolor="#404040",
            labelcolor="white",
        )

    plt.tight_layout()

    if args.output:
        plt.savefig(args.output, dpi=150, facecolor="#1a1a1a")
        print(f"画像を保存: {args.output}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
