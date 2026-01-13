#!/usr/bin/env python3
"""KiCad PCBファイルから部品・パッド情報を抽出するスクリプト."""

import argparse
from pathlib import Path

from pcb_assembly.pcb import extract_components, extract_pads


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KiCad PCBファイルから部品・パッド情報を抽出"
    )
    parser.add_argument("pcb_file", type=Path, help="KiCad PCBファイル (.kicad_pcb)")
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="出力ディレクトリ (デフォルト: PCBファイルと同じ場所)",
    )
    parser.add_argument(
        "--components-only",
        "-c",
        action="store_true",
        help="部品情報のみ抽出",
    )
    parser.add_argument(
        "--pads-only",
        "-p",
        action="store_true",
        help="パッド情報のみ抽出",
    )
    args = parser.parse_args()

    pcb_path: Path = args.pcb_file
    if not pcb_path.exists():
        print(f"エラー: ファイルが見つかりません: {pcb_path}")
        return

    output_dir: Path = args.output_dir or pcb_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    base_name = pcb_path.stem

    extract_comps = not args.pads_only
    extract_pads_flag = not args.components_only

    if extract_comps:
        print(f"部品情報を抽出中: {pcb_path}")
        components = extract_components(pcb_path)
        csv_path = output_dir / f"{base_name}_pnp.csv"
        components.save(csv_path)
        print(f"  {len(components)} 部品 -> {csv_path}")

    if extract_pads_flag:
        print(f"パッド情報を抽出中: {pcb_path}")
        pads = extract_pads(pcb_path)
        json_path = output_dir / f"{base_name}_pads.json"
        pads.save(json_path)
        print(f"  {len(pads)} パッド -> {json_path}")

    print("完了")


if __name__ == "__main__":
    main()
