"""ペーストディスペンサーの流量キャリブレーション."""

import statistics

import attrs


@attrs.frozen
class FlowCalibration:
    """流量キャリブレーション結果.

    N回転分のペーストを実測した質量と比重から `rotations_per_ul` を算出する。

    rotations_per_ul = rotations × specific_gravity / mass_mg

    導出:
        mass_g = mass_mg / 1000
        V_uL = mass_g / specific_gravity × 1000 = mass_mg / specific_gravity
        rotations_per_ul = rotations / V_uL = rotations × specific_gravity / mass_mg

    Attributes:
        rotations: 実行された回転数 [rev]
        mass_mg: 計測されたペーストの質量 [mg]
        specific_gravity: ペーストの比重（水比重, 無次元）
    """

    rotations: float
    mass_mg: float
    specific_gravity: float

    @property
    def rotations_per_ul(self) -> float:
        """1μLあたりの回転数 [rev/μL]."""
        return self.rotations * self.specific_gravity / self.mass_mg


@attrs.frozen
class FlowCalibrationSet:
    """複数回の流量計測を集約した結果.

    同一 rotations・同一 specific_gravity で N 回計測した質量から、質量の平均を
    用いて `rotations_per_ul` を 1 つ算出する。質量は直接計測量なので、非線形な
    `rotations_per_ul` を平均するより質量を平均する方が統計的に素直。

    Attributes:
        rotations: 各計測で実行された回転数 [rev]
        masses_mg: 各計測で得られたペーストの質量 [mg]（1 要素以上）
        specific_gravity: ペーストの比重（水比重, 無次元）
    """

    rotations: float
    masses_mg: tuple[float, ...] = attrs.field(
        converter=tuple, validator=attrs.validators.min_len(1)
    )
    specific_gravity: float

    @property
    def mean_mass_mg(self) -> float:
        """各計測質量の平均 [mg]."""
        return statistics.mean(self.masses_mg)

    @property
    def rotations_per_ul(self) -> float:
        """平均質量から算出した 1μLあたりの回転数 [rev/μL]."""
        return FlowCalibration(
            rotations=self.rotations,
            mass_mg=self.mean_mass_mg,
            specific_gravity=self.specific_gravity,
        ).rotations_per_ul

    @property
    def per_measurement(self) -> tuple[FlowCalibration, ...]:
        """各計測を単一の FlowCalibration として表したもの."""
        return tuple(
            FlowCalibration(
                rotations=self.rotations,
                mass_mg=mass,
                specific_gravity=self.specific_gravity,
            )
            for mass in self.masses_mg
        )

    @property
    def stdev_rotations_per_ul(self) -> float:
        """各計測 rotations_per_ul の標準偏差 [rev/μL]（計測 1 回なら 0.0）."""
        values = [c.rotations_per_ul for c in self.per_measurement]
        if len(values) < 2:
            return 0.0
        return statistics.stdev(values)
