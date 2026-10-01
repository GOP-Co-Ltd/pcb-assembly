"""直径 → 体積の 3 次モデルと、その最小二乗フィット.

``V = a·d³ + b·d² + c·d`` の 3 項で、**定数項を持たない**。blank セル（直径 0）の
真値 0 を厳密に表す必要があるため。切片列を設計行列へ入れないので ``V(0) = 0`` は
構造的に保証される。

3 項すべてが要る。直径と体積は本来 3 次の関係だが、実測では純 3 次（``a·d³`` のみ）
では誤差が 0.124 に留まり、3 項を使うと 0.087 まで下がる。塗布点は球ではなく板に
潰れた形なので、高さと直径の比が量によって変わることを 2 次・1 次の項が吸収する。

**被覆域で単調非減少であることを保証する。** 「直径が大きいほど体積が大きい」は物理
そのものであり、崩れると中央値集約が「view ごとに推定して集約する」ことと一致しなく
なる（:mod:`pcbasm.pasting.paste_volume.aggregate`）。最小二乗そのものはこれを守らない
ので、フィットは求めた係数を厳密に検査し、崩れていれば係数が非負の純 3 次モデルへ切り替える。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs
import numpy as np

from pcbasm.utils import is_finite_number

# 校正ファイルへ記録するモデル形式の識別子
MODEL_KIND = "cubic_through_origin"

# 運転時補正で採用する直径範囲を決める、被覆域の下端側から除外する割合。
# 同条件で採った 2 つの校正は大径側で 3 % しか違わないのに、被覆域の下端では 58 %
# 食い違う（docs/paste-volume-diameter-calibration.md「被覆域の下端は信頼できる範囲
# から外す」）。被覆域が比 1.9 倍しかないところへ [d³, d², d] の 3 自由度を当てている
# ので、曲線が一致しても係数が個別に定まらないため。
#
# 絶対値 [mm] ではなく被覆域の幅に対する割合にしてある。食い違いの原因は共線性で、
# それは被覆域の広さで決まる。広く採れた校正ほど係数が定まるので、下限も相対的に
# 下がってよい。実測の 2 校正（幅 0.51 / 0.54 mm）では下限が 0.79 / 0.83 mm となり、
# そこでの食い違いは 10 % 程度。
RELIABLE_RANGE_MARGIN = 0.35


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

    @property
    def reliable_diameter_min_mm(self) -> float:
        """運転時補正で採用する直径の下限 [mm]（被覆域の下端にマージンを取る）.

        被覆域の下端付近は係数が同定されず、校正どうしで数十 % 食い違う。

        補正の材料にはこの下限より大きい直径だけを使う。
        """
        width = self.diameter_max_mm - self.diameter_min_mm
        return self.diameter_min_mm + RELIABLE_RANGE_MARGIN * width

    def covers_reliably(self, diameter_mm: float) -> bool:
        """運転時補正の材料にしてよい直径か（上限は被覆域のまま）."""
        if not is_finite_number(diameter_mm):
            return False
        return self.reliable_diameter_min_mm <= diameter_mm <= self.diameter_max_mm

    def is_monotonic_in_range(self) -> bool:
        """被覆域内で単調非減少か（導関数の最小値が 0 以上か）.

        中央値集約が「1 view ごとの推定を集約する」ことと一致するための前提。
        :func:`fit_cubic_through_origin` はこれが成り立つ係数だけを返す。

        標本点で確かめると点と点の間の落ち込みを見逃すので、導関数の最小値を
        閉じた式で求める。
        """
        return (
            _quadratic_min_in_range(
                3.0 * self.cubic_ul_per_mm3,
                2.0 * self.quadratic_ul_per_mm2,
                self.linear_ul_per_mm,
                self.diameter_min_mm,
                self.diameter_max_mm,
            )
            >= 0.0
        )

    def is_positive_in_range(self) -> bool:
        """被覆域内で推定体積が常に正か（負の体積は物理的にありえない）.

        ``V(d) = d·(a·d² + b·d + c)`` で被覆域の直径は正なので、符号は括弧内の
        2 次式だけで決まる。単調性と同じく閉じた式で厳密に判定する。
        """
        if self.diameter_min_mm <= 0.0:
            return False  # V(0) = 0 は正ではない
        return (
            _quadratic_min_in_range(
                self.cubic_ul_per_mm3,
                self.quadratic_ul_per_mm2,
                self.linear_ul_per_mm,
                self.diameter_min_mm,
                self.diameter_max_mm,
            )
            > 0.0
        )

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

    求めた係数が被覆域で単調でなければ、係数が非負の純 3 次モデルへ切り替えて単調性を保証する
    （:func:`_monotonic_cubic`）。実測 8 session・LOSO 56 通りでは一度も発生しない。

    切り替えは連続的に起きず、切り替わると 2 次・1 次の項を丸ごと失う。導関数の最小値が
    0 のすぐ上にあるフィットは、丸め誤差の符号ひとつで切り替え側へ移る。実データの余裕は大きい。

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
    if not model.is_monotonic_in_range():
        model = _monotonic_cubic(diameters, volumes)
    error = model.validate()
    if error is not None:
        return None, error
    if not model.is_positive_in_range():
        return None, (
            "被覆域の下端で推定体積が0以下になる係数が求まりました"
            "（点が偏っています）"
        )
    return model, None


def _monotonic_cubic(diameters: np.ndarray, volumes: np.ndarray) -> CubicVolumeModel:
    """単調性が崩れたときの切り替え先となる純 3 次モデル ``V = a·d³``.

    直径が正・体積が非負であることは呼び出し前に検証済みなので ``a >= 0`` が構造的に
    決まり、``V'(d) = 3a·d² >= 0`` がすべての直径で成り立つ。2 次・1 次の項を持たせた
    まま単調性を課すには制約付き求解が要るが、非負最小二乗を実測 8 session で試すと
    どの session でも 2 次・1 次の係数が 0 になって純 3 次と一致した。切り替え先を純 3 次
    に固定すれば、求解器を持ち込まずに同じ結果が得られる。
    """
    cube = diameters**3
    return CubicVolumeModel(
        cubic_ul_per_mm3=float((cube * volumes).sum() / (cube * cube).sum()),
        quadratic_ul_per_mm2=0.0,
        linear_ul_per_mm=0.0,
        diameter_min_mm=float(diameters.min()),
        diameter_max_mm=float(diameters.max()),
    )


def _quadratic_min_in_range(
    quadratic: float, linear: float, constant: float, low: float, high: float
) -> float:
    """区間 ``[low, high]`` における 2 次式の最小値（厳密）.

    2 次式の最小は端点か頂点のいずれか。下に凸（``quadratic > 0``）で頂点が区間の
    内側にあるときだけ頂点を見れば足りる。上に凸と 1 次の最小は必ず端点。

    Args:
        quadratic: 2 次の係数
        linear: 1 次の係数
        constant: 定数項
        low: 区間の下限
        high: 区間の上限
    """

    def value(x: float) -> float:
        return quadratic * x**2 + linear * x + constant

    candidates = [value(low), value(high)]
    if quadratic > 0.0:
        vertex = -linear / (2.0 * quadratic)
        if low < vertex < high:
            candidates.append(value(vertex))
    return min(candidates)
