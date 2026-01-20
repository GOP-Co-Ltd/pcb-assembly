"""Configを読み込む機能を実装するモジュール."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Self

import attrs
import cattrs


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


@attrs.frozen
class Camera:
    """カメラの設定."""

    width: int
    height: int
    fps: float
    crop: CameraCrop
    device_id: int = 0
    format: str = "YUYV"


@attrs.frozen
class Toolhead:
    """ツールヘッドの設定."""

    x: float
    y: float


@attrs.frozen
class ReferencePoint:
    """基準点の設定."""

    x: float
    y: float
    offset_x: float
    offset_y: float
    target_diameter: float


class Machine:
    """マシン設定をまとめるクラス.

    各設定はアクセス時に遅延生成される。
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Machineを初期化する.

        Args:
            data: TOMLから読み込んだ辞書データ
        """
        self._data = data.copy()
        self._converter = cattrs.Converter()

    @classmethod
    def from_toml(cls, path: str | Path) -> Self:
        """TOMLファイルからMachineを生成する.

        Args:
            path: TOMLファイルのパス

        Returns:
            Machine インスタンス
        """
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(data)

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
        return self._get_config("camera", Camera)

    @property
    def toolhead(self) -> Toolhead:
        """ツールヘッド設定を取得する."""
        return self._get_config("toolhead", Toolhead)

    @property
    def reference_point(self) -> ReferencePoint:
        """基準点設定を取得する."""
        return self._get_config("reference_point", ReferencePoint)
