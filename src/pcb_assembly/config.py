"""Configを読み込む機能を実装するモジュール."""

from __future__ import annotations

import tomllib
from enum import Enum, auto
from pathlib import Path
from typing import Any

import attrs
import cattrs

from pcb_assembly.geometry import Point2d


@attrs.frozen
class Klipper:
    """Klipperの設定."""

    host: str = "localhost"
    port: int = 7125


@attrs.frozen
class Probe:
    """プローブの設定."""

    a_pin: int
    b_pin: int
    rotation_pulse: int
    rotation_distance: float
    inverse: bool = False


@attrs.frozen
class PasteDispenser:
    """ペーストディスペンサーの設定."""

    syringe_size: float
    nozzle_size: str


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

    @property
    def size(self) -> tuple[int, int]:
        """カメラサイズを(width, height)のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class Toolhead:
    """ツールヘッドの設定."""

    x: float
    y: float


class Corner(Enum):
    """ボードのコーナーを表す列挙型."""

    TOP_LEFT = auto()
    TOP_RIGHT = auto()


@attrs.frozen
class ReferencePoint:
    """基準点の設定.

    x, yは左上基準点マーカーのマシン座標。
    offset_x, offset_yは基準点マーカーからボードコーナーへのオフセット（正値）。

    座標関係:
        - ボード左上コーナー = (x + offset_x, y + offset_y)
        - 右上基準点 = (x + 2*offset_x + board_width, y) （対称配置を仮定）
    """

    x: float
    y: float
    offset_x: float
    offset_y: float
    target_diameter: float

    def to_point(self) -> Point2d:
        """基準点座標をPoint2dとして返す."""
        return Point2d(self.x, self.y)

    def get_reference_position(
        self, corner: Corner = Corner.TOP_LEFT, *, board_width: float | None = None
    ) -> Point2d:
        """指定コーナーの基準点マーカー位置を返す.

        Args:
            corner: コーナー種別
            board_width: ボード幅（TOP_RIGHTの場合は必須）

        Returns:
            基準点マーカーのマシン座標

        Raises:
            ValueError: TOP_RIGHTでboard_widthが指定されていない場合
        """
        match corner:
            case Corner.TOP_LEFT:
                return self.to_point()
            case Corner.TOP_RIGHT:
                if board_width is None:
                    raise ValueError("TOP_RIGHTを計算するときはboard_widthが必要です。")
                return Point2d(self.x + 2 * self.offset_x + board_width, self.y)

    def offset_from_board(self, corner: Corner = Corner.TOP_LEFT) -> Point2d:
        """ボードコーナーから見た基準点マーカーへのオフセット.

        Args:
            corner: コーナー種別

        Returns:
            ボードコーナーから基準点マーカーへのオフセット
        """
        match corner:
            case Corner.TOP_LEFT:
                return Point2d(-self.offset_x, -self.offset_y)
            case Corner.TOP_RIGHT:
                return Point2d(self.offset_x, -self.offset_y)


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
    def probe(self) -> Probe:
        """プローブ設定を取得する."""
        return self._get_config("probe", Probe)

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
    def toolhead(self) -> Toolhead:
        """ツールヘッド設定を取得する."""
        return self._get_config("toolhead", Toolhead)

    @property
    def reference_point(self) -> ReferencePoint:
        """基準点設定を取得する."""
        return self._get_config("reference_point", ReferencePoint)
