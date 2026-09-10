"""直径 → 体積の 3 次モデルと、その最小二乗フィット.

``V = a·d³ + b·d² + c·d`` の 3 項で、**定数項を持たない**。blank セル（直径 0）の
真値 0 を厳密に表す必要があるため。切片列を設計行列へ入れないので ``V(0) = 0`` は
構造的に保証される。

3 項すべてが要る。直径と体積は本来 3 次の関係だが、実測では純 3 次（``a·d³`` のみ）
では誤差が 0.124 に留まり、3 項を使うと 0.087 まで下がる。塗布点は球ではなく板に
潰れた形なので、高さと直径の比が量によって変わることを 2 次・1 次の項が吸収する。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs
import numpy as np

from pcbasm.utils import is_finite_number

# 校正ファイルへ記録するモデル形式の識別子
MODEL_KIND = "cubic_through_origin"

# 被覆域内の単調性・正値性を確かめる分割数
_RANGE_SAMPLES = 64


@attrs.frozen
class CubicVolumeModel:
    """切片 0 固定の 3 次モデル.

    Attributes:
        cubic_ul_per_mm3: 3 次の係数 a [µL/mm³]
        quadratic_ul_per_mm2: 2 次の係数 b [µL/mm²]
        linear_ul_per_mm: 1 次の係数 c [µL/mm]
        diameter_min_mm: フィットが被覆する正の直径の下限 [mm]
        diameter_max_mm: 同じく上限 [mm]
    """

    cubic_ul_per_mm3: float
    quadratic_ul_per_mm2: float
    linear_ul_per_mm: float
    diameter_min_mm: float
    diameter_max_mm: float

    def volume_ul(self, diameter_mm: float) -> float:
        """直径 [mm] から体積 [µL] を求める（0 以下の直径は 0.0）."""
        if not is_finite_number(diameter_mm) or diameter_mm <= 0:
            return 0.0
        return (
            self.cubic_ul_per_mm3 * diameter_mm**3
            + self.quadratic_ul_per_mm2 * diameter_mm**2
            + self.linear_ul_per_mm * diameter_mm
        )

    def covers(self, diameter_mm: float) -> bool:
        """フィットが被覆する直径範囲に入るか."""
        if not is_finite_number(diameter_mm):
            return False
        return self.diameter_min_mm <= diameter_mm <= self.diameter_max_mm

    def is_monotonic_in_range(self) -> bool:
        """被覆域内で単調増加か.

        中央値集約が「1 view ごとの推定を集約する」ことと一致するための前提。 崩れていたら診断へ残して運転者に見せる。
        """
        volumes = [self.volume_ul(value) for value in _range_samples(self)]
        return all(later >= earlier for earlier, later in zip(volumes, volumes[1:]))

    def is_positive_in_range(self) -> bool:
        """被覆域内で推定体積が常に正か（負の体積は物理的にありえない）."""
        return all(self.volume_ul(value) > 0.0 for value in _range_samples(self))

    def validate(self) -> str | None:
        """係数と被覆域を検証する（不正なら理由文）."""
        for name, value in (
            ("3次の係数", self.cubic_ul_per_mm3),
            ("2次の係数", self.quadratic_ul_per_mm2),
            ("1次の係数", self.linear_ul_per_mm),
        ):
            if not is_finite_number(value):
                return f"{name}は有限値が必要です: {value!r}"
        for name, value in (
            ("被覆域の下限", self.diameter_min_mm),
            ("被覆域の上限", self.diameter_max_mm),
        ):
            if not is_finite_number(value) or value < 0:
                return f"{name}は0以上の有限値が必要です: {value!r}"
        if self.diameter_max_mm < self.diameter_min_mm:
            return (
                "被覆域の上限は下限以上が必要です: "
                f"{self.diameter_max_mm!r} < {self.diameter_min_mm!r}"
            )
        return None


def fit_cubic_through_origin(
    diameters_mm: Sequence[float], volumes_ul: Sequence[float]
) -> tuple[CubicVolumeModel | None, str | None]:
    """直径と体積の組から切片 0 固定の 3 次モデルを最小二乗で求める.

    直径 0 の組（blank）は設計行列上ゼロ行なので係数へ寄与しない。被覆域も動かさない
    よう、範囲は正の直径だけから決める。

    Args:
        diameters_mm: 各 sample の直径 [mm]
        volumes_ul: 各 sample の教師体積 [µL]

    Returns:
        ``(モデル, None)`` または ``(None, 理由)``
    """
    if len(diameters_mm) != len(volumes_ul):
        return None, (
            f"直径と体積の個数が違います: {len(diameters_mm)} != {len(volumes_ul)}"
        )
    for name, values in (("直径", diameters_mm), ("体積", volumes_ul)):
        for value in values:
            if not is_finite_number(value):
                return None, f"{name}は有限値が必要です: {value!r}"
            if value < 0:
                return None, f"{name}は0以上が必要です: {value!r}"

    positive = [
        (float(diameter), float(volume))
        for diameter, volume in zip(diameters_mm, volumes_ul, strict=True)
        if diameter > 0
    ]
    if len(positive) < 3:
        return None, (f"3次のフィットには正の直径が3点以上必要です: {len(positive)}点")
    diameters = np.array([item[0] for item in positive], dtype=np.float64)
    volumes = np.array([item[1] for item in positive], dtype=np.float64)
    if float(diameters.max() - diameters.min()) <= 0.0:
        return None, f"直径がすべて同じ値です: {diameters[0]!r}"

    design = np.stack([diameters**3, diameters**2, diameters], axis=1)
    coefficients, *_ = np.linalg.lstsq(design, volumes, rcond=None)
    if not np.isfinite(coefficients).all():
        return None, "フィットが収束しませんでした（係数が有限値になりません）"

    model = CubicVolumeModel(
        cubic_ul_per_mm3=float(coefficients[0]),
        quadratic_ul_per_mm2=float(coefficients[1]),
        linear_ul_per_mm=float(coefficients[2]),
        diameter_min_mm=float(diameters.min()),
        diameter_max_mm=float(diameters.max()),
    )
    error = model.validate()
    if error is not None:
        return None, error
    if not model.is_positive_in_range():
        return None, (
            "被覆域内で推定体積が0以下になる係数が求まりました"
            "（データが単調でないか、点が偏っています）"
        )
    return model, None


def _range_samples(model: CubicVolumeModel) -> list[float]:
    """被覆域を等分した直径列（単調性・正値性の確認に使う）."""
    span = model.diameter_max_mm - model.diameter_min_mm
    if span <= 0:
        return [model.diameter_min_mm]
    step = span / (_RANGE_SAMPLES - 1)
    return [model.diameter_min_mm + step * index for index in range(_RANGE_SAMPLES)]
