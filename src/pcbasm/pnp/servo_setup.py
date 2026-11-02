"""ピンセットのサーボの役割判別と開閉の範囲.

2 台のサーボを 1 台ずつ動かして、人がどちらが Rotation / Open Close かを答える。
次に Open Close のトルクを切り、人が手で閉じ切り・開き切りにした位置を読む。
ここには判別と範囲の検証だけを置き、人への問い合わせはジョブ（``web.api.jobs.pnp.servo_setup``）が担う。
"""

from __future__ import annotations

import enum
import time
from collections.abc import Iterable, Mapping

import attrs

from pcbasm.hal import ServoBus, ServoMode, scan
from pcbasm.hal.servo import STEPS_PER_REV

# 開き量がこれ未満なら、手で開き切りにし損ねたとみなす [deg]
MIN_SPAN_DEGREES = 3.0
# 閉じ〜開きが半回転を超えるなら 4095/0 の境目をまたいでいる
_MAX_SPAN_STEPS = STEPS_PER_REV // 2
_POLL_INTERVAL = 0.02  # 移動の完了を見る間隔 [sec]


class ServoSetupError(ValueError):
    """役割や開閉の範囲が使えない（文言は利用者にそのまま見せる）."""


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


def roles_from_answers(answers: Mapping[int, ServoRole]) -> ServoRoles:
    """2 台それぞれに人が答えた役割から、役割の割り当てを作る.

    Raises:
        ServoSetupError: 両方の役割がそろっていない場合
    """
    by_role = {role: servo_id for servo_id, role in answers.items()}
    if len(answers) != 2 or len(by_role) != 2:
        raise ServoSetupError(
            "2 台に同じ役割を選びました。Rotation と Open Close を 1 台ずつ選んでください"
        )
    return ServoRoles(
        rotation_id=by_role[ServoRole.ROTATION],
        open_close_id=by_role[ServoRole.OPEN_CLOSE],
    )


def verify_modes(bus: ServoBus, roles: ServoRoles) -> None:
    """各サーボのモードが役割に合っているか確かめる.

    Raises:
        ServoSetupError: 役割とモードが合わない場合
    """
    for role in ServoRole:
        servo_id = roles.id_of(role)
        if (mode := bus.mode(servo_id)) is not role.mode:
            raise ServoSetupError(
                f"{role.label} に選んだ ID {servo_id} のモードが {mode.name}（{mode.value}）です。"
                f"{role.mode.name}（{role.mode.value}）が要ります。役割の選び間違いでなければ、"
                "モードを FD ツールで書き換えてからやり直してください"
            )


def release(bus: ServoBus, servo_id: int) -> None:
    """トルクを切る（手で回せるようにする）."""
    bus.set_torque(servo_id, False)


def hold_here(bus: ServoBus, servo_id: int) -> int:
    """今の位置でトルクを入れ、その位置を返す.

    トルクを入れる前に目標を今の位置にし、古い目標へ跳ばないようにする。
    """
    position = bus.position(servo_id)
    bus.goto(servo_id, position)
    bus.set_torque(servo_id, True)
    return position


@attrs.frozen
class OpenCloseRange:
    """Open Close の閉じ切り・開き切りの位置（raw 0〜4095）.

    Attributes:
        closed_position: 閉じ切りの位置
        open_position: 開き切りの位置（閉じより小さくてもよい）

    Raises:
        ServoSetupError: 2 つが同じか、開き量が :data:`MIN_SPAN_DEGREES` 未満か、4095/0 の境目をまたぐ場合
    """

    closed_position: int
    open_position: int

    def __attrs_post_init__(self) -> None:
        steps = abs(self.open_position - self.closed_position)
        if steps == 0:
            raise ServoSetupError(
                f"閉じ切りと開き切りが同じ位置（{self.closed_position}）です。開き切りまで動かしてから読んでください"
            )
        if steps > _MAX_SPAN_STEPS:
            raise ServoSetupError(
                f"閉じ切り（{self.closed_position}）と開き切り（{self.open_position}）の間が"
                "サーボの 4095/0 の境目をまたいでいます。ホーンの付け位置を変えてください"
            )
        if self.span_degrees < MIN_SPAN_DEGREES:
            raise ServoSetupError(
                f"開き量が {self.span_degrees:.1f}° しかありません（{MIN_SPAN_DEGREES:g}° 以上が要ります）。"
                "閉じ切り・開き切りまで動かしてから読んでください"
            )

    @property
    def span_degrees(self) -> float:
        """閉じ切りから開き切りまでの角度 [deg]."""
        return abs(self.open_position - self.closed_position) * 360 / STEPS_PER_REV


def exercise(
    bus: ServoBus, servo_id: int, span: OpenCloseRange, *, timeout: float = 6.0
) -> None:
    """Open Close を閉じ切り → 開き切りと動かす（各移動の完了を待つ）.

    Raises:
        ServoSetupError: 移動が timeout 内に終わらない場合
    """
    for target in (span.closed_position, span.open_position):
        bus.goto(servo_id, target)
        deadline = time.monotonic() + timeout
        while bus.moving(servo_id):
            if time.monotonic() > deadline:
                raise ServoSetupError(
                    f"サーボ {servo_id} の移動が {timeout} 秒で終わりません。当たって止まっていないか見てください"
                )
            time.sleep(_POLL_INTERVAL)


def servo_settings(roles: ServoRoles, span: OpenCloseRange) -> dict[str, int]:
    """machine.toml の [pnp.servo] に書く値（設定キー → 値）."""
    return {
        "pnp.servo.rotation_id": roles.rotation_id,
        "pnp.servo.open_close_id": roles.open_close_id,
        "pnp.servo.closed_position": span.closed_position,
        "pnp.servo.open_position": span.open_position,
    }
