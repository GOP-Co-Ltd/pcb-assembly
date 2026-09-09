"""掃引点列の等間隔分割（塗布ドメイン共通の純関数）."""

from __future__ import annotations


def sweep_schedule(minimum: float, maximum: float, divisions: int) -> tuple[float, ...]:
    """``minimum`` から ``maximum`` までを ``divisions`` 点に等間隔分割した昇順列.

    流量キャリブレーションの吐出レート列 [μL/sec] / 塗布速度列 [mm/sec] と、dataset
    収集の吐出量列 [μL] が共用する。

    Args:
        minimum: 最小値（0 より大きい）
        maximum: 最大値（``minimum`` 以上）
        divisions: 点数（1 以上）

    Returns:
        昇順の列（端点含む）。入力不正なら空。
    """
    if divisions < 1 or minimum <= 0.0 or maximum < minimum:
        return ()
    if divisions == 1:
        return (minimum,)
    step = (maximum - minimum) / (divisions - 1)
    return tuple(minimum + step * i for i in range(divisions))
