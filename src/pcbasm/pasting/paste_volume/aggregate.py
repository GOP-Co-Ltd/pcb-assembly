"""複数 view の直径を 1 つへ畳む.

集約は中央値に固定する。3 次モデルが被覆域で単調非減少で、かつ検出できた view 数が
奇数なら ``median(V(dᵢ)) == V(median(dᵢ))`` が厳密に成り立つので、「1 view ごとに
推定して集約する」ことと「直径を集約してから 1 回モデルへ通す」ことが一致する。

偶数のときは中央 2 つの平均になるため、モデルの 2 次以上のぶんだけ差が出る。実素材
の view 間ばらつき（相対 0.2% 程度）では相対 1e-4 未満で、実害は無い。検出できた
view が 1 つ落ちて偶数になる経路が実在するので、厳密一致には依存しない。

平均で畳むと view 数の偶奇によらず崩れ、推定器の内と外で結果が変わる。

マルチ view の主目的は検出失敗時のフォールバックで、測定精度の底上げではない
（実測では view 間ばらつきは体積換算 1.2%）。検出できなかった view は中央値から
除き、残った view で測る。
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

import attrs

from pcbasm.pasting.paste_volume.detect import DotMeasurement


@attrs.frozen
class DotDiameter:
    """1 セルぶんの直径（全 view を畳んだもの）.

    Attributes:
        diameter_mm: 検出できた view の中央値 [mm]（1 つも無ければ 0.0）
        view_count: 与えられた view 数
        detected_view_count: はんだを検出できた view 数
        view_diameters_mm: view ごとの直径 [mm]（未検出は 0.0。入力順）
        spread_mm: 検出できた view の最大 - 最小 [mm]（フォールバック品質の指標）
    """

    diameter_mm: float
    view_count: int
    detected_view_count: int
    view_diameters_mm: tuple[float, ...]
    spread_mm: float


def aggregate_views(measurements: Sequence[DotMeasurement]) -> DotDiameter:
    """View ごとの計測を 1 つの直径へ畳む.

    未検出（blank や検出失敗）は中央値の母数から外す。

    1 つも検出できなければ直径 0.0 を返す（blank セルの真値 0 と同じ形）。
    """
    diameters = tuple(item.diameter_mm for item in measurements)
    detected = [item.diameter_mm for item in measurements if item.detected]
    if not detected:
        return DotDiameter(
            diameter_mm=0.0,
            view_count=len(measurements),
            detected_view_count=0,
            view_diameters_mm=diameters,
            spread_mm=0.0,
        )
    return DotDiameter(
        diameter_mm=float(statistics.median(detected)),
        view_count=len(measurements),
        detected_view_count=len(detected),
        view_diameters_mm=diameters,
        spread_mm=float(max(detected) - min(detected)),
    )
