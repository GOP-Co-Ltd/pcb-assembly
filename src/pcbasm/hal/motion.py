"""速度指令で動かす XYZ ステージ（pnp-motion ファーム）の抽象.

塗布機の Klipper 経路（:class:`~pcbasm.hal.stage.XYZStage`）とは別の HAL。速度を期限付きで送り続けて動かし、
原点出し・座標指定の移動・脱力・再起動を持つ。実装は ``pcbasm.hal.mcu`` の MCU 版と
``pcbasm.hal.fake.FakeMotionStage``。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Literal

import attrs

from pcbasm.errors import DeviceError
from pcbasm.geometry import Point3d

from .stage import Limits

type Axis = Literal["x", "y", "z"]

# 全軸の原点出しの順。kurousagi はノズルが引っかかるので Z を先に出す
HOMING_ORDER: tuple[Axis, ...] = ("z", "x", "y")


@attrs.frozen
class Velocity:
    """各軸の速度 [mm/s]."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class MotionState(enum.Enum):
    """ステージの状態.

    原点出しで MCU が接触停止の状態になっても、ここでは ``IDLE`` として扱う（速度指令で再開できる）。

    Attributes:
        IDLE: 止まっている。速度指令を受けると動き出す
        MOVING: 速度指令を受けて動いている（期限が切れて速度 0 のまま流れている場合も含む）
        SHUTDOWN: MCU が shutdown した。:meth:`MotionStage.restart` まで何も受け付けない
    """

    IDLE = enum.auto()
    MOVING = enum.auto()
    SHUTDOWN = enum.auto()


@attrs.frozen
class MotionStatus:
    """ステージの状態の読み出し.

    Attributes:
        position: 位置 [mm]（原点が未の軸は意味を持たない）
        velocity: 今の速度 [mm/s]
        state: 状態
        homed: 原点出しを済ませた軸
        shutdown_reason: shutdown した理由（shutdown していなければ None）
    """

    position: Point3d
    velocity: Velocity
    state: MotionState
    homed: frozenset[Axis]
    shutdown_reason: str | None = None


class MotionError(DeviceError):
    """ステージが指令を受け付けない（shutdown している、切断した、など）."""


def homing_order(axes: Collection[Axis], homed: Collection[Axis]) -> tuple[Axis, ...]:
    """原点出しをする軸を、出す順に並べる.

    空なら全軸。Z の原点が未のまま X・Y を求められたら、Z を先に足す。

    Args:
        axes: 原点出しを求められた軸
        homed: 原点出しを済ませた軸

    Returns:
        :data:`HOMING_ORDER` の順に並べた軸
    """
    wanted = set(axes or HOMING_ORDER)
    if "z" not in homed:
        wanted.add("z")
    return tuple(axis for axis in HOMING_ORDER if axis in wanted)


class MotionStage(ABC):
    """速度指令で動かす XYZ ステージ.

    スレッドの約束:
        - :meth:`emergency_stop` と :meth:`status` は、どのスレッドからでも即座に呼べる。
          ほかの操作（:meth:`home` や :meth:`move_to` の待ち）の終わりを待たない
        - ほかの操作は 1 つずつ実行する（同時に呼ばれたら、先の操作が終わるまで待つ）
    """

    @property
    @abstractmethod
    def limits(self) -> Limits:
        """各軸の可動域と、座標指定の移動の速度の範囲."""

    @property
    @abstractmethod
    def max_velocity(self) -> Velocity:
        """各軸の最高速度 [mm/s]."""

    @abstractmethod
    def status(self) -> MotionStatus:
        """今の状態を返す（定期報告の最新値を読むだけで、操作の終わりを待たない）.

        Raises:
            MotionError: ステージと話せない
        """

    @abstractmethod
    def home(self, axes: Collection[Axis] = ()) -> None:
        """原点出しをする（終わるまで待つ）.

        軸の順は :func:`homing_order` に従う。空なら全軸、Z の原点が未なら Z を先に出す。
        途中でジョブを中止しても止まらない。止めるのは :meth:`emergency_stop` だけ。

        Args:
            axes: 原点出しをする軸

        Raises:
            MotionError: shutdown している、または原点出しに失敗した
        """

    @abstractmethod
    def set_velocity(self, velocity: Velocity) -> None:
        """速度を期限付きで 1 回送る.

        期限までに次の指令が来なければ速度は 0 になる。止まっているときに受けると動き出す。
        止まっているときに全軸 0 を受けたら、何もしない（動き出さず、励磁も入れない）。

        Args:
            velocity: 各軸の速度 [mm/s]

        Raises:
            ValueError: 原点が未の軸に 0 以外を渡した
            MotionError: shutdown している
        """

    @abstractmethod
    def stop(self) -> None:
        """減速して止め、止まるまで待つ.

        Raises:
            MotionError: shutdown している
        """

    @abstractmethod
    def move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        *,
        speed: float | None = None,
    ) -> None:
        """指定した軸を座標へ動かし、着くまで待つ（None の軸は動かさない）.

        途中でジョブを中止しても止まらない。止めるのは :meth:`emergency_stop` だけ。

        Args:
            x: X の行き先 [mm]
            y: Y の行き先 [mm]
            z: Z の行き先 [mm]
            speed: 速度 [mm/s]（None なら実装の既定）

        Raises:
            ValueError: 可動域の外か、原点が未の軸を指定した
            MotionError: shutdown している
        """

    @abstractmethod
    def relax(self) -> None:
        """止めて励磁を切る。原点は「未」に戻る.

        Raises:
            MotionError: shutdown している
        """

    @abstractmethod
    def restart(self) -> None:
        """MCU を reset して設定し直す。shutdown から戻り、原点は「未」になる.

        Raises:
            MotionError: ステージと話せない
        """

    @abstractmethod
    def emergency_stop(self) -> None:
        """即座に止める（MCU は shutdown し、:meth:`restart` まで何も受け付けない）.

        Raises:
            MotionError: ステージと話せない
        """

    @abstractmethod
    def close(self) -> None:
        """接続を閉じる."""
