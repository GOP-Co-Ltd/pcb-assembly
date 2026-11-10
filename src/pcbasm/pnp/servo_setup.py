"""ピンセットのサーボの役割判別と開閉の範囲.

2 台のサーボを 1 台ずつ動かして、人がどちらが Rotation / Open Close かを答える。取り付けたばかりの
サーボは役割に合うモードとは限らないので、役割が決まったら合わないサーボにモードを書く。次に Open Close のトルクを切り、人が手で閉じ切り・開き切りにした位置を読む。
ここには判別と範囲の検証だけを置き、人への問い合わせはジョブ（``web.api.jobs.pnp.servo_setup``）が担う。

人の答えが使えないとき（同じ役割を 2 回選んだ、範囲が狭い）は戻り値で知らせ、ジョブが尋ね直す。
:class:`ServoSetupError` は、人が答え直しても先へ進めない失敗（2 台見つからない、モードを書けない）に使う。
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping

import attrs

from pcbasm.config.pnp import open_close_span_error
from pcbasm.hal import ServoBus, ServoMode, scan, wait_stopped
from pcbasm.hal.servo import STEPS_PER_REV, position_to_degrees

# 開き量がこれ未満なら、手で開き切りにし損ねたとみなす [deg]
MIN_SPAN_DEGREES = 3.0
# 設定に書く角度の小数の桁数（0.01° は 1 step = 0.088° より細かいので、読み戻すと同じ位置になる）
_ANGLE_DIGITS = 2


class ServoSetupError(ValueError):
    """サーボの組や役割が使えず、先へ進めない（文言は利用者にそのまま見せる）."""


class ServoRole(enum.StrEnum):
    """ピンセットのサーボの役割."""

    ROTATION = "rotation"
    OPEN_CLOSE = "open_close"

    @property
    def label(self) -> str:
        """表示名."""
        return _LABELS[self]

    @property
    def mode(self) -> ServoMode:
        """この役割に要るサーボのモード."""
        return _MODES[self]


_LABELS = {
    ServoRole.ROTATION: "Rotation（回転）",
    ServoRole.OPEN_CLOSE: "Open Close（開閉）",
}
_MODES = {ServoRole.ROTATION: ServoMode.STEP, ServoRole.OPEN_CLOSE: ServoMode.POSITION}


def role_of_mode(mode: ServoMode) -> ServoRole:
    """そのモードで使う役割（人に尋ねるときの既定の答え）."""
    return next(role for role in ServoRole if role.mode is mode)


def find_pair(bus: ServoBus, ids: Iterable[int]) -> tuple[int, int]:
    """Ids の中で応答した 2 台の ID を返す.

    Raises:
        ServoSetupError: 応答したのがちょうど 2 台でない場合
    """
    found = scan(bus, ids)
    if len(found) != 2:
        listed = "、".join(map(str, found)) or "なし"
        raise ServoSetupError(
            f"応答したサーボが 2 台ではありません（{len(found)} 台: {listed}）。"
            "2 台を別々の ID にして、両方をつないでください"
        )
    return found[0], found[1]


@attrs.frozen
class ServoRoles:
    """どの ID がどの役割か.

    Attributes:
        rotation_id: Rotation のサーボ ID
        open_close_id: Open Close のサーボ ID
    """

    rotation_id: int
    open_close_id: int

    def id_of(self, role: ServoRole) -> int:
        """その役割のサーボ ID."""
        return self.rotation_id if role is ServoRole.ROTATION else self.open_close_id


def roles_from_answers(answers: Mapping[int, ServoRole]) -> ServoRoles | None:
    """2 台それぞれに人が答えた役割から、役割の割り当てを作る.

    Returns:
        役割の割り当て。両方の役割がそろっていない（同じ役割を 2 回選んだ）なら None
    """
    by_role = {role: servo_id for servo_id, role in answers.items()}
    if len(answers) != 2 or len(by_role) != 2:
        return None
    return ServoRoles(
        rotation_id=by_role[ServoRole.ROTATION],
        open_close_id=by_role[ServoRole.OPEN_CLOSE],
    )


def apply_modes(bus: ServoBus, roles: ServoRoles) -> tuple[int, ...]:
    """役割とモードが合わないサーボに、役割のモードを書く.

    書く前にそのサーボのトルクを切る（モードが変わると goal の意味が変わるため）。

    Returns:
        モードを書いたサーボの ID（役割の順。合っていれば空）

    Raises:
        ServoSetupError: 書いたのに読み戻すと役割のモードになっていない場合
    """
    written: list[int] = []
    for role in ServoRole:
        servo_id = roles.id_of(role)
        if bus.mode(servo_id) is role.mode:
            continue
        bus.set_torque(servo_id, False)
        bus.set_mode(servo_id, role.mode)
        if (mode := bus.mode(servo_id)) is not role.mode:
            raise ServoSetupError(
                f"{role.label} の ID {servo_id} にモード {role.mode.name}（{role.mode.value}）を書きましたが、"
                f"読み戻すと {mode.name}（{mode.value}）です"
            )
        written.append(servo_id)
    return tuple(written)


@attrs.frozen
class OpenCloseRange:
    """Open Close の閉じ切り・開き切りの位置（raw 0〜4095）.

    Attributes:
        closed_position: 閉じ切りの位置
        open_position: 開き切りの位置（閉じより小さくてもよい）
    """

    closed_position: int
    open_position: int

    def validate(self) -> str | None:
        """使えない範囲ならその理由（使えるなら None）.

        設定と同じ規則（同じ角度は不可、360°/0° の境目をまたがない。
        :func:`pcbasm.config.pnp.open_close_span_error`）に加え、開き量が
        :data:`MIN_SPAN_DEGREES` 未満なら開き切りにし損ねたとみなす。
        """
        if error := open_close_span_error(self.closed_angle, self.open_angle):
            return (
                f"{error}。閉じ切り・開き切りまで動かしてから読み直すか、"
                "境目をまたぐならホーンの付け位置を変えてください"
            )
        if self.span_degrees < MIN_SPAN_DEGREES:
            return (
                f"開き量が {self.span_degrees:.1f}° しかありません（{MIN_SPAN_DEGREES:g}° 以上が要ります）。"
                "閉じ切り・開き切りまで動かしてから読んでください"
            )
        return None

    @property
    def closed_angle(self) -> float:
        """閉じ切りの角度（サーボの絶対角 [deg]。設定に書く小数 2 桁）."""
        return round(position_to_degrees(self.closed_position), _ANGLE_DIGITS)

    @property
    def open_angle(self) -> float:
        """開き切りの角度（サーボの絶対角 [deg]。設定に書く小数 2 桁）."""
        return round(position_to_degrees(self.open_position), _ANGLE_DIGITS)

    @property
    def span_degrees(self) -> float:
        """閉じ切りから開き切りまでの角度 [deg]."""
        return abs(self.open_position - self.closed_position) * 360 / STEPS_PER_REV

    @property
    def limits(self) -> tuple[int, int]:
        """サーボの角度制限に書く（下限、上限）の位置（閉じ切り〜開き切り。逆向きでも下限 < 上限）."""
        low, high = sorted((self.closed_position, self.open_position))
        return low, high


def release_angle_limits(bus: ServoBus, servo_id: int) -> None:
    """サーボの角度制限を全域（0〜4095）に戻す（前回の設定の制限に引っ張られずに動かす・読むため）.

    Raises:
        ServoError: 通信に失敗した場合
    """
    bus.set_angle_limits(servo_id, 0, STEPS_PER_REV - 1)


def exercise(
    bus: ServoBus, servo_id: int, span: OpenCloseRange, *, timeout: float = 6.0
) -> bool:
    """Open Close を閉じ切り → 開き切りと動かす（各移動の完了を待つ）.

    移動が timeout 内に終わらなければ、当たりに押し付け続けないようトルクを切って止める。

    Returns:
        閉じ切り・開き切りとも timeout 内に動き終えたか（False ならトルクを切ってある）

    Raises:
        ServoError: 通信に失敗した場合
    """
    for target in (span.closed_position, span.open_position):
        bus.goto(servo_id, target)
        if not wait_stopped(bus, servo_id, timeout=timeout):
            bus.set_torque(servo_id, False)
            return False
    return True


def servo_settings(roles: ServoRoles, span: OpenCloseRange) -> dict[str, int | float]:
    """machine.toml の [pnp.servo] に書く値（設定キー → 値。開閉は角度 [deg]）."""
    return {
        "pnp.servo.rotation_id": roles.rotation_id,
        "pnp.servo.open_close_id": roles.open_close_id,
        "pnp.servo.closed_angle": span.closed_angle,
        "pnp.servo.open_angle": span.open_angle,
    }
