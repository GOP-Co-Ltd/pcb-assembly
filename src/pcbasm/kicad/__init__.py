"""KiCad（pcbnew）との境界。pcbnew を import するのはこのパッケージだけ.

pcbnew（KiCad 9 以降）が無い環境では、このパッケージのどの名前も import できない。

- :mod:`.read`: ``.kicad_pcb`` から基板データ（:mod:`pcbasm.board`）を読む
- :mod:`.build`: テスト用 PCB の生成と保存
- :mod:`.footprint`: footprint ライブラリの検索・読込と、footprint / pad の幾何
- :mod:`.units`: KiCad 内部単位（nm）と mm の変換

``build`` / ``footprint`` / ``units`` は re-export しないので、モジュールを直接 import する。
"""

from .read import load_board

__all__ = ["load_board"]
