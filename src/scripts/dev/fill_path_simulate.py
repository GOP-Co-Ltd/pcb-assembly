#!/usr/bin/env python3
"""KiCad PCBから paste pad ごとの fill path を生成し、可視化するスクリプト.

実 PCB データを読み込み、指定レイヤの paste pad に対して
``build_paste_fill_path`` で塗布経路を生成、PCB outline と重ねた
1枚の PNG として書き出す（描画は ``pcbasm.visualization.render_fill_paths``）。

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

from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import build_paste_fill_path
from pcbasm.pcb import Layer, PadList, PcbFile
from pcbasm.visualization import render_fill_paths


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
