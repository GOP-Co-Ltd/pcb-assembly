"""ピンセットのロードセルの校正（既知の重さから ``counts_per_gram`` を求める）.

手順はジョブ ``load_cell_calibrate`` が並べる: 何も載せずに風袋引き → 既知の重さを載せる → 風袋からの差 [counts]
の平均を測る → ここで ``counts_per_gram`` を求める。考え方は Klipper の ``klipper/docs/Load_Cell.md`` の
TARE と CALIBRATE（仕様として読んだだけで、klippy のコードは写していない）。

決めたこと:

- **符号:** 差の絶対値で求める。押す向きと逆に掛けた荷重でも、感度の大きさは同じ
- **小さすぎる差:** 1000 counts 未満は受けない（kurousagi の 2667 counts/g で約 0.4 g）。HX711 のノイズと丸めで
  値がぶれるため
"""

from __future__ import annotations

COUNTS_PER_GRAM_KEY = "pnp.motion.load_cell.counts_per_gram"

_MIN_COUNTS = 1000.0


def counts_per_gram(counts: float, grams: float) -> float:
    """風袋からの差 ``counts`` と、載せた重さ ``grams`` [g] から 1 g あたりの値を求める.

    Raises:
        ValueError: 重さが正でない、または差が小さすぎる
    """
    if not grams > 0:
        raise ValueError(f"重さは正にしてください: {grams:g} g")
    if abs(counts) < _MIN_COUNTS:
        raise ValueError(
            f"重さの差が小さすぎて校正できません（{abs(counts):.0f} counts。"
            f"{_MIN_COUNTS:.0f} counts 以上要る）。重いおもりで測り直してください"
        )
    return abs(counts) / grams


def describe(counts: float, grams: float, current: float | None) -> str:
    """校正の結果の要約（ログに出す）.

    Args:
        counts: 風袋からの差 [counts]（押す向きが正）
        grams: 載せた重さ [g]
        current: 今の ``counts_per_gram``（無ければ None）

    Raises:
        ValueError: :func:`counts_per_gram` が受けない値
    """
    value = counts_per_gram(counts, grams)
    parts = [f"{grams:g} g で {counts:+.0f} counts。counts_per_gram = {value:.2f}"]
    if current is not None:
        parts.append(
            f"今の値 {current:g} の {value / current:.3f} 倍"
            f"（今の値では {abs(counts) / current:.2f} g と読んでいた）"
        )
    if counts < 0:
        parts.append(
            "差が負（押す向きと逆の荷重）。おもりを押す向きと逆に掛けたなら正常。"
            "押す向きに掛けたなら sensor_orientation を見直す"
        )
    return "。".join(parts)
