"""吐出量キャリブレーションの算出モデルと段ずらし幾何.

実塗布（銅板に線を引く）を通して 3 つのキャリブ値を検証ループまで回すための、
**純粋な算出ロジックと幾何**のみを提供する。HAL（カメラ/Klipper/applicator 等）は
一切触らない。実際に線を引いて計量する動作は webui 側のジョブが担う。

キャリブ対象（依存順 ①→②→③）:
    ① ``rotations_per_ul`` — モーター回転 ↔ 吐出体積の係数（量の基準）
    ② ``max_dispense_rate`` — 吐出レート上限 [μL/sec]（効率の落ちで検出）
    ③ ``max_fill_speed`` — 連続塗布できる移動速度上限 [mm/sec]（目視選択）

線の塗布面積はスロット（stadium）近似で統一する（``slot_area`` 参照）。
"""

import math
import statistics

import attrs

from pcbasm.geometry import Point2d


def slot_area(length: float, bead_width: float) -> float:
    """線の塗布面積をスロット（stadium）近似で算出する [mm²].

    長方形（``length × bead_width``）に両端の半円キャップ
    （``π·(bead_width/2)²`` 合計で1円）を加える。キャリブ線（~10mm）の
    端キャップ寄与（数%）を長方形近似で落とさないための統一式。

    Args:
        length: 線の長さ [mm]
        bead_width: ビード幅 [mm]（= ``nozzle_diameter × bead_width_factor``）

    Returns:
        スロット近似面積 [mm²]
    """
    return length * bead_width + math.pi * (bead_width / 2.0) ** 2


@attrs.frozen
class LineLayout:
    """段ずらし n 本の線を並べる純粋幾何.

    掃除不要のため、各線を行方向（Y）にずらして重ならないよう並べる。
    HAL 非依存で、各線の始点・終点座標のみを返す。

    Attributes:
        line_length: 各線の長さ [mm]（X 方向に伸びる）
        line_count: 線の本数（1 以上）
        row_pitch: 隣接する線の Y 方向間隔 [mm]
        origin: 1 本目の始点座標（既定は原点）
    """

    line_length: float = attrs.field(validator=attrs.validators.gt(0.0))
    line_count: int = attrs.field(validator=attrs.validators.ge(1))
    row_pitch: float = attrs.field(validator=attrs.validators.gt(0.0))
    origin: Point2d = attrs.field(factory=lambda: Point2d(0.0, 0.0))

    def line(self, index: int) -> tuple[Point2d, Point2d]:
        """Index 番目（0 始まり）の線の (始点, 終点) を返す.

        各線は X 方向に ``line_length`` 伸び、Y 方向に ``index × row_pitch``
        だけずれる。範囲外の index は IndexError。
        """
        if not 0 <= index < self.line_count:
            raise IndexError(index)
        y = self.origin.y + index * self.row_pitch
        start = Point2d(self.origin.x, y)
        end = Point2d(self.origin.x + self.line_length, y)
        return start, end

    @property
    def lines(self) -> tuple[tuple[Point2d, Point2d], ...]:
        """全線の (始点, 終点) を段ずらし順に並べたもの."""
        return tuple(self.line(i) for i in range(self.line_count))

    @property
    def span(self) -> float:
        """全線を含む Y 方向の総幅 [mm]（1 本なら 0.0）."""
        return (self.line_count - 1) * self.row_pitch


def dispense_rate_schedule(
    rate_min: float, rate_max: float, divisions: int
) -> list[float]:
    """② 用の吐出レート列 [μL/sec] を生成する.

    ``rate_min`` から ``rate_max`` までを ``divisions`` 点に等間隔分割する。
    効率の落ち（律速点）を低レート側から探すため、昇順で返す。

    Args:
        rate_min: 最小吐出レート [μL/sec]（0 より大きい）
        rate_max: 最大吐出レート [μL/sec]（``rate_min`` より大きい）
        divisions: 生成する点数（1 以上）

    Returns:
        昇順の吐出レート列 [μL/sec]。入力不正なら空リスト。
    """
    if divisions < 1 or rate_min <= 0.0 or rate_max < rate_min:
        return []
    return _linspace(rate_min, rate_max, divisions)


def fill_speed_schedule(
    speed_min: float, speed_max: float, divisions: int
) -> list[float]:
    """③ 用の等間隔速度列 [mm/sec] を生成する.

    ``speed_min`` から ``speed_max`` までを ``divisions`` 点に等間隔分割する。

    Args:
        speed_min: 最小塗布速度 [mm/sec]（0 より大きい）
        speed_max: 最大塗布速度 [mm/sec]（``speed_min`` より大きい）
        divisions: 生成する点数（1 以上）

    Returns:
        昇順の速度列 [mm/sec]。入力不正なら空リスト。
    """
    if divisions < 1 or speed_min <= 0.0 or speed_max < speed_min:
        return []
    return _linspace(speed_min, speed_max, divisions)


def _linspace(start: float, stop: float, count: int) -> list[float]:
    """start..stop を count 点に等間隔分割した列（端点含む）."""
    if count == 1:
        return [start]
    step = (stop - start) / (count - 1)
    return [start + step * i for i in range(count)]


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

    各レートの計測（``RateMeasurement``）を低レート→高レートの順に持つ。
    低レート側 ``baseline_count`` 点の efficiency 中央値を baseline とし、
    baseline から ``drop_frac`` を超えて初めて落ちたレートの**直前**のレートを
    上限とみなす（その直前までは効率を保てていた、の意）。判定不能なら None。

    Attributes:
        measurements: レート昇順の計測列（1 要素以上）
        drop_frac: baseline からの相対低下しきい値（既定 0.10 = 10%）
        baseline_count: baseline 中央値を取る低レート側の点数（既定 3）
    """

    measurements: tuple[RateMeasurement, ...] = attrs.field(
        converter=tuple, validator=attrs.validators.min_len(1)
    )
    drop_frac: float = attrs.field(
        default=0.10,
        validator=attrs.validators.and_(
            attrs.validators.gt(0.0), attrs.validators.lt(1.0)
        ),
    )
    baseline_count: int = attrs.field(default=3, validator=attrs.validators.ge(1))

    @property
    def efficiencies(self) -> tuple[float, ...]:
        """各レートの efficiency をレート昇順で並べたもの."""
        return tuple(m.efficiency for m in self.measurements)

    @property
    def baseline_efficiency(self) -> float:
        """低レート側 ``baseline_count`` 点の efficiency 中央値."""
        n = min(self.baseline_count, len(self.measurements))
        return statistics.median(self.efficiencies[:n])

    @property
    def max_dispense_rate(self) -> float | None:
        """効率の落ちを初めて示す直前のレート [μL/sec]（判定不能は None）.

        baseline よりも ``drop_frac`` を超えて低い efficiency を最初に示す点を
        「落ちた点」とみなし、その 1 つ手前のレートを返す。1 点目で既に落ちている
        場合（直前が無い）や、全域で落ちが無い場合は None。
        """
        threshold = self.baseline_efficiency * (1.0 - self.drop_frac)
        for i, eff in enumerate(self.efficiencies):
            if eff < threshold:
                if i == 0:
                    return None
                return self.measurements[i - 1].rate
        return None


@attrs.frozen
class FillSpeedSweep:
    """③ 速度列と選択番号の対応.

    ``fill_speed_schedule`` で生成した速度列を保持し、目視選択した番号から
    ``max_fill_speed`` を引く。

    Attributes:
        speeds: 速度列 [mm/sec]（1 要素以上、通常は昇順）
    """

    speeds: tuple[float, ...] = attrs.field(
        converter=tuple, validator=attrs.validators.min_len(1)
    )

    def speed_at(self, index: int) -> float | None:
        """選択番号（0 始まり）に対応する速度 [mm/sec]（範囲外は None）."""
        if not 0 <= index < len(self.speeds):
            return None
        return self.speeds[index]


@attrs.frozen
class RotationsPerUlRound:
    """① 検証ループの 1 ラウンド（前後の rotations_per_ul）.

    検証前に使っていた値 ``previous`` で塗布・計量し、その結果から算出した
    新値 ``computed`` の相対差で収束を判定する。

    Attributes:
        previous: このラウンドで使った（検証前の）rotations_per_ul [rev/μL]
        computed: 計量から算出した新しい rotations_per_ul [rev/μL]
    """

    previous: float = attrs.field(validator=attrs.validators.gt(0.0))
    computed: float = attrs.field(validator=attrs.validators.gt(0.0))

    @property
    def relative_change(self) -> float:
        """``|computed - previous| / previous``（無次元）."""
        return abs(self.computed - self.previous) / self.previous

    def converged(self, rel_tol: float) -> bool:
        """前後の rotations_per_ul が相対許容 ``rel_tol`` 内かを判定する."""
        return self.relative_change <= rel_tol
