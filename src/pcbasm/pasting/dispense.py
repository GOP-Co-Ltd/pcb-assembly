"""塗布実績の集計値.

HAL を参照しない値オブジェクトだけを置く。収集した dataset を学習側で読むとき、
``pcbasm.hal``（picamera2 / OpenCV を要求する）を引き込まずに schema を扱えるようにする。

:class:`DispenseExecution` は :class:`~pcbasm.hal.Speed` を持つため
:mod:`pcbasm.pasting.applicator` に残す。
"""

from __future__ import annotations

from typing import Literal

import attrs

from pcbasm.pasting.fill_path import AppliedDispenseMode


@attrs.frozen
class DispenseSummary:
    """複数 ``DispenseExecution`` の集計（dataset metadata の ``execution`` にもそのまま使う）.

    Attributes:
        applied_mode: 全成分が同一方式ならその方式、混在なら ``"mixed"``、成分無しは ``None``
        path_length_mm: 経路長の合計 [mm]
        commanded_volume_ul: 指令量の合計 [μL]
        prime_extra_volume_ul: プライム追加量の合計 [μL]
        effective_rate_ul_s: 体積加重の実効吐出レート [μL/sec]
        rotations: 回転数の合計 [rev]
    """

    applied_mode: AppliedDispenseMode | Literal["mixed"] | None
    path_length_mm: float
    commanded_volume_ul: float
    prime_extra_volume_ul: float
    effective_rate_ul_s: float
    rotations: float
