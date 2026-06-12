"""API 境界で共有する pydantic モデル."""

from __future__ import annotations

from pydantic import BaseModel


class Position(BaseModel):
    """ステージ座標 [mm]."""

    x: float
    y: float
    z: float


class KlipperStatus(BaseModel):
    """Klipper の接続状態とステージ状態."""

    connected: bool
    position: Position | None = None
    homed_axes: str | None = None
    error: str | None = None
