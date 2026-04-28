"""ペーストディスペンサーの流量キャリブレーション."""

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
