"""流量キャリブレーションの数理（質量計測 → 係数、掃引の量計算、収束判定）.

HAL には触れない純粋な算出のみ。実際に線を引いて計量する手順は
:mod:`pcbasm.pasting.flowcalib.procedure` と web ジョブが担う。

キャリブ対象（依存順 ①→②→③）:
    ① ``rotations_per_ul`` — モーター回転 ↔ 吐出体積の係数（量の基準）
    ② ``max_dispense_rate`` — 吐出レート上限 [μL/sec]（効率の落ちで検出）
    ③ ``max_fill_speed`` — 連続塗布できる移動速度上限 [mm/sec]（目視選択）

線の塗布面積はスロット（stadium）近似で統一する（:func:`slot_area`）。
"""

import math
import statistics
from typing import Self

import attrs

from pcbasm.utils import is_finite_number

# ① 収束判定の相対許容（採用→再計測ループの収束ヒント表示用）
CONVERGENCE_REL_TOL = 0.02


@attrs.frozen
class FlowCalibration:
    """N 回転分のペーストを計量した質量と密度から ``rotations_per_ul`` を算出する.

    同一 ``rotations`` で複数回計測した場合は質量の平均を使う（質量は直接計測量なので、
    非線形な ``rotations_per_ul`` を平均するより素直）。

        volume_ul = mean_mass_mg / density_mg_per_ul
        rotations_per_ul = rotations / volume_ul

    Attributes:
        rotations: 各計測で実行した回転数 [rev]
        masses_mg: 各計測で得られたペースト質量 [mg]（1 要素以上）
        density_mg_per_ul: はんだペースト密度 [mg/μL]
    """

    rotations: float = attrs.field(validator=attrs.validators.gt(0.0))
    masses_mg: tuple[float, ...] = attrs.field(
        converter=tuple,
        validator=attrs.validators.and_(
            attrs.validators.min_len(1),
            attrs.validators.deep_iterable(attrs.validators.gt(0.0)),
        ),
    )
    density_mg_per_ul: float = attrs.field(validator=attrs.validators.gt(0.0))

    @property
    def mean_mass_mg(self) -> float:
        """各計測質量の平均 [mg]."""
        return statistics.mean(self.masses_mg)

    @property
    def volume_ul(self) -> float:
        """平均質量と密度から算出した体積 [μL]."""
        return self.mean_mass_mg / self.density_mg_per_ul

    @property
    def rotations_per_ul(self) -> float:
        """1μL あたりの回転数 [rev/μL]."""
        return self.rotations / self.volume_ul

    @property
    def stdev_rotations_per_ul(self) -> float:
        """各計測ごとの rotations_per_ul の標本標準偏差 [rev/μL]（計測 1 回なら 0.0）."""
        if len(self.masses_mg) < 2:
            return 0.0
        return statistics.stdev(
            self.rotations * self.density_mg_per_ul / mass for mass in self.masses_mg
        )

    def dispense_rate_for(self, rotation_rate: float) -> float:
        """回転速度 [rev/sec] を吐出レート [μL/sec] に変換する."""
        return rotation_rate / self.rotations_per_ul

    def dispense_accel_for(self, rotation_accel: float) -> float:
        """回転加速度 [rev/sec²] を吐出加速度 [μL/sec²] に変換する."""
        return rotation_accel / self.rotations_per_ul


@attrs.frozen
class MassFlowEstimate:
    """部分入力を許す質量キャリブレーションの見積り（導出不能の値は ``None``）.

    Attributes:
        volume_ul: 計測質量から算出した体積 [μL]
        rotations_per_ul: 1μL あたりの回転数 [rev/μL]
        max_dispense_rate: 回転速度を変換した吐出レート [μL/sec]
        dispense_accel: 回転加速度を変換した吐出加速度 [μL/sec²]
    """

    volume_ul: float | None
    rotations_per_ul: float | None
    max_dispense_rate: float | None
    dispense_accel: float | None

    @classmethod
    def estimate(
        cls,
        *,
        mass_mg: float,
        rotations: float,
        rate: float,
        accel: float,
        density_mg_per_ul: float,
    ) -> Self:
        """質量計測の部分入力からキャリブレーション値を見積もる.

        非正・非有限の入力から導出できない値は ``None`` を返す（エラーにしない）。
        算術は :class:`FlowCalibration` へ委譲し、確定値は小数第 6 位へ丸める。
        桁あふれや丸めにより有限の正値として表せない結果も ``None`` にする。

        Args:
            mass_mg: 計測されたペースト質量 [mg]
            rotations: キャリブレーションに使った実効回転数 [rev]
            rate: 回転速度 [rev/sec]
            accel: 回転加速度 [rev/sec²]
            density_mg_per_ul: はんだペースト密度 [mg/μL]
        """
        if not _positive_finite(mass_mg) or not _positive_finite(density_mg_per_ul):
            return cls(None, None, None, None)
        volume_ul = mass_mg / density_mg_per_ul
        if not _positive_finite(volume_ul):
            return cls(None, None, None, None)
        if not _positive_finite(rotations):
            return cls(_rounded_positive(volume_ul), None, None, None)
        calib = FlowCalibration(
            rotations=rotations,
            masses_mg=(mass_mg,),
            density_mg_per_ul=density_mg_per_ul,
        )
        rotations_per_ul = calib.rotations_per_ul
        if not _positive_finite(rotations_per_ul):
            return cls(_rounded_positive(volume_ul), None, None, None)
        return cls(
            volume_ul=_rounded_positive(volume_ul),
            rotations_per_ul=_rounded_positive(rotations_per_ul),
            max_dispense_rate=(
                _rounded_positive(calib.dispense_rate_for(rate))
                if _positive_finite(rate)
                else None
            ),
            dispense_accel=(
                _rounded_positive(calib.dispense_accel_for(accel))
                if _positive_finite(accel)
                else None
            ),
        )


def _positive_finite(value: float) -> bool:
    return is_finite_number(value) and value > 0


def _rounded_positive(value: float) -> float | None:
    if not _positive_finite(value):
        return None
    rounded = round(value, 6)
    return rounded if rounded > 0 else None


def slot_area(length: float, bead_width: float) -> float:
    """線の塗布面積をスロット（stadium）近似で算出する [mm²].

    長方形（``length × bead_width``）に両端の半円キャップ（合計 1 円）を加える。
    キャリブ線（~10mm）の端キャップ寄与（数%）を長方形近似で無視しないための統一式。

    Args:
        length: 線の長さ [mm]
        bead_width: ビード幅 [mm]（= ``nozzle_diameter × bead_width_factor``）
    """
    return length * bead_width + math.pi * (bead_width / 2.0) ** 2


def rate_sweep_amount_ul(rate: float, line_length: float, fill_speed: float) -> float:
    """② のレート掃引で指令レートを実際に出すための 1 線あたりの吐出量 [μL].

    ``FillSequence`` は移動速度を主設定とし、吐出レートを
    ``total_amount × fill_speed / 経路長`` で導出する（``rate_cap`` は頭打ちにしか
    働かない）。移動速度 ``fill_speed`` を変えずに指令レート ``rate`` を出すには
    吐出量を ``rate × line_length / fill_speed`` にする。

    Args:
        rate: 指令吐出レート [μL/sec]
        line_length: 線の長さ [mm]
        fill_speed: 線引きの移動速度 [mm/sec]
    """
    return rate * line_length / fill_speed


def speed_sweep_amount_ul(
    line_length: float, bead_width: float, ul_per_mm2: float
) -> float:
    """③ の速度掃引で 1 線に塗る実塗布同等の量 [μL]（面積換算）.

    Args:
        line_length: 線の長さ [mm]
        bead_width: ビード幅 [mm]
        ul_per_mm2: 単位面積あたりの塗布量 [μL/mm²]
    """
    return ul_per_mm2 * slot_area(line_length, bead_width)


def commanded_rotations(
    *, line_count: int, amount_ul: float, rotations_per_ul: float
) -> float:
    """① で ``line_count`` 本を ``amount_ul`` ずつ引くときに指令する回転数 [rev]."""
    return line_count * amount_ul * rotations_per_ul


@attrs.frozen
class RateMeasurement:
    """② の 1 レートでの計測.

    Attributes:
        rate: 指令吐出レート [μL/sec]
        measured_ul: 計量から換算した実吐出体積 [μL]（= mass / density）
        commanded_ul: 指令吐出体積 [μL]
    """

    rate: float = attrs.field(validator=attrs.validators.gt(0.0))
    measured_ul: float = attrs.field(validator=attrs.validators.ge(0.0))
    commanded_ul: float = attrs.field(validator=attrs.validators.gt(0.0))

    @property
    def efficiency(self) -> float:
        """吐出効率 ``measured_ul / commanded_ul``（無次元）."""
        return self.measured_ul / self.commanded_ul


@attrs.frozen
class DispenseRateCalibration:
    """② 吐出効率の落ち検出から ``max_dispense_rate`` を求める.

    各レートの計測（:class:`RateMeasurement`）を低レート→高レートの順に持つ。
    低レート側 ``baseline_count`` 点の efficiency 中央値を baseline とし、baseline から
    ``drop_frac`` を超えて初めて落ちたレートの**直前**のレートを上限とみなす。
    判定不能なら ``None``（raise しない）。

    Attributes:
        measurements: レート昇順の計測列
        drop_frac: baseline からの相対低下しきい値（既定 0.10 = 10%）
        baseline_count: baseline 中央値を取る低レート側の点数（既定 3）
    """

    measurements: tuple[RateMeasurement, ...] = attrs.field(converter=tuple)
    drop_frac: float = 0.10
    baseline_count: int = 3

    @property
    def efficiencies(self) -> tuple[float, ...]:
        """各レートの efficiency をレート昇順で並べたもの."""
        return tuple(m.efficiency for m in self.measurements)

    @property
    def baseline_efficiency(self) -> float | None:
        """低レート側 ``baseline_count`` 点の efficiency 中央値（計測が無ければ None）."""
        if not self.measurements:
            return None
        n = min(self.baseline_count, len(self.measurements))
        return statistics.median(self.efficiencies[:n])

    @property
    def max_dispense_rate(self) -> float | None:
        """効率の落ちを初めて示す直前のレート [μL/sec]（判定不能は None）.

        1 点目で既に落ちている場合（直前が無い）や、全域で落ちが無い場合は None。
        """
        baseline = self.baseline_efficiency
        if baseline is None:
            return None
        threshold = baseline * (1.0 - self.drop_frac)
        for i, eff in enumerate(self.efficiencies):
            if eff < threshold:
                return None if i == 0 else self.measurements[i - 1].rate
        return None


@attrs.frozen
class RotationsPerUlRound:
    """① 検証ループの 1 ラウンド（前後の rotations_per_ul）.

    検証前に使っていた値 ``previous`` で塗布・計量し、その結果から算出した
    新値 ``computed`` の相対差で収束を判定する。

    Attributes:
        previous: このラウンドで使った（検証前の）rotations_per_ul [rev/μL]
        computed: 計量から算出した新しい rotations_per_ul [rev/μL]
        rotations_used: このラウンドで指令した回転数 [rev]
    """

    previous: float = attrs.field(validator=attrs.validators.gt(0.0))
    computed: float = attrs.field(validator=attrs.validators.gt(0.0))
    rotations_used: float

    @classmethod
    def evaluate(
        cls,
        *,
        mass_mg: float,
        line_count: int,
        amount_ul: float,
        previous_rotations_per_ul: float,
        density_mg_per_ul: float,
    ) -> Self:
        """① の 1 ラウンド（``line_count`` 本 × ``amount_ul`` を引いて計量）を評価する.

        指令回転数 ``line_count × amount_ul × previous_rotations_per_ul`` と計量質量から
        新 ``rotations_per_ul`` を算出する。``dispense_accel`` は設定値のまま触らない。

        Args:
            mass_mg: 全線の合計質量 [mg]
            line_count: 引いた線の本数
            amount_ul: 1 線あたりの指令量 [μL]
            previous_rotations_per_ul: 線引きに使った rotations_per_ul [rev/μL]
            density_mg_per_ul: はんだペースト密度 [mg/μL]
        """
        rotations_used = commanded_rotations(
            line_count=line_count,
            amount_ul=amount_ul,
            rotations_per_ul=previous_rotations_per_ul,
        )
        flow = FlowCalibration(
            rotations=rotations_used,
            masses_mg=(mass_mg,),
            density_mg_per_ul=density_mg_per_ul,
        )
        return cls(
            previous=previous_rotations_per_ul,
            computed=flow.rotations_per_ul,
            rotations_used=rotations_used,
        )

    @property
    def relative_change(self) -> float:
        """``|computed - previous| / previous``（無次元）."""
        return abs(self.computed - self.previous) / self.previous

    def converged(self, rel_tol: float = CONVERGENCE_REL_TOL) -> bool:
        """前後の rotations_per_ul が相対許容 ``rel_tol`` 内かを判定する."""
        return self.relative_change <= rel_tol
