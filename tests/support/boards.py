"""テストで繰り返し使う KiCad 基板を、1 プロセスにつき 1 回だけ読んで共有する道具.

`load_board` は銅箔の塗り潰し（pcbnew の zone fill）を含み 1 回 0.3 秒ほどかかる。
`Board` とその中身は frozen な attrs と shapely の幾何なので、共有しても書き換わらない。
"""

from __future__ import annotations

import functools

from pcbasm.board import Board
from pcbasm.kicad import load_board
from tests.support.paths import LED_BLINKER_PCB


@functools.cache
def led_blinker_board() -> Board:
    """led_blinker の `Board`（初回だけ読み、以後は同じものを返す）."""
    return load_board(LED_BLINKER_PCB)
