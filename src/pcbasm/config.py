"""Configを読み込む機能を実装するモジュール."""

from __future__ import annotations

import tomllib
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import attrs
import cattrs

from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.utils import PROJECT_ROOT

DISPENSE_MODES = ("auto", "dot", "line", "area")
DispenseMode = Literal["auto", "dot", "line", "area"]
MACHINE_TYPES = ("paste", "pnp")
MachineType = Literal["paste", "pnp"]
PasteHeight = float | Literal["auto"]
DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618
DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0


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


@attrs.frozen
class PadAlign:
    """pad単位の銅箔照合による位置合わせの設定."""

    tolerance: float = 0.05  # 収束許容誤差 [mm]
    max_correction: float = 1.0  # 1回の照合で許容する最大ずれ [mm]。超過は照合失敗
    search_window: float = 2.0  # 照合の探索窓 片側幅 [mm]
    roi_margin: float = 1.0  # pad ROIのマージン [mm]
    min_roi: float = 3.0  # pad ROIの最小辺長 [mm]
    theta_range: float = 2.0  # 回転探索の片側範囲 [deg]
    canny_low: float = 100.0  # Cannyエッジ検出の下側閾値
    canny_high: float = 200.0  # Cannyエッジ検出の上側閾値
    blur_ksize: int = 5  # GaussianBlurカーネルサイズ (奇数)
    max_failures: int = 0  # 照合失敗の許容部品数。超過で塗布ジョブを即中止

    def __attrs_post_init__(self) -> None:
        if isinstance(self.max_failures, bool) or self.max_failures < 0:
            raise ValueError(
                f"max_failuresは0以上の整数である必要があります: {self.max_failures}"
            )


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
    solder_paste_density: float = 3.78  # はんだペースト密度 [mg/μL] (S3X70-E150DN)
    dispense_mode: DispenseMode = "auto"  # 塗布方式 auto / dot / line / area
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
    air_pump_enabled: bool = True  # エアポンプの有効/無効
    pad_align: PadAlign = attrs.field(factory=PadAlign)  # pad位置合わせ設定

    def __attrs_post_init__(self) -> None:
        if self.dispense_mode not in DISPENSE_MODES:
            raise ValueError(f"未知の塗布方式です: {self.dispense_mode}")
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

    @property
    def effective_retract_rate(self) -> float:
        """retract_rate 未指定時は max_dispense_rate を返す."""
        return (
            self.max_dispense_rate if self.retract_rate is None else self.retract_rate
        )


@attrs.frozen
class Probe:
    """ロードセルプローブの設定."""

    min_radius: float  # サンプル点が銅箔境界から確保すべき最小距離 [mm] (ノズルが銅箔島の外に出ないためのクリアランス)
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

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"

    def board_position(self, width: float, height: float) -> Point2d:
        """基板座標系（左上原点）でのコーナー位置を返す.

        Args:
            width: 基板幅 (mm)
            height: 基板高さ (mm)
        """
        match self:
            case Corner.TOP_LEFT:
                return Point2d(0.0, 0.0)
            case Corner.TOP_RIGHT:
                return Point2d(width, 0.0)
            case Corner.BOTTOM_LEFT:
                return Point2d(0.0, height)
            case Corner.BOTTOM_RIGHT:
                return Point2d(width, height)


CORNERS = tuple(corner.value for corner in Corner)


@attrs.frozen
class ReferencePoint:
    """基準点マーカーの設定.

    x, yはアンカーコーナーの基準点マーカーのおおよそのマシン座標。
    cornerはマーカーを置くアンカーコーナー、offsetは基板コーナーから マーカーへのオフセット（マーカー位置 = 基板コーナー +
    offset）。
    """

    x: float
    y: float
    target_diameter: float
    offset: tuple[float, float]
    corner: Corner = Corner.TOP_LEFT

    def to_point(self) -> Point2d:
        """基準点マーカーのマシン座標をPoint2dとして返す."""
        return Point2d(self.x, self.y)

    def offset_point(self) -> Point2d:
        """基板コーナー→マーカーのオフセットをPoint2dとして返す."""
        return Point2d(x=self.offset[0], y=self.offset[1])


@attrs.frozen
class BoardAlign:
    """基板コーナーの輪郭照合によるboard変換計測の設定."""

    tolerance: float = 0.05  # 各コーナーサーボの収束許容誤差 [mm]
    max_correction: float = 2.0  # 照合ずれの上限 [mm]。超過は誤マッチとして棄却
    search_window: float = 1.5  # 照合の探索窓 片側幅 [mm]
    edge_length: float = 2.0  # コーナーROIの片側辺長 = 含める外形エッジ長 [mm]
    theta_range: float = 2.0  # 回転探索の片側範囲 [deg]
    canny_low: float = 100.0  # Cannyエッジ検出の下側閾値
    canny_high: float = 200.0  # Cannyエッジ検出の上側閾値
    blur_ksize: int = 5  # GaussianBlurカーネルサイズ (奇数)


@attrs.frozen
class NozzleCap:
    """ノズルキャップ位置の設定（マシン座標 [mm]）."""

    x: float
    y: float
    z: float


def _structure_dispense_mode(value: object, _: object) -> DispenseMode:
    if isinstance(value, str) and value in DISPENSE_MODES:
        return value
    raise ValueError(f"未知の塗布方式です: {value!r}")


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
    def nozzle_cap(self) -> NozzleCap | None:
        """ノズルキャップ位置設定を取得する（未記録なら None）."""
        if "nozzle_cap" not in self._data:
            return None
        return self._get_config("nozzle_cap", NozzleCap)

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
    def board_align(self) -> BoardAlign:
        """基板コーナー照合設定を取得する（節欠落時は既定値）."""
        if "board_align" not in self._data:
            return BoardAlign()
        return self._get_config("board_align", BoardAlign)

    @property
    def probe(self) -> Probe:
        """プローブ設定を取得する."""
        return self._get_config("probe", Probe)


def get_machine_config(name: str, file: str = "machine.toml") -> Machine:
    """マシン名からMachine設定を読み込む.

    Args:
        name: マシン名（configs/ディレクトリ下のサブディレクトリ名）
        file: 設定ファイル名

    Returns:
        Machine設定オブジェクト
    """
    path = PROJECT_ROOT / "configs" / name / file
    return Machine(path)
