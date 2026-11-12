"""速度指令で動かす XYZ ステージ（pnp-motion ファーム）の抽象.

塗布機の Klipper 経路（:class:`~pcbasm.hal.stage.XYZStage`）とは別の HAL。速度を期限付きで送り続けて動かし、
原点出し・座標指定の移動・脱力・再起動と、ステージのロードセルの荷重の読み取りとタール、接触停止を持つ。実装は ``pcbasm.hal.mcu`` の MCU 版と
``pcbasm.hal.fake.FakeMotionStage``。

接触停止と接触保持（ロードセルがある機体）:

- **向き:** 部品実装機 kurousagi はステージが動き、ピンセットは動かない。ステージが上がるとピンセットに近づいて
  Z が減り（Z−）、下がると離れて Z が増える（Z+）。Z=0 が原点（原点出しは Z+ の向き）で、可動域は −40〜0 mm。
  押す向き（ピンセットに押し当たる向き）は設定で明示する（:attr:`ContactSettings.press_positive`。kurousagi は
  Z− なので False）。「原点出しの逆」と推し量らないのは、それが成り立たない機体がありうるため
- **接触停止:** 動いている間に荷重が ``trigger_force`` を超えたら、その場で止める。止まると「接触保持」に入り、
  :attr:`MotionStatus.contact` が真になる
- **接触保持:** 荷重が ``release_force`` 以上の間は、Z の離す向きにだけ動かせる（:func:`contact_velocity`、
  :func:`contact_move_error`）。X・Y と押す向き、原点出し、タールは受け付けない。荷重が ``release_force`` を
  下回ったら、次の操作で通常に戻る
- **離れた範囲のタール:** MCU 版は、Z がピンセットから離す向きの端から ``free_tare_zone`` [mm] 以内で止まっていれば、
  ステージに載せた基板などの荷重を差の大きさを問わず自動のタールに取り込む（原点を出していない Z と、接触保持の間は
  取り込まない）。範囲の外では ±2 g 以内だけ。fake は自動のタールをしないので持たない
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Collection
from contextlib import AbstractContextManager
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


@attrs.frozen(kw_only=True)
class ContactSettings:
    """ロードセルの接触停止の設定.

    Attributes:
        trigger_force: これを超える荷重 [g] で止める（押される向きだけを見る）
        release_force: 接触保持から戻る荷重 [g]（これを下回ったら戻る）
        press_positive: Z+ へ動くとピンセットに押し当たる（kurousagi は Z− で押し当たるので False）
    """

    trigger_force: float = 50.0
    release_force: float = 15.0
    press_positive: bool = False

    def __attrs_post_init__(self) -> None:
        if not self.trigger_force > 0:
            raise ValueError(f"trigger_force は正にする: {self.trigger_force}")
        if not self.release_force > 0:
            raise ValueError(f"release_force は正にする: {self.release_force}")
        if not self.release_force < self.trigger_force:
            raise ValueError(
                f"release_force（{self.release_force}）は trigger_force（{self.trigger_force}）より小さくする"
            )


class MotionState(enum.Enum):
    """ステージの状態.

    原点出しで MCU が接触停止の状態になっても、ここでは ``IDLE`` として扱う（速度指令で再開できる）。
    ロードセルの接触で止まったときも ``IDLE`` で、:attr:`MotionStatus.contact` が真になる。

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
        load: ステージに掛かる荷重 [g]（押される向きが正）。ロードセルが無いか、まだ読めていないか、
            読めないか、shutdown していれば None
        load_error: ロードセルが読めない理由（読めているか、ロードセルが無ければ None）
        contact: ロードセルの接触で止まり、まだ離していない（接触保持の間。モジュールの docstring）
    """

    position: Point3d
    velocity: Velocity
    state: MotionState
    homed: frozenset[Axis]
    shutdown_reason: str | None = None
    load: float | None = None
    load_error: str | None = None
    contact: bool = False


class MotionError(DeviceError):
    """ステージが指令を受け付けない（shutdown している、切断した、など）."""


class ContactStopped(MotionError):
    """原点出しか座標指定の移動の途中で、ロードセルの接触で止まった（接触保持に入った）."""


def release_direction(*, press_positive: bool) -> str:
    """接触から離す Z の向き（表示用。``Z+`` か ``Z−``）.

    Args:
        press_positive: Z+ へ動くとピンセットに押し当たる
    """
    return "Z−" if press_positive else "Z+"


def contact_velocity(velocity: Velocity, *, press_positive: bool) -> Velocity:
    """接触保持の間に送ってよい速度: X・Y は 0、Z は離す向きだけを残す.

    Args:
        velocity: 求められた速度 [mm/s]
        press_positive: Z+ へ動くとピンセットに押し当たる
    """
    releasing = velocity.z < 0 if press_positive else velocity.z > 0
    return Velocity(z=velocity.z if releasing else 0.0)


def contact_move_error(
    here: Point3d,
    x: float | None,
    y: float | None,
    z: float | None,
    *,
    press_positive: bool,
) -> str | None:
    """接触保持の間に、座標指定の移動を受け付けない理由（受け付けるなら None）.

    X・Y を指定したら受け付けない。Z は、今の位置から押す向きへ動かす行き先なら受け付けない。

    Args:
        here: 今の位置 [mm]
        x: X の行き先 [mm]（None なら動かさない）
        y: Y の行き先 [mm]
        z: Z の行き先 [mm]
        press_positive: Z+ へ動くとピンセットに押し当たる
    """
    if x is not None or y is not None:
        return "接触中は X・Y を動かせません。先に Z を離す向きに動かしてください"
    if z is not None and (z > here.z if press_positive else z < here.z):
        release = release_direction(press_positive=press_positive)
        return f"接触中は押す向きに動かせません。{release} で離してください"
    return None


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
        - :meth:`emergency_stop`・:meth:`status`・:meth:`tare`・:meth:`measure_load_counts`・
          :meth:`suspend_auto_tare` は、どのスレッドからでも
          呼べる。
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
            ValueError: 接触保持の間
            ContactStopped: 途中でロードセルの接触で止まった（その軸の原点は未のまま）
            MotionError: shutdown している、または原点出しに失敗した
        """

    @abstractmethod
    def set_velocity(self, velocity: Velocity) -> None:
        """速度を期限付きで 1 回送る.

        期限までに次の指令が来なければ速度は 0 になる。止まっているときに受けると動き出す。
        止まっているときに全軸 0 を受けたら、何もしない（動き出さず、励磁も入れない）。動いているときに全軸 0 を
        受けたら、:meth:`stop` と同じく止まるまで待つ（接触停止は動いている間だけ効く）。
        接触保持の間は :func:`contact_velocity` で絞ってから送る。

        Args:
            velocity: 各軸の速度 [mm/s]

        Raises:
            ValueError: 原点が未の軸に 0 以外を渡した
            MotionError: shutdown している、またはロードセルの異常で止まった
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
            ValueError: 可動域の外か、原点が未の軸を指定した。接触保持の間に :func:`contact_move_error` が拒んだ
            ContactStopped: 途中でロードセルの接触で止まった
            MotionError: shutdown している
        """

    @abstractmethod
    def relax(self) -> None:
        """止めて励磁を切る。原点は「未」に戻り、接触保持も消える.

        Raises:
            MotionError: shutdown している
        """

    @abstractmethod
    def keep_energized(self) -> AbstractContextManager[None]:
        """抜けるまで、励磁したまま止まっていても自動で脱力させない（入れ子にできる）.

        人の確認待ちなど、ジョブの途中で止まったまま待つ間に、原点を失ったり Z が落ちたりしないよう囲む。
        抜けると（例外で抜けても）、そこから設定の時間を数え直す。

        Raises:
            MotionError: 入るときに shutdown している
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
    def tare(self) -> None:
        """ロードセルの今の荷重を 0 にする（タール）.

        :meth:`status` と同じく、どのスレッドからでも呼べ、ほかの操作の終わりを待たない。

        Raises:
            ValueError: ロードセルが無い、値が来ていない、または接触保持の間
            MotionError: shutdown している、またはステージと話せない
        """

    @abstractmethod
    def measure_load_counts(self, seconds: float) -> float:
        """呼んでから届いたロードセルの値 ``seconds`` 秒ぶんの平均を、タール値との差 [counts] で返す（校正に使う）.

        押す向きが正（``sensor_orientation`` を反映する。荷重 [g] と同じ符号）。:meth:`tare` と同じく、
        どのスレッドからでも呼べ、ほかの操作の終わりを待たない（``seconds`` 秒ほど待つ）。

        Raises:
            MotionError: ロードセルが無い、値が来ない、途中で誤りのサンプルが来た、shutdown している、
                またはステージと話せない
        """

    @abstractmethod
    def suspend_auto_tare(self, suspended: bool) -> None:
        """自動のタールを止める・再び許す（校正の間、載せたおもりをタールに取り込まない）.

        接触保持の間に止めるのとは別に持つ（保持から戻っても止めたまま）。:meth:`tare` と同じく、どのスレッドからでも
        呼べる。shutdown や接続が切れた後でも例外を出さない（校正の後始末で呼ぶため）。ロードセルが無ければ何もしない。
        """

    @abstractmethod
    def close(self) -> None:
        """接続を閉じる."""
