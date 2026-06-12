#!/usr/bin/env python3
"""KiCad PCBファイルから部品・パッド・銅箔情報を抽出し、可視化するスクリプト."""

import argparse
from pathlib import Path

from pcbasm.pcb import PcbFile
from pcbasm.visualization import render_pcb


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
