"""[paste_dispenser] 節：ペーストディスペンサーと、その下位表（pad_align は alignment が持つ）."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import attrs

from pcbasm.config.alignment import AlignmentConfig
from pcbasm.geometry import Point3d
from pcbasm.validators import (
    is_finite_number,
    validate_finite_number,
    validate_non_negative_number,
    validate_positive_number,
)

DISPENSE_MODES = ("auto", "dot", "line", "area", "micro_line")
DispenseMode = Literal["auto", "dot", "line", "area", "micro_line"]
LINE_DIRECTIONS = ("unconstrained", "outward", "inward")
LineDirection = Literal["unconstrained", "outward", "inward"]
PasteHeight = float | Literal["auto"]
DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618
DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0

# 数値の代わりに "auto" を受け付ける高さの項目
_AUTO_HEIGHT_FIELDS = frozenset(
    {"paste_height", "micro_line_z_start", "micro_line_z_end"}
)


def validate_paste_lift_height(value: float) -> str | None:
    """塗布後の上昇高さを検証する."""
    if isinstance(value, bool) or value <= 0:
        return f"lift_heightは正の値である必要があります: {value}"
    return None


@attrs.frozen
class FlowCalibrationConfig:
    """運転時流量キャリブレーション（塗布中の吐出量を画像で測って補正する）の設定.

    はんだ塗布の塗布パス直前に、基板ごとに設定した測定位置へ既知量のドットを塗り、
    塗布前後画像から推定した体積の比で ``rotations_per_ul`` を補正する。
    推定には :mod:`pcbasm.pasting.paste_volume` の校正ファイルを使う。

    測定位置は基板ごとに違うので、ここではなく基板設定
    （:class:`~pcbasm.pasting.settings.PasteSettingsModel`）が持つ。
    塗る点数は測定位置の個数で決まり、0 個なら補正しない。

    1 点だけでは点ごとの吐出ばらつき（実測で相対 9〜11 %）がそのまま補正値に
    乗るので、3 点ほど置くとよい。

    ``crop_size_mm`` と測定位置の間隔の関係はここでは検証しない。
    WebUI は項目ごとに保存するので、2 つの値の一方だけが先に書き込まれる。
    ここで不正として拒否すると machine.toml 全体が読めなくなるため、判定は
    :func:`~pcbasm.pasting.paste_volume.runtime.plan_flow_calibration` で行う。

    塗り終えてすぐ撮ると、ペーストが広がりきる前の小さい円を測ることになる。
    そのため塗布後の撮影に入る前に ``settle_seconds`` だけ待つ。

    Attributes:
        calibration_file: 使う校正ファイル名（空なら無効）
        amount_ul: 1 点あたりの指令塗布量 [μL]
        crop_size_mm: 塗布前後画像の一辺 [mm]
        settle_seconds: 全点を塗ってから塗布後の撮影に入るまでの待ち [秒]（0 で待たない）
    """

    calibration_file: str = ""
    amount_ul: float = 0.2
    crop_size_mm: float = 2.0
    settle_seconds: float = 10.0

    def __attrs_post_init__(self) -> None:
        for name in ("amount_ul", "crop_size_mm"):
            if error := validate_positive_number(name, getattr(self, name)):
                raise ValueError(error)
        if error := validate_non_negative_number("settle_seconds", self.settle_seconds):
            raise ValueError(error)

    @property
    def enabled(self) -> bool:
        """校正ファイルが指定されていて、実際に補正を試みるか."""
        return bool(self.calibration_file)


@attrs.frozen
class NozzleCapConfig:
    """ノズルキャップ位置の設定.

    Attributes:
        position: キャップ位置（マシン座標 [mm]）。未教示なら None で、駐機は PRESENT へ落ちる
    """

    position: Point3d | None = None


@attrs.frozen
class NozzleCleanConfig:
    """ノズルクリーニング位置と動作の設定（マシン座標 [mm]）.

    クリーニング時は ``position`` の XY へ移動し、その場で ``purge_ul`` だけ押し出してから
    十字に往復してノズル先端をシリコンクリーナーへこすりつける。塗布ジョブがこれを
    行うのは、初回パージを撮って塗布を検出できなかったときだけ。

    ``position.z`` はクリーニング面（シリコン表面）の高さで、ノズル先端が面に触れる位置を
    教示して記録する。実際にこする高さは面から ``press_depth`` だけ押し込んだ位置。
    面と押し込み量を分けてあるので、シリコンが摩耗したら ``press_depth`` だけ増やせばよい。

    座標は既定値を持たない。原点を既定にすると、そこをクリーニング位置として移動してしまう。
    設定画面から動作値だけを保存した節は、未教示（``position`` が None）として読む。

    ``press_depth`` の上限はここで検証しない。妥当性は押し込んだ高さが可動域に入るかでしか
    決まらず、それは printer.cfg 依存なので
    :func:`~pcbasm.pasting.nozzle_clean.plan_nozzle_clean` が判定する。

    Attributes:
        position: クリーニング面の位置。未教示なら None で、塗布ジョブはこすらない
        press_depth: 面からの押し込み量。0 で押し込まない
        purge_ul: こすり前にその場で押し出す量 [uL]。0 でパージしない
        stroke: 十字往復の片振幅。0 でこすらない
        passes: 十字往復の反復回数。0 でこすらない
        wipe_speed: こすり移動速度 [mm/sec]
    """

    position: Point3d | None = None
    press_depth: float = 0.5
    purge_ul: float = 0.2
    stroke: float = 2.0
    passes: int = 2
    wipe_speed: float = 10.0

    def __attrs_post_init__(self) -> None:
        if self.position is not None:
            for name in ("x", "y", "z"):
                if error := validate_finite_number(name, getattr(self.position, name)):
                    raise ValueError(error)
        for name in ("press_depth", "purge_ul", "stroke"):
            if error := validate_non_negative_number(name, getattr(self, name)):
                raise ValueError(error)
        if error := validate_positive_number("wipe_speed", self.wipe_speed):
            raise ValueError(error)
        if (
            isinstance(self.passes, bool)
            or not isinstance(self.passes, int)
            or self.passes < 0
        ):
            raise ValueError(
                f"passesは0以上の整数である必要があります: {self.passes!r}"
            )


@attrs.frozen
class ToolheadConfig:
    """ツールヘッドの設定."""

    x: float
    y: float


@attrs.frozen
class PasteDispenserConfig:
    """ペーストディスペンサーの設定."""

    rotations_per_ul: float  # 1μL あたりの回転数 [rev/μL]
    nozzle_diameter: float  # ノズル内径 [mm]
    nozzle_outer_diameter: float | None = attrs.field(
        default=None, kw_only=True
    )  # ノズル外径 [mm]。微小線の端 inset に使う。未指定時は nozzle_diameter
    max_fill_speed: float  # 連続塗布できる移動速度上限 [mm/sec]
    max_dispense_rate: float  # 吐出レート上限 [μL/sec]
    dispense_accel: float  # 吐出加速度 [μL/sec²]
    retract_amount: float  # リトラクション量 [μL]
    retract_rate: float | None = attrs.field(
        default=None, kw_only=True
    )  # リトラクションレート [μL/sec]。未指定時は max_dispense_rate
    retract_accel_factor: float  # リトラクション加速度係数
    toolhead: ToolheadConfig
    paste_height: PasteHeight  # 塗布時の基板表面からのノズル高さ [mm]、または auto
    ul_per_mm2: float  # パッド面積あたりのペースト量 [μL/mm²]
    lift_height: float = 2.0  # 塗布後に持ち上げる高さ [mm]
    solder_paste_density: float = 3.78  # はんだペースト密度 [mg/μL] (S3X70-E150DN)
    dispense_mode: DispenseMode = (
        "auto"  # 塗布方式 auto / dot / line / area / micro_line
    )
    line_direction: LineDirection = (
        "unconstrained"  # 線塗布の走行方向 unconstrained / outward / inward
    )
    auto_line_aspect_ratio: float = (
        DEFAULT_AUTO_LINE_ASPECT_RATIO  # Auto 時に線塗布へ切り替える縦横比
    )
    auto_area_short_side_factor: float = (
        # Auto 時に面塗布へ切り替える短辺のノズル径倍率（短辺 > nozzle_diameter * この値 → area）
        DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR
    )
    prime_extra_delay: float = 0.0  # プライム後の追加遅延 [sec]
    initial_purge_ul: float = (
        0.1  # fill sequence 前に pad 中心へ点塗布する初回パージ量 [μL]
    )
    bead_width_factor: float = (
        1.0  # ビード幅係数 w = nozzle_diameter * bead_width_factor
    )
    overlap: float = 0.0  # ジグザグ行間オーバーラップ [0,1)
    boundary_margin: float = 0.0  # 外周マージン [mm]（負なら pad 外形をはみ出して塗る）
    micro_line_z_start: PasteHeight = (
        "auto"  # 微小線の始点で基板表面から取る隙間 [mm]、または auto（塗布量から計算）
    )
    micro_line_z_end: PasteHeight = (
        "auto"  # 微小線の終点で基板表面から取る隙間 [mm]、または auto（塗布量から計算）
    )
    micro_line_settle_seconds: float = (
        0.1  # 微小線で上空に押し出してから下降するまでの静定待ち [sec]
    )
    post_dispense_settle_seconds: float = (
        0.1  # 塗布の移動が終わってからヘッドを上げるまでの静定待ち [sec]（全方式）
    )
    pad_align: AlignmentConfig = attrs.field(
        factory=AlignmentConfig
    )  # pad 位置合わせ設定
    flow_calibration: FlowCalibrationConfig = attrs.field(
        factory=FlowCalibrationConfig
    )  # 運転時流量キャリブレーション設定
    nozzle_cap: NozzleCapConfig = attrs.field(
        factory=NozzleCapConfig
    )  # タスク終了時の駐機先
    nozzle_clean: NozzleCleanConfig = attrs.field(
        factory=NozzleCleanConfig
    )  # ノズルクリーニング

    def __attrs_post_init__(self) -> None:
        if error := self.validate_values(attrs.asdict(self, recurse=False)):
            raise ValueError(error)
        if self.effective_nozzle_outer_diameter < self.nozzle_diameter:
            raise ValueError(
                "nozzle_outer_diameterはnozzle_diameter以上である必要があります: "
                f"{self.nozzle_outer_diameter} < {self.nozzle_diameter}"
            )

    @staticmethod
    def validate_values(values: Mapping[str, object]) -> str | None:
        """塗布設定の値を検証する。部分編集では渡された項目だけを調べる.

        TOML 読込・マシン設定の保存・pad override で同じ制約を使う。
        キーの許可判定と、入れ子の設定の検証は各入力元が担当する。
        """
        positive = {
            "auto_area_short_side_factor",
            "paste_height",
            "lift_height",
            "solder_paste_density",
            "nozzle_diameter",
            "nozzle_outer_diameter",
            "max_fill_speed",
            "max_dispense_rate",
            "retract_amount",
            "bead_width_factor",
            "ul_per_mm2",
            "micro_line_z_start",
            "micro_line_z_end",
        }
        above_one = {"auto_line_aspect_ratio", "retract_accel_factor"}
        non_negative = {
            "initial_purge_ul",
            "prime_extra_delay",
            "micro_line_settle_seconds",
            "post_dispense_settle_seconds",
        }
        numeric = positive | above_one | non_negative | {"overlap", "boundary_margin"}
        for name, value in values.items():
            if name == "dispense_mode":
                if not isinstance(value, str) or value not in DISPENSE_MODES:
                    return f"未知の塗布方式です: {value!r}"
            elif name == "line_direction":
                if not isinstance(value, str) or value not in LINE_DIRECTIONS:
                    return f"未知の線走行方向です: {value!r}"
            elif name in numeric:
                if name in _AUTO_HEIGHT_FIELDS and value == "auto":
                    continue
                if name == "nozzle_outer_diameter" and value is None:
                    continue
                if not is_finite_number(value):
                    return f"{name}は有限な数値である必要があります: {value!r}"
                if name in positive and value <= 0:
                    return f"{name}は正の値である必要があります: {value}"
                if name in above_one and value <= 1.0:
                    return f"{name}は1.0より大きい必要があります: {value}"
                if name in non_negative and value < 0:
                    return f"{name}は0以上の値である必要があります: {value}"
                if name == "overlap" and not 0.0 <= value < 1.0:
                    return f"overlapは0以上1未満である必要があります: {value}"
        return None

    @property
    def density_mg_per_ul(self) -> float:
        """はんだペースト密度 [mg/μL]（TOML キー ``solder_paste_density`` のコード内名）."""
        return self.solder_paste_density

    @property
    def effective_nozzle_outer_diameter(self) -> float:
        """nozzle_outer_diameter 未指定時は nozzle_diameter を返す."""
        return (
            self.nozzle_diameter
            if self.nozzle_outer_diameter is None
            else self.nozzle_outer_diameter
        )

    @property
    def effective_retract_rate(self) -> float:
        """retract_rate 未指定時は max_dispense_rate を返す."""
        return (
            self.max_dispense_rate if self.retract_rate is None else self.retract_rate
        )
