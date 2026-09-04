"""銅板上の線配置（段ずらし・折り返し）と掃引点の計画.

HAL 非依存の純粋幾何。各線の始点・終点は基板左上原点の board 座標 [mm] で返す。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import attrs

from pcbasm.geometry import Point2d
from pcbasm.pasting.flowcalib.flow import (
    rate_sweep_amount_ul,
    speed_sweep_amount_ul,
    sweep_schedule,
)

if TYPE_CHECKING:
    from pcbasm.pasting.flowcalib.params import CalibrationParams

# 「ちょうど収まる」寸法が浮動小数点誤差で 1 行/1 列失われないための微小許容
_GRID_EPSILON = 1e-9


@attrs.frozen
class LineLayout:
    """基板内に n 本の線を段ずらし・折り返しで並べる純粋幾何.

    各線は X 方向に ``line_length`` 伸び、Y 方向へ ``row_pitch`` 間隔で並ぶ。
    描画領域（``margin`` を除いた基板内側）の下端に達したら右隣の列
    （X を ``line_length + row_pitch`` ずらす）へ折り返す。全 ``line_count`` 本が
    描画領域に収まるかは :attr:`fits` / :func:`validate_line_layout` で確認する
    （収まらない配置でも構築はできる。呼び出し側が先に検証する）。

    Attributes:
        line_length: 各線の長さ [mm]（X 方向に伸びる）
        line_count: 線の本数（1 以上）
        row_pitch: 隣接する線の Y 方向間隔 [mm]
        board_width: 基板の幅 [mm]（X 方向）
        board_height: 基板の高さ [mm]（Y 方向）
        margin: 基板端から描画領域までのマージン [mm]（全周、既定 0）
    """

    line_length: float = attrs.field(validator=attrs.validators.gt(0.0))
    line_count: int = attrs.field(validator=attrs.validators.ge(1))
    row_pitch: float = attrs.field(validator=attrs.validators.gt(0.0))
    board_width: float = attrs.field(validator=attrs.validators.gt(0.0))
    board_height: float = attrs.field(validator=attrs.validators.gt(0.0))
    margin: float = attrs.field(default=0.0, validator=attrs.validators.ge(0.0))

    @property
    def rows_per_column(self) -> int:
        """1 列に収まる線の本数（描画領域の高さから算出、収まらなければ 0）."""
        usable_height = self.board_height - 2.0 * self.margin
        if usable_height < 0.0:
            return 0
        return int(usable_height / self.row_pitch + _GRID_EPSILON) + 1

    @property
    def column_pitch(self) -> float:
        """隣接する列の X 方向間隔 [mm]（線の長さ + 段ずらし間隔）."""
        return self.line_length + self.row_pitch

    @property
    def max_columns(self) -> int:
        """描画領域に収まる列数（1 本も収まらなければ 0）."""
        usable_width = self.board_width - 2.0 * self.margin
        if usable_width + _GRID_EPSILON < self.line_length:
            return 0
        return (
            int((usable_width - self.line_length) / self.column_pitch + _GRID_EPSILON)
            + 1
        )

    @property
    def capacity(self) -> int:
        """折り返しを使って描画領域に収まる線の最大本数."""
        return self.rows_per_column * self.max_columns

    @property
    def fits(self) -> bool:
        """全 ``line_count`` 本が描画領域に収まるか."""
        return self.line_count <= self.capacity

    def line(self, index: int) -> tuple[Point2d, Point2d]:
        """Index 番目（0 始まり）の線の (始点, 終点) を返す.

        線は列内を上から下（Y+）へ埋め、列が尽きたら右隣の列の先頭へ折り返す。 範囲外の index は IndexError。
        """
        if not 0 <= index < self.line_count:
            raise IndexError(index)
        column, row = divmod(index, self.rows_per_column)
        x = self.margin + column * self.column_pitch
        y = self.margin + row * self.row_pitch
        return Point2d(x, y), Point2d(x + self.line_length, y)

    @property
    def lines(self) -> tuple[tuple[Point2d, Point2d], ...]:
        """全線の (始点, 終点) を段ずらし順に並べたもの."""
        return tuple(self.line(i) for i in range(self.line_count))


def validate_line_layout(layout: LineLayout) -> str | None:
    """線が描画領域に収まらなければ、ユーザーに調整を促す文言を返す."""
    if layout.fits:
        return None
    return (
        f"線 {layout.line_count} 本は折り返しても銅板の描画領域に収まりません"
        f"（最大 {layout.capacity} 本）。線の本数/分割数・線の長さ・段ずらし間隔を"
        "調整してください"
    )


@attrs.frozen
class RateSweepPoint:
    """② レート掃引の 1 点（専用の線位置付き）.

    Attributes:
        index: 掃引順（0 始まり）
        rate: 指令吐出レート [μL/sec]
        amount_ul: 移動速度を固定したまま ``rate`` を出すための吐出量 [μL]
        start: 線の始点（board 座標）
        end: 線の終点（board 座標）
    """

    index: int
    rate: float
    amount_ul: float
    start: Point2d
    end: Point2d


def plan_rate_sweep(
    params: CalibrationParams, *, fill_speed: float
) -> tuple[tuple[RateSweepPoint, ...] | None, str | None]:
    """② のレート列と線位置を計画する（掃引点ごとに専用の線位置を確保する）.

    Args:
        params: 掃引範囲・線設定・銅板寸法
        fill_speed: 固定する移動速度 [mm/sec]（通常 ``max_fill_speed``）

    Returns:
        ``(points, None)`` または ``(None, 理由)``（レート列が空 / 線が収まらない）
    """
    rates = sweep_schedule(params.rate_min, params.rate_max, params.rate_divisions)
    if not rates:
        return (
            None,
            "吐出レート列が生成できません（rate_min / rate_max / divisions を確認）",
        )
    layout = params.line_layout(len(rates))
    if (message := validate_line_layout(layout)) is not None:
        return None, message
    return (
        tuple(
            RateSweepPoint(
                index=index,
                rate=rate,
                amount_ul=rate_sweep_amount_ul(rate, params.line_length, fill_speed),
                start=start,
                end=end,
            )
            for index, (rate, (start, end)) in enumerate(
                zip(rates, layout.lines, strict=True)
            )
        ),
        None,
    )


@attrs.frozen
class SpeedSweepPoint:
    """③ 速度掃引の 1 点（専用の線位置付き）.

    Attributes:
        index: 掃引順（0 始まり。目視選択の番号）
        fill_speed: 塗布移動速度 [mm/sec]
        amount_ul: 1 線の吐出量 [μL]（全点で同一）
        start: 線の始点（board 座標）
        end: 線の終点（board 座標）
    """

    index: int
    fill_speed: float
    amount_ul: float
    start: Point2d
    end: Point2d


@attrs.frozen
class SpeedSweep:
    """③ 速度掃引の計画.

    Attributes:
        points: 速度昇順の掃引点
        total_amount_ul: 各線に塗る実塗布同等の量 [μL]
    """

    points: tuple[SpeedSweepPoint, ...]
    total_amount_ul: float


def plan_speed_sweep(
    params: CalibrationParams, *, ul_per_mm2: float, bead_width: float
) -> tuple[SpeedSweep | None, str | None]:
    """③ の速度列と線位置を計画する（各線は実塗布同等の総量で固定）.

    Args:
        params: 掃引範囲・線設定・銅板寸法
        ul_per_mm2: 単位面積あたりの塗布量 [μL/mm²]
        bead_width: ビード幅 [mm]

    Returns:
        ``(sweep, None)`` または ``(None, 理由)``（速度列が空 / 線が収まらない）
    """
    speeds = sweep_schedule(params.speed_min, params.speed_max, params.speed_divisions)
    if not speeds:
        return (
            None,
            "塗布速度列が生成できません（speed_min / speed_max / divisions を確認）",
        )
    layout = params.line_layout(len(speeds))
    if (message := validate_line_layout(layout)) is not None:
        return None, message
    amount_ul = speed_sweep_amount_ul(params.line_length, bead_width, ul_per_mm2)
    return (
        SpeedSweep(
            points=tuple(
                SpeedSweepPoint(
                    index=index,
                    fill_speed=speed,
                    amount_ul=amount_ul,
                    start=start,
                    end=end,
                )
                for index, (speed, (start, end)) in enumerate(
                    zip(speeds, layout.lines, strict=True)
                )
            ),
            total_amount_ul=amount_ul,
        ),
        None,
    )
