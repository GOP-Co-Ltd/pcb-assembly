"""[paste_dispenser] 節：ペーストディスペンサーと、その下位表.

下位表 ``defaults``（pad ごとに上書きできる塗布パラメータ）と ``motion``（吐出ダイナミクス）の型は
:mod:`pcbasm.pasting.params` が持つ。
"""

from __future__ import annotations

import attrs

from pcbasm.geometry import Point3d
from pcbasm.pasting.params import DispenseMotion, PasteParams
from pcbasm.validators import (
    is_finite_number,
    validate_finite_number,
    validate_non_negative_number,
    validate_positive_number,
)

DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618
DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0


@attrs.frozen
class VolumeCorrectionConfig:
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


@attrs.frozen(kw_only=True)
class PasteDispenserConfig:
    """ペーストディスペンサーの設定.

    Attributes:
        rotations_per_ul: 1μL あたりの回転数 [rev/μL]
        nozzle_diameter: ノズル内径 [mm]
        nozzle_outer_diameter: ノズル外径 [mm]。微小線の端 inset に使う。未指定なら nozzle_diameter
        toolhead: カメラに対するツールヘッドの相対位置
        defaults: 塗布パラメータの既定値（基板ごとの override の基準）
        motion: 吐出ダイナミクス
        solder_paste_density: はんだペースト密度 [mg/μL]（既定は S3X70-E150DN）
        auto_line_aspect_ratio: Auto 時に線塗布へ切り替える縦横比
        auto_area_short_side_factor: Auto 時に面塗布へ切り替える短辺のノズル径倍率（短辺 > nozzle_diameter * この値 → area）
        initial_purge_ul: fill sequence 前に pad 中心へ点塗布する初回パージ量 [μL]
        volume_correction: 運転時の塗布量補正
        nozzle_cap: タスク終了時の駐機先
        nozzle_clean: ノズルクリーニング
    """

    rotations_per_ul: float
    nozzle_diameter: float
    nozzle_outer_diameter: float | None = None
    toolhead: ToolheadConfig
    defaults: PasteParams
    motion: DispenseMotion
    solder_paste_density: float = 3.78
    auto_line_aspect_ratio: float = DEFAULT_AUTO_LINE_ASPECT_RATIO
    auto_area_short_side_factor: float = DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR
    initial_purge_ul: float = 0.1
    volume_correction: VolumeCorrectionConfig = attrs.field(
        factory=VolumeCorrectionConfig
    )
    nozzle_cap: NozzleCapConfig = attrs.field(factory=NozzleCapConfig)
    nozzle_clean: NozzleCleanConfig = attrs.field(factory=NozzleCleanConfig)

    def __attrs_post_init__(self) -> None:
        for name in (
            "nozzle_diameter",
            "solder_paste_density",
            "auto_area_short_side_factor",
        ):
            if error := validate_positive_number(name, getattr(self, name)):
                raise ValueError(error)
        if self.nozzle_outer_diameter is not None:
            if error := validate_positive_number(
                "nozzle_outer_diameter", self.nozzle_outer_diameter
            ):
                raise ValueError(error)
        if (
            not is_finite_number(self.auto_line_aspect_ratio)
            or self.auto_line_aspect_ratio <= 1.0
        ):
            raise ValueError(
                "auto_line_aspect_ratioは1.0より大きい必要があります: "
                f"{self.auto_line_aspect_ratio!r}"
            )
        if error := validate_non_negative_number(
            "initial_purge_ul", self.initial_purge_ul
        ):
            raise ValueError(error)
        if self.effective_nozzle_outer_diameter < self.nozzle_diameter:
            raise ValueError(
                "nozzle_outer_diameterはnozzle_diameter以上である必要があります: "
                f"{self.nozzle_outer_diameter} < {self.nozzle_diameter}"
            )

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
