"""Configを読み込む機能を実装するモジュール."""

from __future__ import annotations

import tomllib
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal

import attrs
import cattrs

from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.utils import PROJECT_ROOT

DISPENSE_MODES = ("auto", "dot", "line", "area")
DispenseMode = Literal["auto", "dot", "line", "area"]
PasteHeight = float | Literal["auto"]
DEFAULT_AUTO_LINE_ASPECT_RATIO = 1.618


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


@attrs.frozen
class PasteDispenser:
    """ペーストディスペンサーの設定."""

    rotations_per_ul: float  # 1μLあたりの回転数 [rev/μL]
    nozzle_diameter: float  # ノズル内径 [mm]
    fill_speed: float  # 塗布移動速度 [mm/sec]（主設定）
    max_dispense_rate: float  # 吐出レート上限 [μL/sec]
    dispense_accel: float  # 吐出加速度 [μL/sec²]
    retract_amount: float  # リトラクション量 [μL]
    retract_rate: float  # リトラクションレート [μL/sec]
    retract_accel_factor: float  # リトラクション加速度係数
    toolhead: Toolhead
    paste_height: PasteHeight  # 塗布面のZ高さ [mm]、または auto
    ul_per_mm2: float  # パッド面積あたりのペースト量 [μL/mm²]
    solder_paste_density: float = 3.78  # はんだペースト密度 [mg/μL] (S3X70-E150DN)
    dispense_mode: DispenseMode = "auto"  # 塗布方式 auto / dot / line / area
    auto_line_aspect_ratio: float = (
        DEFAULT_AUTO_LINE_ASPECT_RATIO  # Auto時に線塗布へ切り替える縦横比
    )
    prime_extra_delay: float = 0.0  # プライム後の追加遅延 [sec]
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


@attrs.frozen
class Probe:
    """電気接触式プローブの設定."""

    servo_name: str  # printer.cfgの[servo <name>]のname部分
    revolution_distance: float  # サーボ一回転あたりの移動量 [mm]
    down_distance: float  # グラウンドを下げる距離 [mm]
    min_radius: float  # サンプル点が銅箔境界から確保すべき最小距離 [mm] (ニードル-probe ground間の目測距離に相当)
    lift_height: float = 1.0  # PROBE実行後に接触点から持ち上げる高さ [mm]
    min_samples: int = (
        6  # 最小サンプル数 (HeightPlaneの2次曲面フィットに必要な最小点数)
    )
    max_samples: int = 9  # 最大サンプル数
    shift: tuple[float, float] = (
        0.0,
        0.0,
    )  # プローブ点のヒューリスティックなシフト量 [x, y] (mm, マシン座標系)

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

    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()


@attrs.frozen
class CornerOffsets:
    """各コーナーにおけるボード端から基準点マーカーへのオフセット.

    top_leftは必須。それ以外は少なくとも1つ指定する必要がある。

    Attributes:
        top_left: 左上コーナーのオフセット [x, y] (mm)
        top_right: 右上コーナーのオフセット [x, y] (mm)
        bottom_left: 左下コーナーのオフセット [x, y] (mm)
        bottom_right: 右下コーナーのオフセット [x, y] (mm)
    """

    top_left: tuple[float, float]
    top_right: tuple[float, float] | None = None
    bottom_left: tuple[float, float] | None = None
    bottom_right: tuple[float, float] | None = None

    def __attrs_post_init__(self) -> None:
        if (self.top_right, self.bottom_left, self.bottom_right).count(None) >= 2:
            raise ValueError(
                "top_left以外に少なくとも2つのコーナーオフセットを指定してください"
            )

    def has_corner(self, corner: Corner) -> bool:
        """指定コーナーのオフセットが定義されているか返す."""
        match corner:
            case Corner.TOP_LEFT:
                return True
            case Corner.TOP_RIGHT:
                return self.top_right is not None
            case Corner.BOTTOM_LEFT:
                return self.bottom_left is not None
            case Corner.BOTTOM_RIGHT:
                return self.bottom_right is not None

    def get(self, corner: Corner) -> Point2d:
        """指定コーナーのオフセットをPoint2dで返す.

        Raises:
            ValueError: 指定コーナーのオフセットが未定義の場合
        """
        match corner:
            case Corner.TOP_LEFT:
                offset = self.top_left
            case Corner.TOP_RIGHT:
                offset = self.top_right
            case Corner.BOTTOM_LEFT:
                offset = self.bottom_left
            case Corner.BOTTOM_RIGHT:
                offset = self.bottom_right

        if offset is None:
            raise ValueError(f"{corner.name}のオフセットは定義されていません")
        return Point2d(x=offset[0], y=offset[1])


@attrs.frozen
class ReferencePoint:
    """基準点の設定.

    x, yは左上基準点マーカーのマシン座標。
    offsetsは各コーナーにおけるボード端から基準点マーカーへのオフセット。

    座標関係:
        - ボード左上コーナー = to_point() - offsets.get(TOP_LEFT)
        - 各コーナーの基準点 = ボードコーナー + offsets.get(corner)
    """

    x: float
    y: float
    target_diameter: float
    offsets: CornerOffsets

    def to_point(self) -> Point2d:
        """左上基準点マーカーのマシン座標をPoint2dとして返す."""
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
            基準点マーカーのマシン座標

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
