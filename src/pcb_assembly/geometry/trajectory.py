from collections.abc import Iterable
from typing import Self

import attrs

from .transform import Point, Point2d, Point3d


def sort_by_nearest(positions: Iterable[Point3d], start: Point3d) -> list[Point3d]:
    """開始点から最も近い順に並べ替える（greedy nearest neighbor）.

    Args:
        positions: 並べ替える位置のシーケンス
        start: 開始点

    Returns:
        開始点から近い順に並べ替えた位置のリスト
    """
    if not positions:
        return []

    remaining = list(positions)
    result: list[Point3d] = []
    current = start

    while remaining:
        nearest_idx = min(
            range(len(remaining)), key=lambda i: (remaining[i] - current).norm()
        )
        nearest = remaining.pop(nearest_idx)
        result.append(nearest)
        current = nearest

    return result


@attrs.frozen
class Move:
    """移動指示を表す.

    Noneの座標は現在位置を維持する。 relative=Trueの場合は相対移動として扱う。
    """

    x: float | None = None
    y: float | None = None
    z: float | None = None
    v: float | None = None
    relative: bool = False

    @property
    def is_empty(self) -> bool:
        return (self.x, self.y, self.z, self.v) == (None, None, None, None)

    @classmethod
    def from_point(
        cls,
        point: Point,
        v: float | None = None,
        relative: bool = False,
    ) -> Self:
        """PointからMoveを生成する.

        Args:
            point: 座標（Point2dの場合はz=None）
            v: 速度
            relative: 相対移動フラグ

        Returns:
            Moveインスタンス
        """
        z = point.z if isinstance(point, Point3d) else None
        return cls(x=point.x, y=point.y, z=z, v=v, relative=relative)


@attrs.frozen
class Waypoint:
    """絶対座標での経由点を表す."""

    x: float
    y: float
    z: float
    v: float

    @property
    def position(self) -> Point3d:
        return Point3d(self.x, self.y, self.z)


class Trajectory:
    """軌道を管理するクラス.

    相対・絶対移動を含むMoveを受け取り、絶対座標のWaypointリストに変換する。
    """

    def __init__(self, origin: Point3d, default_velocity: float) -> None:
        self._position = origin
        self._velocity = default_velocity
        self._waypoints: list[Waypoint] = []

    @property
    def position(self) -> Point3d:
        """現在位置."""
        return self._position

    @property
    def velocity(self) -> float:
        """現在の速度."""
        return self._velocity

    @property
    def waypoints(self) -> list[Waypoint]:
        """経由点リスト."""
        return self._waypoints.copy()

    def add(self, *moves: Move, move: Iterable[Move] | None = None) -> None:
        """移動を追加する.

        Args:
            *moves: 追加するMove（可変長引数）
            move: 追加するMoveのイテラブル
        """
        all_moves = list(moves)
        if move is not None:
            all_moves.extend(move)

        for m in all_moves:
            if m.is_empty:
                continue

            waypoint = self._resolve(m)
            self._waypoints.append(waypoint)
            self._position = waypoint.position
            self._velocity = waypoint.v

    def _resolve(self, m: Move) -> Waypoint:
        """Moveを絶対座標のWaypointに変換する."""
        if m.relative:
            x = self._position.x + (m.x if m.x is not None else 0.0)
            y = self._position.y + (m.y if m.y is not None else 0.0)
            z = self._position.z + (m.z if m.z is not None else 0.0)
        else:
            x = m.x if m.x is not None else self._position.x
            y = m.y if m.y is not None else self._position.y
            z = m.z if m.z is not None else self._position.z

        v = m.v if m.v is not None else self._velocity

        return Waypoint(x=x, y=y, z=z, v=v)

    def distance(self) -> float:
        """軌道の総距離を返す."""
        if not self._waypoints:
            return 0.0

        total = 0.0
        prev = self._waypoints[0].position
        for wp in self._waypoints[1:]:
            total += (wp.position - prev).norm()
            prev = wp.position
        return total

    def time(self) -> float:
        """軌道の総所要時間を返す."""
        if not self._waypoints:
            return 0.0

        total = 0.0
        prev = self._waypoints[0].position
        for wp in self._waypoints[1:]:
            dist = (wp.position - prev).norm()
            total += dist / wp.v
            prev = wp.position
        return total
