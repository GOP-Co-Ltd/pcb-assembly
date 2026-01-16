from collections.abc import Iterable

import attrs

from .transform import Position


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


@attrs.frozen
class Waypoint:
    """絶対座標での経由点を表す."""

    x: float
    y: float
    z: float
    v: float

    @property
    def position(self) -> Position:
        return Position(self.x, self.y, self.z)


class Trajectory:
    """軌道を管理するクラス.

    相対・絶対移動を含むMoveを受け取り、絶対座標のWaypointリストに変換する。
    """

    def __init__(self, origin: Position, default_velocity: float) -> None:
        self._position = origin
        self._velocity = default_velocity
        self._waypoints: list[Waypoint] = []

    @property
    def position(self) -> Position:
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
