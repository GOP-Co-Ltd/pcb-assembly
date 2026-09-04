"""流量キャリブレーションジョブのパラメータ（既定値の唯一の出典）."""

from collections.abc import Mapping
from typing import Any, Self

import attrs

from pcbasm.pasting.flowcalib.lines import LineLayout

# 段ずらしレイアウトの描画領域マージン（銅板端から全周）[mm]
LAYOUT_MARGIN_MM = 5.0

_INT_FIELDS = frozenset({"line_count", "rate_divisions", "speed_divisions"})
# web ジョブの ParamSpec 名（永続化キー）と属性名が異なるもの: ParamSpec 名 → 属性名
PARAM_ALIASES: Mapping[str, str] = {"line_amount": "line_amount_ul"}


@attrs.frozen
class CalibrationParams:
    """吐出量キャリブレーション統合ジョブの入力パラメータ.

    web ジョブの ``ParamSpec`` 既定値はこのクラスの既定値を参照する。線設定・掃引範囲は
    実行中変更可のため、:meth:`from_mapping` で各ラウンドの先頭に読み直す。

    Attributes:
        board_width: 銅板幅 [mm]
        board_height: 銅板高さ [mm]
        line_length: 線の長さ [mm]
        line_count: ① の線の本数
        line_amount_ul: ① の 1 線あたりの塗布量 [μL]
        row_pitch: 線の段ずらし間隔 [mm]
        removal_z_offset: 計量退避 Z オフセット [mm]（z_max から引く量。0 で全退避）
        rate_min: ② 吐出レート最小 [μL/sec]
        rate_max: ② 吐出レート最大 [μL/sec]
        rate_divisions: ② 吐出レート分割数
        speed_min: ③ 塗布速度最小 [mm/sec]
        speed_max: ③ 塗布速度最大 [mm/sec]
        speed_divisions: ③ 塗布速度分割数
    """

    board_width: float = 40.0
    board_height: float = 40.0
    line_length: float = 10.0
    line_count: int = 10
    line_amount_ul: float = 0.5
    row_pitch: float = 3.0
    removal_z_offset: float = 0.0
    rate_min: float = 0.5
    rate_max: float = 5.0
    rate_divisions: int = 6
    speed_min: float = 1.0
    speed_max: float = 10.0
    speed_divisions: int = 6

    @classmethod
    def from_mapping(cls, values: Mapping[str, float | int | str]) -> Self:
        """ジョブの ``ctx.params`` から読み直す.

        int 系（本数・分割数）は ``max(1, int(v))`` に正規化する。欠落キーは既定値。
        :data:`PARAM_ALIASES` のキー（``line_amount``）は属性名へ読み替える。
        """
        aliased = {PARAM_ALIASES.get(key, key): value for key, value in values.items()}
        kwargs: dict[str, Any] = {}
        for field in attrs.fields(cls):
            if field.name not in aliased:
                continue
            value = aliased[field.name]
            if field.name in _INT_FIELDS:
                kwargs[field.name] = max(1, int(value))
            else:
                kwargs[field.name] = float(value)
        return cls(**kwargs)

    @property
    def required_line_count(self) -> int:
        """①②③ のいずれを実行しても必要になる線の本数（最大値）."""
        return max(self.line_count, self.rate_divisions, self.speed_divisions)

    def line_layout(self, line_count: int | None = None) -> LineLayout:
        """段ずらしレイアウトを組む（``line_count`` 省略時は ① の本数）."""
        return LineLayout(
            line_length=self.line_length,
            line_count=self.line_count if line_count is None else line_count,
            row_pitch=self.row_pitch,
            board_width=self.board_width,
            board_height=self.board_height,
            margin=LAYOUT_MARGIN_MM,
        )
