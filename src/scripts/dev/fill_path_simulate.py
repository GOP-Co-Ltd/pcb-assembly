#!/usr/bin/env python3
"""KiCad PCBから paste pad ごとの fill path を生成し、可視化するスクリプト.

実 PCB データを読み込み、指定レイヤの paste pad に対して
``build_paste_fill_path`` で塗布経路を生成、PCB outline と重ねた
1枚の PNG として書き出す。

被覆パラメータ（``--bead-width-factor`` / ``--overlap`` / ``--boundary-margin``）で
``build_paste_fill_path`` の塗布幅・行間・外周マージンを調整して可視化できる。

起動例::

    uv run python -m scripts.dev.fill_path_simulate <pcb_file> \\
        --nozzle-diameter 0.4 --layer top -o /tmp/fill_path.png

    uv run python -m scripts.dev.fill_path_simulate <pcb_file> \\
        -d 0.4 --overlap 0.3 --boundary-margin 0.2 -o /tmp/fill_path.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon
from shapely import Polygon as ShapelyPolygon
from shapely.geometry import LineString
from shapely.ops import unary_union

from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import build_paste_fill_path
from pcbasm.pcb import Layer, Outline, PadList, PcbFile
from pcbasm.visualization import polygon_with_holes_patch

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
            halos.append(
                LineString(
                    [(component[0].x, component[0].y), (component[0].x, component[0].y)]
                ).buffer(bead_width / 2)
            )
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


def _pads_on_layer(pads: PadList, layer: Layer) -> PadList:
    """指定レイヤの pad のみを抽出する."""
    return PadList(pad for pad in pads if pad.layer == layer)


def _build_paths(
    pads: PadList,
    nozzle_diameter: float,
    *,
    bead_width_factor: float,
    overlap: float,
    boundary_margin: float,
) -> list[list[list[Point2d]]]:
    """各 pad の成分別塗布経路を ``pads`` と同じ並びで返す.

    ``build_paste_fill_path`` は成分別ポリラインのリストを返すため、戻り値は
    「パッド × 成分 × ポリライン点列」の三重リストとなる。被覆パラメータ
    （``bead_width_factor`` / ``overlap`` / ``boundary_margin``）はそのまま委譲する。
    """
    return [
        build_paste_fill_path(
            pad.polygon,
            nozzle_diameter,
            bead_width_factor=bead_width_factor,
            overlap=overlap,
            boundary_margin=boundary_margin,
        )
        for pad in pads
    ]


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
    parser.add_argument(
        "--bead-width-factor",
        "-b",
        type=float,
        default=1.0,
        help="ビード幅係数 w = nozzle * factor (default: 1.0)",
    )
    parser.add_argument(
        "--overlap",
        type=float,
        default=0.0,
        help="ジグザグ行間オーバーラップ [0,1) (default: 0.0)",
    )
    parser.add_argument(
        "--boundary-margin",
        "-m",
        type=float,
        default=0.0,
        help="外周マージン [mm] (default: 0.0)",
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

    paths = _build_paths(
        pads,
        args.nozzle_diameter,
        bead_width_factor=args.bead_width_factor,
        overlap=args.overlap,
        boundary_margin=args.boundary_margin,
    )
    non_empty = sum(1 for components in paths if components)
    print(f"  fill path 生成: {non_empty} / {len(paths)} 成功")

    render_fill_paths(
        outline=outline,
        pads=pads,
        paths=paths,
        nozzle_diameter=args.nozzle_diameter,
        bead_width_factor=args.bead_width_factor,
        overlap=args.overlap,
        boundary_margin=args.boundary_margin,
        layer=layer,
        output_path=output_path,
    )
    print(f"  画像 -> {output_path}")
    print("完了")


if __name__ == "__main__":
    main()
