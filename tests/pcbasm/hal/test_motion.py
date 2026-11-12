"""`pcbasm.hal.motion` の仕様テスト."""

import pytest

from pcbasm.geometry import Point3d
from pcbasm.hal import (
    Axis,
    ContactSettings,
    Velocity,
    contact_move_error,
    contact_velocity,
    homing_order,
)


class TestHomingOrder:
    """原点出しの軸の順（Z → X → Y、Z の原点が未なら Z を先に出す）."""

    @pytest.mark.parametrize(
        ("axes", "homed", "expected"),
        [
            ((), (), ("z", "x", "y")),
            ((), ("x", "y", "z"), ("z", "x", "y")),
            (("y", "x"), (), ("z", "x", "y")),
            (("y", "x"), ("z",), ("x", "y")),
            (("x",), (), ("z", "x")),
            (("x",), ("z",), ("x",)),
            (("z",), (), ("z",)),
            (("y", "z"), ("x", "y", "z"), ("z", "y")),
        ],
    )
    def test_orders_z_first_and_adds_unhomed_z(
        self,
        axes: tuple[Axis, ...],
        homed: tuple[Axis, ...],
        expected: tuple[Axis, ...],
    ):
        assert homing_order(axes, homed) == expected


class TestContactSettings:
    def test_defaults_are_the_kurousagi_values(self):
        settings = ContactSettings()

        assert (settings.trigger_force, settings.release_force) == (50.0, 15.0)
        assert not settings.press_positive

    @pytest.mark.parametrize(
        ("trigger_force", "release_force"),
        [(0.0, 15.0), (50.0, 0.0), (50.0, 50.0), (15.0, 50.0)],
        ids=["trigger-zero", "release-zero", "equal", "release-above"],
    )
    def test_rejects_forces_that_cannot_release(
        self, trigger_force: float, release_force: float
    ):
        with pytest.raises(ValueError):
            ContactSettings(trigger_force=trigger_force, release_force=release_force)


class TestContactVelocity:
    """接触保持の間は X・Y を捨て、Z は離す向きだけを残す."""

    @pytest.mark.parametrize(
        ("press_positive", "velocity", "expected"),
        [
            # kurousagi: Z− で押し当たるので、離す向きは Z+（ステージが下がる）
            (False, Velocity(5.0, -3.0, 2.0), Velocity(z=2.0)),
            (False, Velocity(5.0, -3.0, -2.0), Velocity()),
            (False, Velocity(z=0.0), Velocity()),
            (True, Velocity(1.0, 1.0, -2.0), Velocity(z=-2.0)),
            (True, Velocity(z=2.0), Velocity()),
        ],
    )
    def test_keeps_only_z_away_from_the_tweezers(
        self, press_positive: bool, velocity: Velocity, expected: Velocity
    ):
        assert contact_velocity(velocity, press_positive=press_positive) == expected


class TestContactMoveError:
    _HERE = Point3d(10.0, 20.0, -15.0)

    @pytest.mark.parametrize(
        ("press_positive", "target"),
        [
            (False, {"z": -14.0}),
            (False, {"z": -15.0}),
            (False, {}),
            (True, {"z": -16.0}),
        ],
    )
    def test_accepts_z_away_from_the_tweezers(
        self, press_positive: bool, target: dict[str, float]
    ):
        move = {"x": None, "y": None, "z": None, **target}

        assert (
            contact_move_error(self._HERE, **move, press_positive=press_positive)
            is None
        )

    @pytest.mark.parametrize(
        ("press_positive", "target", "word"),
        [
            (False, {"x": 10.0}, "X・Y"),
            (False, {"y": 21.0, "z": -14.0}, "X・Y"),
            (False, {"z": -15.1}, "Z+"),
            (True, {"z": -14.0}, "Z−"),
        ],
    )
    def test_refuses_xy_and_pressing_z(
        self, press_positive: bool, target: dict[str, float], word: str
    ):
        move = {"x": None, "y": None, "z": None, **target}

        reason = contact_move_error(self._HERE, **move, press_positive=press_positive)

        assert reason is not None and word in reason
