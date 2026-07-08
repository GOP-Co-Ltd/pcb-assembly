"""ペーストディスペンサーの流量キャリブレーション."""

import statistics

import attrs


@attrs.frozen
class MassFlowCalibration:
    """質量計測から初期 `rotations_per_ul` を算出する結果.

    Args:
        rotations: キャリブレーションに使う実効回転数 [rev]
        mass_mg: 計測されたペースト質量 [mg]
        density_mg_per_ul: はんだペースト密度 [mg/μL]
    """

    rotations: float = attrs.field(validator=attrs.validators.gt(0.0))
    mass_mg: float = attrs.field(validator=attrs.validators.gt(0.0))
    density_mg_per_ul: float = attrs.field(validator=attrs.validators.gt(0.0))

    @property
    def volume_ul(self) -> float:
        """質量と密度から算出した体積 [μL]."""
        return self.mass_mg / self.density_mg_per_ul

    @property
    def rotations_per_ul(self) -> float:
        """1μLあたりの回転数 [rev/μL]."""
        return self.rotations / self.volume_ul

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
        rotations_per_ul: 1μLあたりの回転数 [rev/μL]
        max_dispense_rate: 回転速度を変換した吐出レート [μL/sec]
        dispense_accel: 回転加速度を変換した吐出加速度 [μL/sec²]
    """

    volume_ul: float | None
    rotations_per_ul: float | None
    max_dispense_rate: float | None
    dispense_accel: float | None


def estimate_mass_flow(
    *,
    mass_mg: float,
    rotations: float,
    rate: float,
    accel: float,
    density_mg_per_ul: float,
) -> MassFlowEstimate:
    """質量計測の部分入力からキャリブレーション値を見積もる.

    非正の入力から導出できない値は ``None`` を返す（エラーにしない）。算術は
    :class:`MassFlowCalibration` へ委譲し（単一ソース維持）、確定値は
    小数第 6 位へ丸めて返す。

    Args:
        mass_mg: 計測されたペースト質量 [mg]
        rotations: キャリブレーションに使った実効回転数 [rev]
        rate: 回転速度 [rev/sec]
        accel: 回転加速度 [rev/sec²]
        density_mg_per_ul: はんだペースト密度 [mg/μL]
    """
    volume_ul = (
        mass_mg / density_mg_per_ul if mass_mg > 0 and density_mg_per_ul > 0 else None
    )
    if volume_ul is None or rotations <= 0:
        return MassFlowEstimate(
            volume_ul=_rounded(volume_ul),
            rotations_per_ul=None,
            max_dispense_rate=None,
            dispense_accel=None,
        )
    calib = MassFlowCalibration(
        rotations=rotations, mass_mg=mass_mg, density_mg_per_ul=density_mg_per_ul
    )
    return MassFlowEstimate(
        volume_ul=_rounded(volume_ul),
        rotations_per_ul=_rounded(calib.rotations_per_ul),
        max_dispense_rate=(
            _rounded(calib.dispense_rate_for(rate)) if rate > 0 else None
        ),
        dispense_accel=(
            _rounded(calib.dispense_accel_for(accel)) if accel > 0 else None
        ),
    )


def _rounded(value: float | None, digits: int = 6) -> float | None:
    """確定値を丸める（``None`` はそのまま）."""
    return None if value is None else round(value, digits)


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

    def dispense_rate_for(self, rotation_rate: float) -> float:
        """回転速度 [rev/sec] を吐出レート [μL/sec] に変換する."""
        return rotation_rate / self.rotations_per_ul

    def dispense_accel_for(self, rotation_accel: float) -> float:
        """回転加速度 [rev/sec²] を吐出加速度 [μL/sec²] に変換する."""
        return rotation_accel / self.rotations_per_ul

    def rescaled_dispense_accel(
        self, previous_dispense_accel: float, previous_rotations_per_ul: float
    ) -> float:
        """rotations_per_ul 変更後も回転加速度を保つ dispense_accel を再算出する.

        旧 ``previous_dispense_accel`` [μL/sec²] が表す回転加速度 [rev/sec²] を旧
        ``previous_rotations_per_ul`` で逆算し、この計測の新 ``rotations_per_ul``
        で吐出加速度へ再変換する。① 検証ループで新 rpu を採用する際に使う。

        Args:
            previous_dispense_accel: 採用前の吐出加速度 [μL/sec²]
            previous_rotations_per_ul: 採用前の rotations_per_ul [rev/μL]

        Returns:
            新 rotations_per_ul に対応する吐出加速度 [μL/sec²]
        """
        rotation_accel = previous_dispense_accel * previous_rotations_per_ul
        return self.dispense_accel_for(rotation_accel)

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
