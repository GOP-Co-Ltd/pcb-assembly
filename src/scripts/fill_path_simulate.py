#!/usr/bin/env python3
"""KiCad PCBから paste pad ごとの fill path を生成し、可視化するスクリプト.

実 PCB データを読み込み、指定レイヤの paste pad に対して
``build_paste_fill_path`` で塗布経路を生成、PCB outline と重ねた
1枚の PNG として書き出す。

起動例::

    uv run python -m scripts.fill_path_simulate <pcb_file> \\
        --nozzle-diameter 0.4 --layer top -o /tmp/fill_path.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon
from shapely.geometry import LineString

from pcb_assembly.control.pasting.fill_path import build_paste_fill_path
from pcb_assembly.geometry import Point2d
from pcb_assembly.pcb import Layer, Outline, PadList, PcbFile
from pcb_assembly.visualization import polygon_with_holes_patch

# fill path 描画の配色（凡例とパス描画で共有）
_HALO_COLOR = "#3399ff"
_CENTER_COLOR = "#cce6ff"
_START_COLOR = "#ff3333"


def render_fill_paths(
    outline: Outline,
    pads: PadList,
    paths: list[list[Point2d]],
    nozzle_diameter: float,
    layer: Layer,
    output_path: Path,
) -> None:
    """Outline・paste pad・fill path を1枚の PNG に重ね描きして保存する.

    ``paths`` は ``pads`` と同じ並びで、各要素は対応 pad の塗布経路。
    パスが構築できなかった pad には空リストが入る想定。
    """
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
        pad_facecolor = "#00aa00"
        pad_edgecolor = "#00ff00"
    else:
        pad_facecolor = "#aa0000"
        pad_edgecolor = "#ff0000"

    for pad in pads:
        coords = list(pad.polygon.exterior.coords)
        ax.add_patch(
            MplPolygon(
                coords,
                closed=True,
                facecolor=pad_facecolor,
                edgecolor=pad_edgecolor,
                linewidth=0.5,
                alpha=0.8,
            )
        )

    # 各 pad の fill path
    for path in paths:
        _draw_fill_path(ax, path, nozzle_diameter)

    # 軸設定
    ax.autoscale()
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.invert_yaxis()  # KiCadと同じ座標系

    title = (
        f"Board: {outline.width:.1f}x{outline.height:.1f}mm, "
        f"Pads ({layer.value}): {len(pads)}, "
        f"Nozzle: {nozzle_diameter:.2f}mm"
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


def _draw_fill_path(ax: Axes, path: list[Point2d], nozzle_diameter: float) -> None:
    """1本の fill path を ax に描画する.

    - halo（ノズル塗布幅の半透明領域）
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

    # ノズル塗布幅 halo: shapely.buffer で mm 単位ポリゴン化
    coords = [(p.x, p.y) for p in path]
    halo = LineString(coords).buffer(nozzle_diameter / 2)
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


def _pads_on_layer(pads: PadList, layer: Layer) -> PadList:
    """指定レイヤの pad のみを抽出する."""
    return PadList(pad for pad in pads if pad.layer == layer)


def _build_paths(pads: PadList, nozzle_diameter: float) -> list[list[Point2d]]:
    """各 pad の塗布経路を ``pads`` と同じ並びで返す."""
    return [build_paste_fill_path(pad.polygon, nozzle_diameter) for pad in pads]


def _parse_layer(value: str) -> Layer:
    """``--layer`` 引数を ``Layer`` に変換する."""
    return {"top": Layer.TOP, "bottom": Layer.BOTTOM}[value]


def main() -> None:
    """CLI エントリポイント: PCB を読み込み fill path を可視化 PNG に出力する."""
    parser = argparse.ArgumentParser(
        description=("実 PCB データを読み込み、paste pad ごとの fill path を可視化する")
    )
    parser.add_argument("pcb_file", type=Path, help="KiCad PCBファイル (.kicad_pcb)")
    parser.add_argument(
        "--nozzle-diameter",
        "-d",
        type=float,
        default=0.4,
        help="ノズル内径 [mm] (default: 0.4)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help=(
            "出力 PNG パス (default: PCBファイルと同じディレクトリの"
            " <stem>_fill_path.png)"
        ),
    )
    parser.add_argument(
        "--layer",
        choices=("top", "bottom"),
        default="top",
        help="描画する paste pad のレイヤ (default: top)",
    )
    args = parser.parse_args()

    pcb_path: Path = args.pcb_file
    if not pcb_path.exists():
        print(f"エラー: ファイルが見つかりません: {pcb_path}")
        return

    layer = _parse_layer(args.layer)
    output_path: Path = args.output or (
        pcb_path.parent / f"{pcb_path.stem}_fill_path.png"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"PCBファイルを読み込み中: {pcb_path}")
    pcb = PcbFile(pcb_path)

    outline = pcb.outline
    print(f"  サイズ: {outline.width:.2f} x {outline.height:.2f} mm")

    pads = _pads_on_layer(pcb.pads, layer)
    print(f"  {layer.value} レイヤの paste pad 数: {len(pads)}")

    paths = _build_paths(pads, args.nozzle_diameter)
    non_empty = sum(1 for p in paths if p)
    print(f"  fill path 生成: {non_empty} / {len(paths)} 成功")

    render_fill_paths(
        outline=outline,
        pads=pads,
        paths=paths,
        nozzle_diameter=args.nozzle_diameter,
        layer=layer,
        output_path=output_path,
    )
    print(f"  画像 -> {output_path}")
    print("完了")


if __name__ == "__main__":
    main()
