"""Configを読み込む機能を実装するモジュール."""

from __future__ import annotations

import os
import tomllib
from enum import Enum, auto
from math import isfinite
from pathlib import Path
from typing import Any, Literal

import attrs
import cattrs

from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.utils import PROJECT_ROOT, is_finite_number

DISPENSE_MODES = ("auto", "dot", "line", "area")
DispenseMode = Literal["auto", "dot", "line", "area"]
LINE_DIRECTIONS = ("unconstrained", "outward", "inward")
LineDirection = Literal["unconstrained", "outward", "inward"]
MACHINE_TYPES = ("paste", "pnp")
MachineType = Literal["paste", "pnp"]
PasteHeight = float | Literal["auto"]
DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618
DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0
DEFAULT_AUDIO_DEVICE = "default"  # ALSAのシステム既定PCM
DEFAULT_AUDIO_VOLUME = 0.75


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
    """Klipperの設定."""

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
    max_passes: int = 5  # 1領域あたりの再計測上限
    converge_tolerance: float = 0.03  # 収束とみなす増分 [mm]
    max_correction: float = 1.0  # 1領域で許容する累積ずれ [mm]
    search_window: float = 2.0  # 照合の探索窓 片側幅 [mm]
    refine_max_short_side: float = 0.4  # pad別逐次位置合わせの最大短辺 [mm]
    canny_low: float = 100.0  # Cannyエッジ検出の下側閾値
    canny_high: float = 200.0  # Cannyエッジ検出の上側閾値
    blur_ksize: int = 5  # GaussianBlurカーネルサイズ (奇数)

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
    """0以上の有限値であるべき設定値を検証する."""
    if not is_finite_number(value) or value < 0:
        return f"{name}は0以上の有限値である必要があります: {value!r}"
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
    何点塗るかはその個数そのもので、0 個なら補正しない。

    1 点だけでは点ごとの吐出ばらつき（実測で相対 9〜11 %）がそのまま補正値に
    乗るので、3 点ほど置くとよい。

    ``crop_size_mm`` と測定位置の間隔の関係はここでは検証しない。

    WebUI は項目ごとに保存するので片方だけ先に書かれる。

    ここで撥ねると machine.toml 全体が読めなくなるため、判定は
    :func:`~pcbasm.pasting.paste_volume.runtime.plan_flow_calibration` で行う。

    塗り終えてすぐ撮ると、ペーストが広がりきる前の小さい円を測ることになる。
    塗布後の撮影に入る前に ``settle_seconds`` だけ置く。

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
class PasteDispenser:
    """ペーストディスペンサーの設定."""

    rotations_per_ul: float  # 1μLあたりの回転数 [rev/μL]
    nozzle_diameter: float  # ノズル内径 [mm]
    max_fill_speed: float  # 連続塗布できる移動速度上限 [mm/sec]
    max_dispense_rate: float  # 吐出レート上限 [μL/sec]
    dispense_accel: float  # 吐出加速度 [μL/sec²]
    retract_amount: float  # リトラクション量 [μL]
    retract_rate: float | None = attrs.field(
        default=None, kw_only=True
    )  # リトラクションレート [μL/sec]。未指定時は max_dispense_rate
    retract_accel_factor: float  # リトラクション加速度係数
    toolhead: Toolhead
    paste_height: PasteHeight  # 塗布面のZ高さ [mm]、または auto
    ul_per_mm2: float  # パッド面積あたりのペースト量 [μL/mm²]
    lift_height: float = 2.0  # 塗布後に持ち上げる高さ [mm]
    solder_paste_density: float = 3.78  # はんだペースト密度 [mg/μL] (S3X70-E150DN)
    dispense_mode: DispenseMode = "auto"  # 塗布方式 auto / dot / line / area
    line_direction: LineDirection = (
        "unconstrained"  # 線塗布の走行方向 unconstrained / outward / inward
    )
    auto_line_aspect_ratio: float = (
        DEFAULT_AUTO_LINE_ASPECT_RATIO  # Auto時に線塗布へ切り替える縦横比
    )
    auto_area_short_side_factor: float = (
        # Auto時に面塗布へ切り替える短辺のノズル径倍率（短辺 > nozzle_diameter * この値 → area）
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
    boundary_margin: float = 0.0  # 外周マージン [mm]
    pad_align: PadAlign = attrs.field(factory=PadAlign)  # pad位置合わせ設定
    flow_calibration: FlowCalibration = attrs.field(
        factory=FlowCalibration
    )  # 運転時流量キャリブレーション設定

    def __attrs_post_init__(self) -> None:
        if self.dispense_mode not in DISPENSE_MODES:
            raise ValueError(f"未知の塗布方式です: {self.dispense_mode}")
        if self.line_direction not in LINE_DIRECTIONS:
            raise ValueError(f"未知の線走行方向です: {self.line_direction}")
        if self.auto_line_aspect_ratio <= 1.0:
            raise ValueError(
                "auto_line_aspect_ratioは1.0より大きい必要があります: "
                f"{self.auto_line_aspect_ratio}"
            )
        if self.auto_area_short_side_factor <= 0:
            raise ValueError(
                "auto_area_short_side_factorは正の値である必要があります: "
                f"{self.auto_area_short_side_factor}"
            )
        if isinstance(self.paste_height, bool) or not isinstance(
            self.paste_height, (int, float, str)
        ):
            raise ValueError(f"paste_heightが不正です: {self.paste_height!r}")
        if isinstance(self.paste_height, str) and self.paste_height != "auto":
            raise ValueError(
                f"paste_heightは'auto'または数値である必要があります: "
                f"{self.paste_height!r}"
            )
        if isinstance(self.paste_height, (int, float)) and self.paste_height <= 0:
            raise ValueError(
                f"paste_heightは正の値である必要があります: {self.paste_height}"
            )
        if error := validate_paste_lift_height(self.lift_height):
            raise ValueError(error)
        if self.solder_paste_density <= 0:
            raise ValueError(
                "solder_paste_densityは正の値である必要があります: "
                f"{self.solder_paste_density}"
            )
        if isinstance(self.initial_purge_ul, bool) or self.initial_purge_ul < 0:
            raise ValueError(
                "initial_purge_ulは0以上の値である必要があります: "
                f"{self.initial_purge_ul}"
            )
        # 塗布ダイナミクス（FillSequence / PasteApplicator は検証済みとして使う）
        if self.nozzle_diameter <= 0:
            raise ValueError(
                f"nozzle_diameterは正の値である必要があります: {self.nozzle_diameter}"
            )
        if self.max_fill_speed <= 0:
            raise ValueError(
                f"max_fill_speedは正の値である必要があります: {self.max_fill_speed}"
            )
        if self.max_dispense_rate <= 0:
            raise ValueError(
                f"max_dispense_rateは正の値である必要があります: {self.max_dispense_rate}"
            )
        if self.retract_amount <= 0:
            raise ValueError(
                f"retract_amountは正の値である必要があります: {self.retract_amount}"
            )
        if self.retract_accel_factor <= 1.0:
            raise ValueError(
                "retract_accel_factorは1.0より大きい必要があります: "
                f"{self.retract_accel_factor}"
            )
        if self.bead_width_factor <= 0:
            raise ValueError(
                f"bead_width_factorは正の値である必要があります: {self.bead_width_factor}"
            )
        if not 0.0 <= self.overlap < 1.0:
            raise ValueError(f"overlapは[0,1)である必要があります: {self.overlap}")
        if self.boundary_margin < 0:
            raise ValueError(
                f"boundary_marginは0以上である必要があります: {self.boundary_margin}"
            )

    @property
    def density_mg_per_ul(self) -> float:
        """はんだペースト密度 [mg/μL]（TOML キー ``solder_paste_density`` のコード内名）."""
        return self.solder_paste_density

    @property
    def effective_retract_rate(self) -> float:
        """retract_rate 未指定時は max_dispense_rate を返す."""
        return (
            self.max_dispense_rate if self.retract_rate is None else self.retract_rate
        )


def validate_probe_board_edge_margin(value: float) -> str | None:
    """基板外形からのprobe点マージンを検証する."""
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
    lift_height: float = 1.0  # PROBE実行後に接触点から持ち上げる高さ [mm]
    min_samples: int = (
        6  # 最小サンプル数 (HeightPlaneの2次曲面フィットに必要な最小点数)
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
        """クロップサイズを(width, height)のタプルで返す."""
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
        """カメラサイズを(width, height)のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class Toolhead:
    """ツールヘッドの設定."""

    x: float
    y: float

    def to_transform(self) -> Transform:
        """Toolheadの位置にxy平行移動するTransformを返す."""
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
        """指定コーナーのオフセットをPoint2dで返す."""
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

    x, yは左上基準点マーカーへ移動するための概略マシン座標。
    offsetsは各ボードコーナーから対応する基準点マーカーへのPCB座標系ベクトル。

    概略移動先の座標関係:
        - ボード左上コーナーの概略位置 = to_point() - offsets.get(TOP_LEFT)
        - 各コーナーの概略基準点位置 = ボードコーナーの概略位置 + offsets.get(corner)
    """

    x: float
    y: float
    target_diameter: float
    offsets: CornerOffsets

    def to_point(self) -> Point2d:
        """左上基準点マーカーの概略マシン座標をPoint2dとして返す."""
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
            board_width: ボード幅（TOP_RIGHT/BOTTOM_RIGHTで必須）
            board_height: ボード高さ（BOTTOM_LEFT/BOTTOM_RIGHTで必須）

        Returns:
            基準点マーカーへ移動するための概略マシン座標

        Raises:
            ValueError: 必要なboard_width/board_heightが指定されていない場合
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


@attrs.frozen
class NozzleCap:
    """ノズルキャップ位置の設定（マシン座標 [mm]）."""

    x: float
    y: float
    z: float


@attrs.frozen
class NozzleClean:
    """ノズルクリーニング位置と動作の設定（マシン座標 [mm]）.

    塗布ジョブの開始時に ``(x, y)`` へ移動し、その場で ``purge_ul`` だけ押し出してから
    十字に往復してノズル先端をシリコンクリーナーへこすりつける。

    ``z`` はクリーニング面（シリコン表面）の高さで、ノズル先端が面に触れる位置を教示して
    記録する。実際にこする高さは ``press_z``（面から ``press_depth`` だけ押し込んだ位置）。
    面と押し込み量を分けてあるので、シリコンが摩耗したら ``press_depth`` だけ増やせばよい。

    座標は既定値を持たない。既定値があると設定画面から動作値だけを保存したときに座標の
    欠けたテーブルが読めてしまい、原点へクリーニングに行く事故になる。

    ``press_depth`` の上限はここで検証しない。妥当性は ``press_z`` が可動域に入るかでしか
    決まらず、それは printer.cfg 依存なので
    :func:`~pcbasm.pasting.nozzle_clean.validate_reach` が判定する。

    Attributes:
        x: クリーニング位置 X
        y: クリーニング位置 Y
        z: クリーニング面（シリコン表面）の Z 高さ
        press_depth: 面からの押し込み量。0 で押し込まない
        purge_ul: こすり前にその場で押し出す量 [uL]。0 でパージしない
        stroke: 十字往復の片振幅。0 でこすらない
        passes: 十字往復の反復回数。0 でこすらない
        wipe_speed: こすり移動速度 [mm/sec]
    """

    x: float
    y: float
    z: float
    press_depth: float = 0.5
    purge_ul: float = 0.2
    stroke: float = 2.0
    passes: int = 2
    wipe_speed: float = 10.0

    def __attrs_post_init__(self) -> None:
        for name in ("x", "y", "z"):
            if error := validate_finite_number(name, getattr(self, name)):
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

    @property
    def press_z(self) -> float:
        """こすり中の Z（面から押し込んだ絶対高さ）."""
        return self.z - self.press_depth


def _structure_dispense_mode(value: object, _: object) -> DispenseMode:
    if isinstance(value, str) and value in DISPENSE_MODES:
        return value
    raise ValueError(f"未知の塗布方式です: {value!r}")


def _structure_line_direction(value: object, _: object) -> LineDirection:
    if isinstance(value, str) and value in LINE_DIRECTIONS:
        return value
    raise ValueError(f"未知の線走行方向です: {value!r}")


def _structure_paste_height(value: object, _: object) -> PasteHeight:
    if value == "auto":
        return "auto"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"paste_heightは'auto'または数値である必要があります: {value!r}"
        )
    return float(value)


class Machine:
    """マシン設定をまとめるクラス.

    各設定はアクセス時に遅延生成される。
    """

    def __init__(self, path: str | Path) -> None:
        """Machineを初期化する.

        Args:
            path: TOMLファイルのパス
        """
        path = Path(path)
        with open(path, "rb") as f:
            self._data = tomllib.load(f)
        self._config_dir = path.parent.resolve()
        self._converter = cattrs.Converter()
        self._converter.register_structure_hook_func(
            lambda t: t == DispenseMode, _structure_dispense_mode
        )
        self._converter.register_structure_hook_func(
            lambda t: t == LineDirection, _structure_line_direction
        )
        self._converter.register_structure_hook_func(
            lambda t: t == PasteHeight, _structure_paste_height
        )

    def _get_config(self, key: str, cls: type[Any]) -> Any:
        """指定されたキーの設定を取得する."""
        if key not in self._data:
            raise KeyError(f"'{key}' は設定ファイルに定義されていません")
        return self._converter.structure(self._data[key], cls)

    @property
    def machine_type(self) -> MachineType:
        """マシン種別を取得する.

        Raises:
            KeyError: machine_type が設定ファイルに定義されていない場合
            ValueError: paste / pnp 以外の値の場合
        """
        if "machine_type" not in self._data:
            raise KeyError("'machine_type' は設定ファイルに定義されていません")
        value = self._data["machine_type"]
        if value not in MACHINE_TYPES:
            raise ValueError(f"未知のマシン種別です: machine_type={value!r}")
        return value

    @property
    def machine_name(self) -> str | None:
        """マシンの表示名を取得する（未設定なら None）.

        ``machine_type`` と同様に cattrs を通さず ``_data`` を直接読む。
        未設定時の代替名（ホスト名など）の解決は環境依存なので API 層に任せる。
        """
        value = self._data.get("machine_name")
        return value if isinstance(value, str) else None

    @property
    def nozzle_cap(self) -> NozzleCap | None:
        """ノズルキャップ位置設定を取得する（未記録なら None）."""
        if "nozzle_cap" not in self._data:
            return None
        return self._get_config("nozzle_cap", NozzleCap)

    @property
    def nozzle_clean(self) -> NozzleClean | None:
        """ノズルクリーニング位置設定を取得する（未記録なら None）."""
        if "nozzle_clean" not in self._data:
            return None
        return self._get_config("nozzle_clean", NozzleClean)

    @property
    def audio(self) -> Audio:
        """通知音の出力設定を取得する（[audio] 未設定・キー欠落は既定値）."""
        return self._converter.structure(self._data.get("audio", {}), Audio)

    @property
    def klipper(self) -> Klipper:
        """Klipper設定を取得する."""
        return self._get_config("klipper", Klipper)

    @property
    def paste_dispenser(self) -> PasteDispenser:
        """ペーストディスペンサー設定を取得する."""
        return self._get_config("paste_dispenser", PasteDispenser)

    @property
    def camera(self) -> Camera:
        """カメラ設定を取得する."""
        if "camera" not in self._data:
            raise KeyError("'camera' は設定ファイルに定義されていません")
        camera_data = self._data["camera"].copy()
        if "calibration_file" in camera_data and self._config_dir is not None:
            camera_data["calibration_file"] = (
                self._config_dir / camera_data["calibration_file"]
            )
        return self._converter.structure(camera_data, Camera)

    @property
    def reference_point(self) -> ReferencePoint:
        """基準点設定を取得する."""
        return self._get_config("reference_point", ReferencePoint)

    @property
    def probe(self) -> Probe:
        """プローブ設定を取得する."""
        return self._get_config("probe", Probe)


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
    """`config/` ディレクトリの machine.toml を読み込む.

    Returns:
        Machine設定オブジェクト
    """
    return Machine(get_config_dir() / "machine.toml")
