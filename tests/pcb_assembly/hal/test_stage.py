import pytest
from pytest_mock import MockerFixture

from pcb_assembly.geometry import Position, Waypoint
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import AxisLimits, Limits, XYZStage
from tests.helpers import mark_hardware


class TestXYZStage:
    """XYZStageクラスのテスト."""

    @pytest.fixture
    def stage(self) -> XYZStage:
        klipper = Klipper()
        stage = XYZStage(klipper.readonly)
        return stage

    @mark_hardware
    def test_get_position(self, stage: XYZStage):
        position = stage.get_position()

        assert isinstance(position, Position)

    @mark_hardware
    def test_get_limits(self, stage: XYZStage):
        limits = stage.get_limits()

        assert isinstance(limits, Limits)
        assert isinstance(limits.x, AxisLimits)
        assert isinstance(limits.y, AxisLimits)
        assert isinstance(limits.z, AxisLimits)
        assert isinstance(limits.v, AxisLimits)
        assert limits.x.min < limits.x.max
        assert limits.y.min < limits.y.max
        assert limits.z.min < limits.z.max
        assert limits.v.min < limits.v.max

    def test_get_limits_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"printer": {"max_velocity": "300"}},
        )
        stage = XYZStage(klipper.readonly)

        with pytest.raises(KeyError, match=r"printer\.cfgに\[stepper_x\]セクション"):
            stage.get_limits()

    def test_get_limits_missing_position_keys(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"step_pin": "PC0"},
                "stepper_y": {"step_pin": "PC1"},
                "stepper_z": {"step_pin": "PC2"},
                "printer": {"max_velocity": "300"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"position_minとposition_max"):
            stage.get_limits()

    def test_get_limits_missing_printer_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"printer\.cfgに\[printer\]セクション"):
            stage.get_limits()

    def test_get_limits_missing_max_velocity(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
                "printer": {"kinematics": "cartesian"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"max_velocity"):
            stage.get_limits()


class TestAxisLimits:
    """AxisLimitsクラスのテスト."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, True),  # 最小値
            (100.0, True),  # 最大値
            (50.0, True),  # 中間値
            (-0.1, False),  # 最小値未満
            (100.1, False),  # 最大値超過
        ],
    )
    def test_contains(self, value: float, expected: bool):
        limits = AxisLimits(min=0.0, max=100.0)

        assert (value in limits) == expected


class TestLimits:
    """Limitsクラスのテスト."""

    @pytest.fixture
    def limits(self) -> Limits:
        return Limits(
            x=AxisLimits(min=0.0, max=100.0),
            y=AxisLimits(min=0.0, max=200.0),
            z=AxisLimits(min=0.0, max=50.0),
            v=AxisLimits(min=0.0, max=300.0),
        )

    @pytest.mark.parametrize(
        ("waypoint", "expected"),
        [
            (Waypoint(x=50.0, y=100.0, z=25.0, v=150.0), True),  # 全制限内
            (Waypoint(x=0.0, y=0.0, z=0.0, v=0.0), True),  # 全最小値
            (Waypoint(x=100.0, y=200.0, z=50.0, v=300.0), True),  # 全最大値
            (Waypoint(x=-1.0, y=100.0, z=25.0, v=150.0), False),  # x軸が範囲外
            (Waypoint(x=50.0, y=201.0, z=25.0, v=150.0), False),  # y軸が範囲外
            (Waypoint(x=50.0, y=100.0, z=51.0, v=150.0), False),  # z軸が範囲外
            (Waypoint(x=50.0, y=100.0, z=25.0, v=301.0), False),  # 速度が範囲外
        ],
    )
    def test_contains(self, limits: Limits, waypoint: Waypoint, expected: bool):
        assert (waypoint in limits) == expected
