#!/usr/bin/env python3
"""KiCad PCBファイルから部品・パッド・銅箔情報を抽出し、可視化するスクリプト."""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon

from pcbasm.pcb import (
    ComponentList,
    CopperList,
    Layer,
    Outline,
    PadList,
    PcbFile,
)
from pcbasm.visualization import polygon_with_holes_patch


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KiCad PCBファイルから部品・パッド情報を抽出し、可視化"
    )
    parser.add_argument("pcb_file", type=Path, help="KiCad PCBファイル (.kicad_pcb)")
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="出力ディレクトリ (デフォルト: PCBファイルと同じ場所)",
    )
    args = parser.parse_args()

    pcb_path: Path = args.pcb_file
    if not pcb_path.exists():
        print(f"エラー: ファイルが見つかりません: {pcb_path}")
        return

    output_dir: Path = args.output_dir or pcb_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    base_name = pcb_path.stem

    # PCBファイルを読み込み
    print(f"PCBファイルを読み込み中: {pcb_path}")
    pcb = PcbFile(pcb_path)

    # 基板アウトラインを保存
    outline = pcb.outline
    print(f"  サイズ: {outline.width:.2f} x {outline.height:.2f} mm")
    outline_path = output_dir / f"{base_name}_outline.json"
    outline.save(outline_path)
    print(f"  アウトライン -> {outline_path}")

    # 部品情報を保存
    components = pcb.components
    csv_path = output_dir / f"{base_name}_pnp.csv"
    components.save(csv_path)
    print(f"  {len(components)} 部品 -> {csv_path}")

    # パッド情報を保存
    pads = pcb.pads
    json_path = output_dir / f"{base_name}_pads.json"
    pads.save(json_path)
    print(f"  {len(pads)} パッド -> {json_path}")

    # 銅箔情報を保存
    copper = pcb.copper
    copper_path = output_dir / f"{base_name}_copper.json"
    copper.save(copper_path)
    print(f"  {len(copper)} 銅箔島 -> {copper_path}")

    # 画像として描画・保存
    print("PCB画像を生成中...")
    image_path = output_dir / f"{base_name}_pcb.png"
    render_pcb(outline, pads, copper, components, image_path)
    print(f"  画像 -> {image_path}")

    print("完了")


if __name__ == "__main__":
    main()
