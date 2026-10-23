"""Config を読み込む機能を実装するモジュール."""

from __future__ import annotations

import copy
import os
import tomllib
from collections.abc import Callable, Mapping
from enum import Enum, auto
from math import isfinite
from pathlib import Path
from typing import Any, Literal

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn

from pcbasm.geometry import Point2d, Point3d, Shift, Transform
from pcbasm.utils import PROJECT_ROOT, is_finite_number

DISPENSE_MODES = ("auto", "dot", "line", "area", "micro_line")
DispenseMode = Literal["auto", "dot", "line", "area", "micro_line"]
LINE_DIRECTIONS = ("unconstrained", "outward", "inward")
LineDirection = Literal["unconstrained", "outward", "inward"]
MACHINE_TYPES = ("paste", "pnp")
MachineType = Literal["paste", "pnp"]
PasteHeight = float | Literal["auto"]
DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618
DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0
DEFAULT_AUDIO_DEVICE = "default"  # ALSA のシステム既定 PCM
DEFAULT_AUDIO_VOLUME = 0.75


# 数値の代わりに "auto" を受け付ける高さの項目
_AUTO_HEIGHT_FIELDS = frozenset(
    {"paste_height", "micro_line_z_start", "micro_line_z_end"}
)


def resolve_paste_height(paste_height: PasteHeight, ul_per_mm2: float) -> float:
    """塗布高さ [mm] を解決する.

    ``auto`` のときは ``ul_per_mm2``（μL/mm² = mm³/mm² = mm、すなわち目標膜厚）を
    そのまま塗布高さ（基板表面からのクリアランス [mm]）として使う。数値指定なら
    その値を返す。
    """
    if paste_height == "auto":
        return ul_per_mm2
    return paste_height


@attrs.frozen
class Klipper:
    """Klipper の設定."""

    host: str = "localhost"
    port: int = 7125


def validate_audio_device(device: str) -> str | None:
    """通知音の出力デバイス名を検証する."""
    if not device.strip():
        return "audio.deviceは空でない文字列である必要があります"
    return None


def validate_audio_volume(volume: float) -> str | None:
    """通知音の音量を検証する."""
    if not isfinite(volume) or not 0.0 <= volume <= 1.0:
        return f"audio.volumeは0以上1以下の有限値である必要があります: {volume!r}"
    return None


@attrs.frozen
class Audio:
    """通知音の出力設定."""

    device: str = DEFAULT_AUDIO_DEVICE
    volume: float = DEFAULT_AUDIO_VOLUME

    def __attrs_post_init__(self) -> None:
        if error := validate_audio_device(self.device):
            raise ValueError(error)
        if isinstance(self.volume, bool) or not isinstance(self.volume, (int, float)):
            raise ValueError(
                f"audio.volumeは0以上1以下の有限値である必要があります: {self.volume!r}"
            )
        volume = float(self.volume)
        if error := validate_audio_volume(volume):
            raise ValueError(error)
        object.__setattr__(self, "device", self.device.strip())
        object.__setattr__(self, "volume", volume)


@attrs.frozen
class PadAlign:
    """重複領域の銅箔照合による位置合わせの設定."""

    region_size_px: int = 100  # 照合領域の一辺 [px]
    region_overlap: float = 0.5  # 隣接する照合領域の重なり [0, 1)
    board_edge_margin: float = 0.5  # 基板外形から照合領域までの余白 [mm]
    max_passes: int = 5  # 1 領域あたりの再計測上限
    converge_tolerance: float = 0.03  # 収束とみなす増分 [mm]
    max_correction: float = 1.0  # 1 領域で許容する累積ずれ [mm]
    search_window: float = 2.0  # 照合の探索窓 片側幅 [mm]
    refine_max_short_side: float = 0.4  # pad 別逐次位置合わせの最大短辺 [mm]
    canny_low: float = 100.0  # Canny エッジ検出の下側閾値
    canny_high: float = 200.0  # Canny エッジ検出の上側閾値
    blur_ksize: int = 5  # GaussianBlur カーネルサイズ (奇数)

    def __attrs_post_init__(self) -> None:
        if (
            isinstance(self.region_size_px, bool)
            or not isinstance(self.region_size_px, int)
            or self.region_size_px < 1
        ):
            raise ValueError(
                f"region_size_pxは1以上の整数である必要があります: "
                f"{self.region_size_px}"
            )
        if error := validate_region_overlap(self.region_overlap):
            raise ValueError(error)
        if (
            isinstance(self.max_passes, bool)
            or not isinstance(self.max_passes, int)
            or self.max_passes < 1
        ):
            raise ValueError(
                f"max_passesは1以上の整数である必要があります: {self.max_passes}"
            )
        for name in (
            "board_edge_margin",
            "converge_tolerance",
            "max_correction",
            "search_window",
        ):
            if error := validate_positive_number(name, getattr(self, name)):
                raise ValueError(error)
        if error := validate_non_negative_number(
            "refine_max_short_side", self.refine_max_short_side
        ):
            raise ValueError(error)
        if error := validate_positive_odd_integer("blur_ksize", self.blur_ksize):
            raise ValueError(error)


def validate_region_overlap(value: float) -> str | None:
    """照合領域の重なり率を検証する."""
    if not is_finite_number(value) or not 0.0 <= value < 1.0:
        return f"region_overlapは0以上1未満の有限値である必要があります: {value!r}"
    return None


def validate_finite_number(name: str, value: float) -> str | None:
    """有限値であるべき設定値を検証する."""
    if not is_finite_number(value):
        return f"{name}は有限な数値である必要があります: {value!r}"
    return None


def validate_positive_number(name: str, value: float) -> str | None:
    """正の有限値であるべき設定値を検証する."""
    if not is_finite_number(value) or value <= 0:
        return f"{name}は正の有限値である必要があります: {value!r}"
    return None


def validate_non_negative_number(name: str, value: float) -> str | None:
    """0 以上の有限値であるべき設定値を検証する."""
    if not is_finite_number(value) or value < 0:
        return f"{name}は0以上の有限値である必要があります: {value!r}"
    return None


def validate_positive_int(name: str, value: object) -> str | None:
    """1 以上の整数であるべき設定値を検証する."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"{name}は1以上の整数である必要があります: {value!r}"
    return None


def validate_positive_odd_integer(name: str, value: object) -> str | None:
    """正の奇数であるべき設定値を検証する."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value % 2 == 0
    ):
        return f"{name}は正の奇数である必要があります: {value!r}"
    return None


def validate_paste_lift_height(value: float) -> str | None:
    """塗布後の上昇高さを検証する."""
    if isinstance(value, bool) or value <= 0:
        return f"lift_heightは正の値である必要があります: {value}"
    return None


@attrs.frozen
class FlowCalibration:
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
class Settle:
    """装置の静定待ち（[settle]。セクションごと省略可）.

    どちらも G4 dwell としてステージへ送る。ステージが止まっても像や機構は少し揺れて
    いるので、撮影・プローブの前に待つ。長くすればタクトタイムが伸び、短くすれば
    検出がぶれる。適切な値は機体の剛性で決まるので、機体ごとに調整する。

    ``move_sec`` は位置合わせ・基板計測・高さ計測・撮影など、移動後の静定すべてで
    共用する。分けるべき差（例えば Z 下降後だけ長く要る）が実測で出たら、そのとき
    設定項目を追加する。

    Attributes:
        move_sec: ステージ移動後、撮影・計測に入るまでの待ち [sec]
        probe_sec: PROBE 実行後の待ち [sec]
    """

    move_sec: float = 0.5
    probe_sec: float = 0.0

    def __attrs_post_init__(self) -> None:
        for name in ("move_sec", "probe_sec"):
            if error := validate_non_negative_number(name, getattr(self, name)):
                raise ValueError(error)


@attrs.frozen
class Detection:
    r"""統計検出のサンプリング（[detection]。セクションごと省略可）.

    円・塗布痕の位置は 1 枚の画像では揺れるので、複数フレームの平均を 1 観測とする
    （:class:`~pcbasm.posctrl.OffsetObserver`）。枚数を増やすと平均のばらつきは
    :math:`1/\sqrt{n}` で下がるが、撮影時間はそのまま伸びる（30 fps なら 10 枚で約 0.33 秒）。

    ``minimum_sample_count`` は品質ゲートで、撮った枚数のうち何枚で対象を検出できれば
    その観測を採用するかを決める。検出枚数がこの値に届かなければ観測は失敗する。
    小さすぎると、たまたま写った 1 枚だけで位置を決めてしまう。

    この設定は、基準点の円（機械的なマーカー）とツールヘッドオフセットの塗布痕
    （濡れた円）の両方に適用される。両者は写りやすさが違う。一方だけ検出しにくい
    ときは、検出しやすい側にも同じ値が適用されることを踏まえて決める。

    Attributes:
        sample_count: 1 観測で撮るフレーム数
        minimum_sample_count: 1 観測に必要な有効検出数（``sample_count`` 以下）
    """

    sample_count: int = 10
    minimum_sample_count: int = 5

    def __attrs_post_init__(self) -> None:
        for name in ("sample_count", "minimum_sample_count"):
            if error := validate_positive_int(name, getattr(self, name)):
                raise ValueError(error)
        if self.minimum_sample_count > self.sample_count:
            raise ValueError(
                "minimum_sample_countはsample_count以下である必要があります: "
                f"minimum_sample_count={self.minimum_sample_count}, "
                f"sample_count={self.sample_count}"
            )


@attrs.frozen
class NozzleCap:
    """ノズルキャップ位置の設定.

    Attributes:
        position: キャップ位置（マシン座標 [mm]）。未教示なら None で、駐機は PRESENT へ落ちる
    """

    position: Point3d | None = None


@attrs.frozen
class NozzleClean:
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
    :func:`~pcbasm.pasting.nozzle_clean.validate_reach` が判定する。

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
class PasteDispenser:
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
    toolhead: Toolhead
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
    pad_align: PadAlign = attrs.field(factory=PadAlign)  # pad 位置合わせ設定
    flow_calibration: FlowCalibration = attrs.field(
        factory=FlowCalibration
    )  # 運転時流量キャリブレーション設定
    nozzle_cap: NozzleCap = attrs.field(factory=NozzleCap)  # タスク終了時の駐機先
    nozzle_clean: NozzleClean = attrs.field(factory=NozzleClean)  # ノズルクリーニング

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


def validate_probe_board_edge_margin(value: float) -> str | None:
    """基板外形からの probe 点マージンを検証する."""
    if value <= 0:
        return (
            "board_edge_marginは正の値である必要があります。"
            f"board_edge_margin={value}"
        )
    return None


@attrs.frozen
class Probe:
    """ロードセルプローブの設定."""

    min_radius: float  # サンプル点が銅箔境界から確保すべき最小距離 [mm] (ノズルが銅箔島の外に出ないためのクリアランス)
    board_edge_margin: float = 2.5  # サンプル点が基板外形から確保すべき最小距離 [mm]
    lift_height: float = 1.0  # PROBE 実行後に接触点から持ち上げる高さ [mm]
    min_samples: int = (
        6  # 最小サンプル数 (HeightPlane の 2 次曲面フィットに必要な最小点数)
    )
    max_samples: int = 9  # 最大サンプル数

    def __attrs_post_init__(self) -> None:
        if self.min_radius <= 0:
            raise ValueError(
                f"min_radiusは正の値である必要があります。min_radius={self.min_radius}"
            )
        if error := validate_probe_board_edge_margin(self.board_edge_margin):
            raise ValueError(error)
        if self.min_samples < 6:
            raise ValueError(
                f"min_samplesは6以上である必要があります。min_samples={self.min_samples}"
            )
        if self.min_samples > self.max_samples:
            raise ValueError(
                "min_samplesはmax_samples以下である必要があります。"
                f"min_samples={self.min_samples}, max_samples={self.max_samples}"
            )


@attrs.frozen
class CameraCrop:
    """カメラのクロップ設定."""

    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        """クロップサイズを (width, height) のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class Camera:
    """カメラの設定."""

    width: int
    height: int
    fps: float
    crop: CameraCrop
    calibration_file: Path
    device_id: int = 0
    format: str = "YUYV"
    backend: str = "csi"

    @property
    def size(self) -> tuple[int, int]:
        """カメラサイズを (width, height) のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class Toolhead:
    """ツールヘッドの設定."""

    x: float
    y: float

    def to_transform(self) -> Transform:
        """Toolhead の位置に xy 平行移動する Transform を返す."""
        return Shift(x=self.x, y=self.y)


class Corner(Enum):
    """ボードのコーナーを表す列挙型."""

    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()


@attrs.frozen
class CornerOffsets:
    """各コーナーにおけるボード端から基準点マーカーへのオフセット.

    Attributes:
        top_left: 左上コーナーのオフセット [x, y] (mm)
        top_right: 右上コーナーのオフセット [x, y] (mm)
        bottom_left: 左下コーナーのオフセット [x, y] (mm)
        bottom_right: 右下コーナーのオフセット [x, y] (mm)
    """

    top_left: tuple[float, float]
    top_right: tuple[float, float]
    bottom_left: tuple[float, float]
    bottom_right: tuple[float, float]

    def get(self, corner: Corner) -> Point2d:
        """指定コーナーのオフセットを Point2d で返す."""
        match corner:
            case Corner.TOP_LEFT:
                offset = self.top_left
            case Corner.TOP_RIGHT:
                offset = self.top_right
            case Corner.BOTTOM_LEFT:
                offset = self.bottom_left
            case Corner.BOTTOM_RIGHT:
                offset = self.bottom_right

        return Point2d(x=offset[0], y=offset[1])


@attrs.frozen
class ReferencePoint:
    """基準点の設定.

    x, y は左上基準点マーカーへ移動するための概略マシン座標。
    offsets は各ボードコーナーから対応する基準点マーカーへの PCB 座標系ベクトル。

    概略移動先の座標関係:
        - ボード左上コーナーの概略位置 = to_point() - offsets.get(TOP_LEFT)
        - 各コーナーの概略基準点位置 = ボードコーナーの概略位置 + offsets.get(corner)
    """

    x: float
    y: float
    target_diameter: float
    offsets: CornerOffsets

    def to_point(self) -> Point2d:
        """左上基準点マーカーの概略マシン座標を Point2d として返す."""
        return Point2d(self.x, self.y)

    def get_reference_position(
        self,
        corner: Corner = Corner.TOP_LEFT,
        *,
        board_width: float | None = None,
        board_height: float | None = None,
    ) -> Point2d:
        """指定コーナーの基準点マーカー位置を返す.

        Args:
            corner: コーナー種別
            board_width: ボード幅（TOP_RIGHT/BOTTOM_RIGHT で必須）
            board_height: ボード高さ（BOTTOM_LEFT/BOTTOM_RIGHT で必須）

        Returns:
            基準点マーカーへ移動するための概略マシン座標

        Raises:
            ValueError: 必要な board_width/board_height が指定されていない場合
        """
        if corner == Corner.TOP_LEFT:
            return self.to_point()

        board_origin = self.to_point() - self.offsets.get(Corner.TOP_LEFT)

        match corner:
            case Corner.TOP_RIGHT:
                if board_width is None:
                    raise ValueError("TOP_RIGHTを計算するときはboard_widthが必要です")
                board_corner = board_origin + Point2d(board_width, 0.0)
            case Corner.BOTTOM_LEFT:
                if board_height is None:
                    raise ValueError(
                        "BOTTOM_LEFTを計算するときはboard_heightが必要です"
                    )
                board_corner = board_origin + Point2d(0.0, board_height)
            case Corner.BOTTOM_RIGHT:
                if board_width is None:
                    raise ValueError(
                        "BOTTOM_RIGHTを計算するときはboard_widthが必要です"
                    )
                if board_height is None:
                    raise ValueError(
                        "BOTTOM_RIGHTを計算するときはboard_heightが必要です"
                    )
                board_corner = board_origin + Point2d(board_width, board_height)

        return board_corner + self.offsets.get(corner)


# [paste_dispenser] の外に書かれていた頃のノズル位置セクション名
LEGACY_NOZZLE_SECTIONS = ("nozzle_cap", "nozzle_clean")


def _merge_legacy_nozzle_sections(data: dict[str, Any]) -> None:
    """旧トップレベルのノズル位置を [paste_dispenser] 配下へ写す.

    設定ファイルを書き換えなくても動き続けるようにする（ファイル自体の移行は
    設定を書き込むときに ``ConfigStore`` が行う）。読み取り経路をここ 1 箇所に
    まとめるのは、``paste_dispenser`` 経由で読む消費者とノズル専用のアクセサで
    見える値が食い違わないようにするため。

    移行済みの値がある場合は、旧セクションに残った値で上書きしない。
    """
    for name in LEGACY_NOZZLE_SECTIONS:
        legacy = data.pop(name, None)
        if legacy is None:
            continue
        parent = data.setdefault("paste_dispenser", {})
        if isinstance(parent, dict):
            parent.setdefault(name, legacy)


def _structure_dispense_mode(value: object, _: object) -> DispenseMode:
    if isinstance(value, str) and value in DISPENSE_MODES:
        return value
    raise ValueError(f"未知の塗布方式です: {value!r}")


def _structure_line_direction(value: object, _: object) -> LineDirection:
    if isinstance(value, str) and value in LINE_DIRECTIONS:
        return value
    raise ValueError(f"未知の線走行方向です: {value!r}")


def _structure_machine_type(value: object, _: object) -> MachineType:
    if isinstance(value, str) and value in MACHINE_TYPES:
        return value
    raise ValueError(f"未知のマシン種別です: {value!r}")


def _structure_paste_height(value: object, _: object) -> PasteHeight:
    if value == "auto":
        return "auto"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"高さは'auto'または数値である必要があります: {value!r}")
    return float(value)


def _position_hook(
    cls: type[Any], converter: cattrs.Converter
) -> Callable[[Mapping[str, Any], object], Any]:
    """TOML の x / y / z を ``position`` の 1 組へ束ねる structure hook を作る.

    3 つそろえば教示済み、すべて無ければ未教示（None）。一部だけなら設定エラーにする。
    """
    structure = make_dict_structure_fn(cls, converter)

    def hook(value: Mapping[str, Any], _: object) -> Any:
        data = dict(value)
        axes = {axis: data.pop(axis) for axis in ("x", "y", "z") if axis in data}
        if axes:
            if len(axes) < 3:
                raise ValueError(
                    "座標は x / y / z の 3 つをそろえて書く必要があります"
                    f"（書かれているのは {', '.join(axes)} だけです）"
                )
            data["position"] = axes
        return structure(data, cls)

    return hook


def _make_converter() -> cattrs.Converter:
    converter = cattrs.Converter()
    converter.register_structure_hook_func(
        lambda t: t == DispenseMode, _structure_dispense_mode
    )
    converter.register_structure_hook_func(
        lambda t: t == LineDirection, _structure_line_direction
    )
    converter.register_structure_hook_func(
        lambda t: t == MachineType, _structure_machine_type
    )
    converter.register_structure_hook_func(
        lambda t: t == PasteHeight, _structure_paste_height
    )
    for cls in (NozzleCap, NozzleClean):
        converter.register_structure_hook(cls, _position_hook(cls, converter))
    return converter


_CONVERTER = _make_converter()


class ConfigError(Exception):
    """machine.toml を読めない・検証に通らない（WebAPI はこの間 degraded で動く）.

    Attributes:
        sections: 誤りのある節（``probe`` など、machine.toml の最上位の表やキーの名前）
    """

    def __init__(self, message: str, sections: frozenset[str] = frozenset()) -> None:
        super().__init__(message)
        self.sections = sections


@attrs.frozen
class Machine:
    """machine.toml 全体の設定（:func:`load_machine` が一括で検証して作る）."""

    machine_type: MachineType
    klipper: Klipper
    paste_dispenser: PasteDispenser
    camera: Camera
    reference_point: ReferencePoint
    probe: Probe
    audio: Audio = attrs.field(factory=Audio)
    settle: Settle = attrs.field(factory=Settle)
    detection: Detection = attrs.field(factory=Detection)


def _describe(exc: BaseException, _: object) -> str:
    if isinstance(exc, KeyError):
        return "必須の項目がありません"
    return str(exc) or type(exc).__name__


def _structure[T](value: object, cls: type[T]) -> T:
    """Machine.toml の値を ``cls`` へ structure する（不正なら全件を挙げた ConfigError）."""
    try:
        return _CONVERTER.structure(value, cls)
    except cattrs.BaseValidationError as exc:
        errors = cattrs.transform_error(exc, "machine.toml", _describe)
        # transform_error は 1 件を「理由 @ machine.toml.<節>.<キー>」の書式で返す
        sections = frozenset(
            error.rpartition(" @ machine.toml.")[2].split(".")[0] for error in errors
        )
        raise ConfigError(" / ".join(errors), sections) from exc


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} を TOML として読めません: {exc}") from exc


def parse_machine(data: Mapping[str, Any], config_dir: Path) -> Machine:
    """machine.toml の内容を全体で 1 回だけ検証して Machine にする.

    書き込む前の内容の検証にも使う。``camera.calibration_file`` は ``config_dir`` からの相対パスとして解決する。

    Raises:
        ConfigError: 検証に通らない場合（不正な項目をすべて挙げる）
    """
    data = copy.deepcopy(dict(data))
    _merge_legacy_nozzle_sections(data)
    machine = _structure(data, Machine)
    calibration_file = config_dir.resolve() / machine.camera.calibration_file
    return attrs.evolve(
        machine,
        camera=attrs.evolve(machine.camera, calibration_file=calibration_file),
    )


def load_machine(path: str | Path) -> Machine:
    """machine.toml をディスクから読み、全体を 1 回で検証する.

    Raises:
        ConfigError: TOML として読めない・検証に通らない場合
    """
    path = Path(path)
    return parse_machine(_read_toml(path), path.parent)


def load_klipper(path: str | Path) -> Klipper:
    """machine.toml の [klipper] 節だけを読む（設定エラーの間も緊急停止を送れるように）.

    Raises:
        ConfigError: TOML として読めない・[klipper] が無いか不正な場合
    """
    data = _read_toml(Path(path))
    if "klipper" not in data:
        raise ConfigError("machine.toml に [klipper] がありません")
    return _structure(data["klipper"], Klipper)


def get_config_dir() -> Path:
    """マシン設定ディレクトリを返す.

    ``PCBASM_CONFIG_DIR`` が設定されていればそれを、無ければ
    ``PROJECT_ROOT/config`` を返す。env は呼び出しごとに読む。

    Returns:
        マシン設定ディレクトリのパス
    """
    value = os.environ.get("PCBASM_CONFIG_DIR")
    return Path(value) if value else PROJECT_ROOT / "config"


def get_machine_config() -> Machine:
    """:func:`get_config_dir` の machine.toml を読み込む.

    Raises:
        ConfigError: TOML として読めない・検証に通らない場合
    """
    return load_machine(get_config_dir() / "machine.toml")
