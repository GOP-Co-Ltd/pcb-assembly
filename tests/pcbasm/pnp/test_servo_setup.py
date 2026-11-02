"""`pcbasm.pnp.servo_setup` の仕様テスト（サーボは FakeServoBus）.

実機のサーボで役割と開閉の範囲を確かめるのは受け入れ試験（docs/acceptance.md）。
"""

import pytest

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeServoBus, ServoCommand
from pcbasm.pnp.servo_setup import (
    MIN_SPAN_DEGREES,
    OpenCloseRange,
    ServoRole,
    ServoRoles,
    ServoSetupError,
    exercise,
    find_pair,
    release,
    role_of_mode,
    roles_from_answers,
    servo_settings,
    verify_modes,
)

ROTATION = ServoRole.ROTATION
OPEN_CLOSE = ServoRole.OPEN_CLOSE


class TestServoRole:
    def test_each_role_needs_its_mode(self):
        assert ROTATION.mode is ServoMode.STEP
        assert OPEN_CLOSE.mode is ServoMode.POSITION

    @pytest.mark.parametrize("role", list(ServoRole))
    def test_role_of_mode_is_the_inverse(self, role: ServoRole):
        assert role_of_mode(role.mode) is role

    def test_labels_differ(self):
        assert ROTATION.label != OPEN_CLOSE.label


class TestFindPair:
    @pytest.mark.parametrize(
        "ids",
        [(), (3,), (1, 2, 3)],
        ids=["none", "one", "three"],
    )
    def test_other_than_two_servos_is_an_error(self, ids: tuple[int, ...]):
        bus = FakeServoBus(dict.fromkeys(ids, ServoMode.POSITION))

        with pytest.raises(ServoSetupError, match=f"{len(ids)} 台"):
            find_pair(bus, range(10))

    def test_returns_the_two_ids_in_scan_order(self):
        bus = FakeServoBus({7: ServoMode.STEP, 2: ServoMode.POSITION})

        assert find_pair(bus, range(10)) == (2, 7)

    def test_ids_outside_the_range_are_not_found(self):
        bus = FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION})

        with pytest.raises(ServoSetupError):
            find_pair(bus, range(2))


class TestRolesFromAnswers:
    def test_one_of_each_role(self):
        roles = roles_from_answers({1: OPEN_CLOSE, 2: ROTATION})

        assert roles == ServoRoles(rotation_id=2, open_close_id=1)

    @pytest.mark.parametrize("role", list(ServoRole))
    def test_same_role_twice_is_none(self, role: ServoRole):
        assert roles_from_answers({1: role, 2: role}) is None


class TestVerifyModes:
    def test_matching_modes_pass(self):
        bus = FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION})

        verify_modes(bus, ServoRoles(rotation_id=1, open_close_id=2))

    def test_swapped_roles_name_the_id_and_both_modes(self):
        bus = FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION})

        with pytest.raises(ServoSetupError) as excinfo:
            verify_modes(bus, ServoRoles(rotation_id=2, open_close_id=1))

        message = str(excinfo.value)
        assert "ID 2" in message
        assert "POSITION" in message
        assert "STEP" in message
        assert "FD" in message


class TestRelease:
    def test_release_turns_the_torque_off(self):
        bus = FakeServoBus({2: ServoMode.POSITION})

        release(bus, 2)

        assert bus.commands == [ServoCommand(2, "torque", 0)]


class TestOpenCloseRange:
    @pytest.mark.parametrize(("closed", "opened"), [(1000, 1300), (1300, 1000)])
    def test_either_direction_is_accepted(self, closed: int, opened: int):
        span = OpenCloseRange(closed, opened)

        assert span.validate() is None
        assert span.span_degrees == pytest.approx(300 * 360 / 4096)

    @pytest.mark.parametrize(
        ("closed", "opened", "message"),
        [
            (1000, 1000, "同じ"),
            (1000, 1033, "開き量"),  # 33 step ≈ 2.9°
            (100, 3000, "境目"),
        ],
        ids=["same", "too_small", "wraps"],
    )
    def test_unusable_range_tells_why(self, closed: int, opened: int, message: str):
        error = OpenCloseRange(closed, opened).validate()

        assert error is not None and message in error

    def test_minimum_span_is_accepted(self):
        steps = round(MIN_SPAN_DEGREES * 4096 / 360) + 1
        span = OpenCloseRange(1000, 1000 + steps)

        assert span.validate() is None
        assert span.span_degrees >= MIN_SPAN_DEGREES

    def test_half_turn_is_accepted(self):
        span = OpenCloseRange(0, 2048)

        assert span.validate() is None
        assert span.span_degrees == pytest.approx(180.0)


class TestExercise:
    def test_moves_to_closed_then_open(self):
        bus = FakeServoBus({2: ServoMode.POSITION}, {2: 1300})

        assert exercise(bus, 2, OpenCloseRange(1000, 1300)) is True
        assert bus.commands == [
            ServoCommand(2, "goto", 1000),
            ServoCommand(2, "goto", 1300),
        ]

    def test_servo_that_keeps_moving_is_released_and_reported(self):
        bus = FakeServoBus({2: ServoMode.POSITION}, {2: 1300})
        bus.jam(2)

        assert exercise(bus, 2, OpenCloseRange(1000, 1300), timeout=0.1) is False
        # 閉じ切りへの移動が終わらないので、開き切りへは動かさずにトルクを切る
        assert bus.commands == [
            ServoCommand(2, "goto", 1000),
            ServoCommand(2, "torque", 0),
        ]
        bus.turn_by_hand(2, 1100)  # トルクが切れていれば手で回せる


class TestServoSettings:
    def test_keys_of_pnp_servo(self):
        settings = servo_settings(
            ServoRoles(rotation_id=1, open_close_id=2), OpenCloseRange(1000, 1300)
        )

        assert settings == {
            "pnp.servo.rotation_id": 1,
            "pnp.servo.open_close_id": 2,
            "pnp.servo.closed_position": 1000,
            "pnp.servo.open_position": 1300,
        }
