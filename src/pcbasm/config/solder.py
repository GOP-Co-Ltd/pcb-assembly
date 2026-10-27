"""[solder] 節：はんだ塗布の装置と、その下位表.

``[solder]`` があれば、その機体ははんだを塗布できる（:attr:`pcbasm.config.MachineConfig.solder`）。
下位表は装置の部品と用途で分ける。

- ``dispenser``: オーガー式ディスペンサー（吐出の物理）
- ``nozzle``: ノズルの径と、カメラからの位置（``offset``）、駐機先（``cap``）、掃除（``clean``）
- ``motion``: 塗布中の動き
- ``defaults``: pad の塗布設定の既定（型は :class:`~pcbasm.pasting.params.PasteParams`）
- ``auto``: 塗布方式 auto の切り替えしきい値
- ``volume_correction``: 運転時の塗布量補正
- ``tact``: 所要時間の予測

設定ページの ``order`` は ``[solder]`` の画面での通し番号で、params の項目と並びを分け合う（重なると :func:`~pcbasm.config.fields.setting_fields` が ValueError にする）。
"""

from __future__ import annotations

import attrs

from pcbasm.config.fields import XYZ, setting
from pcbasm.geometry import Point3d
from pcbasm.pasting.params import DispenseMotion, PasteParams
from pcbasm.validators import (
    as_validator,
    is_finite_number,
    validate_finite_number,
    validate_greater_than_one,
    validate_non_negative_number,
    validate_positive_number,
)

# 設定ページのカード
_BASICS = "吐出の基本"
_SPEED = "吐出の速度"
_RETRACT = "リトラクト"
_PURGE = "プライム・パージ"
_HEIGHT = "Z 高さ"
_MICRO_LINE = "微小線"
_AUTO = "Auto の切り替え"
_VOLUME = "流量キャリブレーション"
_OFFSET = "ツールヘッド"
_POSITIONS = "ノズル位置"
_CLEAN = "ノズルクリーニング"
_TACT = "所要時間の予測"

_positive = as_validator(validate_positive_number)
_non_negative = as_validator(validate_non_negative_number)
_above_one = as_validator(validate_greater_than_one)


def _at_least_one(_: object, attribute: attrs.Attribute, value: float) -> None:
    if not is_finite_number(value) or value < 1.0:
        raise ValueError(
            f"{attribute.name}は1以上の有限値である必要があります: {value!r}"
        )


def _non_negative_int(_: object, __: attrs.Attribute, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"passesは0以上の整数である必要があります: {value!r}")


@attrs.frozen(kw_only=True)
class DispenserConfig:
    """オーガー式ディスペンサー（[solder.dispenser]。吐出の物理）.

    ``retract_rate`` を書かないと ``max_dispense_rate`` と同じ値になる（``pcbasm.config`` が読み込み時に解決する）。

    Attributes:
        rotations_per_ul: 1μL あたりの回転数 [rev/μL]
        max_dispense_rate: 吐出レート上限 [μL/sec]
        dispense_accel: 吐出加速度 [μL/sec²]
        retract_amount: リトラクション量 [μL]
        retract_rate: リトラクション速度 [μL/sec]
        retract_accel_factor: リトラクション加速度係数（> 1.0）
    """

    rotations_per_ul: float = attrs.field(
        metadata=setting(
            label="1uLあたりの回転数", unit="rev/uL", group=_BASICS, order=10
        )
    )
    max_dispense_rate: float = attrs.field(
        validator=_positive,
        metadata=setting(label="吐出レート上限", unit="uL/s", group=_SPEED, order=20),
    )
    dispense_accel: float = attrs.field(
        validator=_positive,
        metadata=setting(label="吐出加速度", unit="uL/s²", group=_SPEED, order=21),
    )
    retract_amount: float = attrs.field(
        validator=_positive,
        metadata=setting(label="リトラクション量", unit="uL", group=_RETRACT, order=30),
    )
    retract_rate: float = attrs.field(
        validator=_positive,
        metadata=setting(
            label="リトラクションレート", unit="uL/s", group=_RETRACT, order=31
        ),
    )
    retract_accel_factor: float = attrs.field(
        validator=_above_one,
        metadata=setting(label="リトラクション加速度係数", group=_RETRACT, order=32),
    )


@attrs.frozen
class NozzleOffsetConfig:
    """カメラ中心からノズルまでの相対位置（[solder.nozzle.offset]）[mm]."""

    x: float = attrs.field(
        metadata=setting(
            label="ツールヘッド相対位置 X", unit="mm", group=_OFFSET, order=90
        )
    )
    y: float = attrs.field(
        metadata=setting(
            label="ツールヘッド相対位置 Y", unit="mm", group=_OFFSET, order=91
        )
    )


@attrs.frozen
class NozzleCapConfig:
    """ノズルキャップ位置（[solder.nozzle.cap]）.

    Attributes:
        position: キャップ位置（マシン座標 [mm]）。未教示なら None で、駐機は PRESENT へ落ちる
    """

    position: Point3d | None = attrs.field(
        default=None,
        metadata=setting(
            label="キャップ位置", unit="mm", group=_POSITIONS, order=110, set=XYZ
        ),
    )


@attrs.frozen
class NozzleCleanConfig:
    """ノズルクリーニング位置と動作（[solder.nozzle.clean]。マシン座標 [mm]）.

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

    position: Point3d | None = attrs.field(
        default=None,
        metadata=setting(
            label="クリーニング位置（Zは面の高さ）",
            unit="mm",
            group=_POSITIONS,
            order=111,
            set=XYZ,
        ),
    )
    press_depth: float = attrs.field(
        default=0.5,
        validator=_non_negative,
        metadata=setting(
            label="押し込み量", unit="mm", group=_CLEAN, order=120, minimum=0.0
        ),
    )
    purge_ul: float = attrs.field(
        default=0.2,
        validator=_non_negative,
        metadata=setting(
            label="クリーニング前パージ量",
            unit="uL",
            group=_CLEAN,
            order=121,
            minimum=0.0,
        ),
    )
    stroke: float = attrs.field(
        default=2.0,
        validator=_non_negative,
        metadata=setting(
            label="こすり幅 片側", unit="mm", group=_CLEAN, order=122, minimum=0.0
        ),
    )
    passes: int = attrs.field(
        default=2,
        validator=_non_negative_int,
        metadata=setting(label="往復回数", group=_CLEAN, order=123, minimum=0),
    )
    wipe_speed: float = attrs.field(
        default=10.0,
        validator=_positive,
        metadata=setting(label="こすり速度", unit="mm/sec", group=_CLEAN, order=124),
    )

    def __attrs_post_init__(self) -> None:
        if self.position is not None:
            for name in ("x", "y", "z"):
                if error := validate_finite_number(name, getattr(self.position, name)):
                    raise ValueError(error)


@attrs.frozen(kw_only=True)
class NozzleConfig:
    """ノズル（[solder.nozzle]）.

    Attributes:
        diameter: 内径 [mm]
        outer_diameter: 外径 [mm]。微小線の端 inset に使う。未指定なら diameter
        offset: カメラ中心からノズルまでの相対位置
        cap: タスク終了時の駐機先
        clean: ノズルクリーニング
    """

    diameter: float = attrs.field(
        validator=_positive,
        metadata=setting(label="ノズル内径", unit="mm", group=_BASICS, order=11),
    )
    outer_diameter: float | None = attrs.field(
        default=None,
        validator=attrs.validators.optional(_positive),
        metadata=setting(label="ノズル外径", unit="mm", group=_BASICS, order=12),
    )
    offset: NozzleOffsetConfig
    cap: NozzleCapConfig = attrs.field(factory=NozzleCapConfig)
    clean: NozzleCleanConfig = attrs.field(factory=NozzleCleanConfig)

    def __attrs_post_init__(self) -> None:
        if self.effective_outer_diameter < self.diameter:
            raise ValueError(
                "outer_diameterはdiameter以上である必要があります: "
                f"{self.outer_diameter} < {self.diameter}"
            )

    @property
    def effective_outer_diameter(self) -> float:
        """outer_diameter 未指定時は diameter を返す."""
        return self.diameter if self.outer_diameter is None else self.outer_diameter


@attrs.frozen(kw_only=True)
class SolderMotionConfig:
    """塗布中の動き（[solder.motion]）.

    Attributes:
        max_fill_speed: 連続塗布できる移動速度上限 [mm/sec]
        lift_height: 塗布後の上昇高さ [mm]
        micro_line_settle_seconds: 微小線で上空に押し出してから下降するまでの静定待ち [sec]
        post_dispense_settle_seconds: 塗布の移動が終わってからヘッドを上げるまでの静定待ち [sec]
    """

    max_fill_speed: float = attrs.field(
        validator=_positive,
        metadata=setting(label="最大塗布速度", unit="mm/s", group=_SPEED, order=22),
    )
    lift_height: float = attrs.field(
        default=2.0,
        validator=_positive,
        metadata=setting(label="吐出後の上昇高さ", unit="mm", group=_HEIGHT, order=81),
    )
    micro_line_settle_seconds: float = attrs.field(
        default=0.1,
        validator=_non_negative,
        metadata=setting(
            label="微小線の静定待ち",
            unit="s",
            group=_MICRO_LINE,
            order=72,
            minimum=0.0,
        ),
    )
    post_dispense_settle_seconds: float = attrs.field(
        default=0.1,
        validator=_non_negative,
        metadata=setting(
            label="塗布後の静定待ち（上昇前）",
            unit="s",
            group=_HEIGHT,
            order=82,
            minimum=0.0,
        ),
    )


@attrs.frozen
class AutoModeConfig:
    """塗布方式 auto の切り替えしきい値（[solder.auto]）.

    Attributes:
        line_aspect_ratio: 線塗布へ切り替える縦横比
        area_short_side_factor: 面塗布へ切り替える短辺のノズル径倍率（短辺 > nozzle.diameter * この値 → area）
    """

    line_aspect_ratio: float = attrs.field(
        default=1.618,
        validator=_above_one,
        metadata=setting(label="Auto線塗布しきい縦横比", group=_AUTO, order=55),
    )
    area_short_side_factor: float = attrs.field(
        default=3.0,
        validator=_positive,
        metadata=setting(label="Auto面塗布しきい短辺倍率", group=_AUTO, order=56),
    )


@attrs.frozen
class VolumeCorrectionConfig:
    """運転時補正（塗布中の吐出量を画像で測って補正する。[solder.volume_correction]）.

    はんだ塗布の塗布パス直前に、基板ごとに設定した測定位置へ既知量のドットを塗り、
    塗布前後画像から推定した体積の比で ``rotations_per_ul`` を補正する。
    推定には :mod:`pcbasm.pasting.paste_volume` の校正ファイルを使う。

    測定位置は基板ごとに違うので、ここではなく基板設定
    （:class:`~pcbasm.pasting.overrides.PasteSettingsModel`）が持つ。
    塗る点数は測定位置の個数で決まり、0 個なら補正しない。

    1 点だけでは点ごとの吐出ばらつき（実測で相対 9〜11 %）がそのまま補正値に
    乗るので、3 点ほど置くとよい。

    ``crop_size_mm`` と測定位置の間隔の関係はここでは検証しない。
    WebUI は項目ごとに保存するので、2 つの値の一方だけが先に書き込まれる。
    ここで不正として拒否すると machine.toml 全体が読めなくなるため、判定は
    :func:`~pcbasm.pasting.paste_volume.runtime_correction.plan_flow_calibration` で行う。

    塗り終えてすぐ撮ると、ペーストが広がりきる前の小さい円を測ることになる。
    そのため塗布後の撮影に入る前に ``settle_seconds`` だけ待つ。

    補正で ``rotations_per_ul`` が ``max_correction_scale`` 倍を超えて増える、または
    1/``max_correction_scale`` 倍を下回って減るときは、pad の塗布を始めずにジョブを失敗させる。
    そこまでずれる係数は運転時補正で直すものではなく、吐出量キャリブレーションのやり直しが要る。

    Attributes:
        calibration_file: 使う校正ファイル名（空なら無効）
        amount_ul: 1 点あたりの指令塗布量 [μL]
        crop_size_mm: 塗布前後画像の一辺 [mm]
        settle_seconds: 全点を塗ってから塗布後の撮影に入るまでの待ち [秒]（0 で待たない）
        max_correction_scale: 補正で許す ``rotations_per_ul`` の変化の倍率（1.0 より大きい。増減とも）
    """

    calibration_file: str = attrs.field(
        default="",
        metadata=setting(
            label="校正ファイル", group=_VOLUME, order=100, clearable=True
        ),
    )
    amount_ul: float = attrs.field(
        default=0.2,
        validator=_positive,
        metadata=setting(
            label="1点あたりの塗布量", unit="uL", group=_VOLUME, order=101
        ),
    )
    crop_size_mm: float = attrs.field(
        default=2.0,
        validator=_positive,
        metadata=setting(label="撮影crop寸法", unit="mm", group=_VOLUME, order=102),
    )
    settle_seconds: float = attrs.field(
        default=10.0,
        validator=_non_negative,
        metadata=setting(
            label="静定待ち", unit="s", group=_VOLUME, order=103, minimum=0.0
        ),
    )
    max_correction_scale: float = attrs.field(
        default=2.0,
        validator=_above_one,
        metadata=setting(label="補正の許容倍率", group=_VOLUME, order=104),
    )

    @property
    def enabled(self) -> bool:
        """校正ファイルが指定されていて、実際に補正を試みるか."""
        return bool(self.calibration_file)


@attrs.frozen
class TactConfig:
    """塗布の所要時間の予測（[solder.tact]。節ごと省略可）.

    塗布ジョブの時間は :mod:`pcbasm.pasting.tact` が、塗布計画・基板・printer.cfg の速度と加速度・機体の静定待ちとカメラの設定から求める。
    ここには、それで求まらない分だけを置く。
    既定値は 001 の実測（2026-10-26、47.5 x 20 mm・65 pad の基板）から決めた。
    ジョブコンソールの経過時間（実測）と予測を見比べて合わせる。

    Attributes:
        fixed_sec: 基板に依らない固定分 [sec]（カメラの起動・ホーミング・最初の基準点への移動・カメラ回転角の計測・最初のプローブの下降・終了後の退避など）
        probe_point_sec: プローブ 1 点の PROBE（下降・荷重の計測・上昇）[sec]
        capture_sec: 撮影 1 回の処理（画像処理と応答の待ち。移動・静定待ち・フレームを撮る時間を除く）[sec]
        align_passes: 基準点・照合領域・pad 中心を 1 か所合わせるまでの平均の撮影回数
    """

    fixed_sec: float = attrs.field(
        default=60.0,
        validator=_non_negative,
        metadata=setting(label="固定分", unit="s", group=_TACT, order=130, minimum=0.0),
    )
    probe_point_sec: float = attrs.field(
        default=3.0,
        validator=_non_negative,
        metadata=setting(
            label="プローブ 1 点", unit="s", group=_TACT, order=131, minimum=0.0
        ),
    )
    capture_sec: float = attrs.field(
        default=0.4,
        validator=_non_negative,
        metadata=setting(
            label="撮影 1 回の処理", unit="s", group=_TACT, order=132, minimum=0.0
        ),
    )
    align_passes: float = attrs.field(
        default=2.3,
        validator=_at_least_one,
        metadata=setting(label="1 か所の撮影回数", group=_TACT, order=133, minimum=1.0),
    )


@attrs.frozen(kw_only=True)
class SolderConfig:
    """はんだ塗布の設定（[solder]）.

    Attributes:
        paste_density: はんだペースト密度 [mg/μL]（既定は S3X70-E150DN）
        initial_purge_ul: fill sequence 前に pad 中心へ点塗布する初回パージ量 [μL]
        dispenser: ディスペンサー
        nozzle: ノズル
        motion: 塗布中の動き
        defaults: 塗布パラメータの既定値（基板ごとの override の基準）
        auto: 塗布方式 auto の切り替えしきい値
        volume_correction: 運転時の塗布量補正
        tact: 所要時間の予測
    """

    paste_density: float = attrs.field(
        default=3.78,
        validator=_positive,
        metadata=setting(
            label="はんだペースト密度", unit="mg/uL", group=_BASICS, order=13
        ),
    )
    initial_purge_ul: float = attrs.field(
        default=0.1,
        validator=_non_negative,
        metadata=setting(
            label="初回パージ量", unit="uL", group=_PURGE, order=41, minimum=0.0
        ),
    )
    dispenser: DispenserConfig
    nozzle: NozzleConfig
    motion: SolderMotionConfig
    defaults: PasteParams
    auto: AutoModeConfig = attrs.field(factory=AutoModeConfig)
    volume_correction: VolumeCorrectionConfig = attrs.field(
        factory=VolumeCorrectionConfig
    )
    tact: TactConfig = attrs.field(factory=TactConfig)

    @property
    def dispense_motion(self) -> DispenseMotion:
        """Fill sequence が使う吐出の動き（``dispenser`` の吐出の物理と ``motion``）."""
        dispenser = attrs.asdict(self.dispenser)
        del dispenser["rotations_per_ul"]
        return DispenseMotion(**dispenser, **attrs.asdict(self.motion))
