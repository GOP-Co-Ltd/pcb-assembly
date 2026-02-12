import pytest
from pytest_mock import MockerFixture

from pcb_assembly.geometry import Move, Point3d, Trajectory, Waypoint
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import Limits, ScalarLimits, XYZStage
from tests.helpers import mark_hardware


class TestXYZStage:
    """XYZStageクラスのテスト."""

    @pytest.fixture
    def stage(self) -> XYZStage:
        klipper = Klipper()
        stage = XYZStage(klipper.readonly)
        return stage

    @pytest.fixture
    def mock_stage(self, mocker: MockerFixture) -> XYZStage:
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
                "printer": {"max_velocity": "300"},
            },
        )
        return XYZStage(klipper.readonly)

    @mark_hardware
    def test_get_position(self, stage: XYZStage):
        position = stage.get_position()

        assert isinstance(position, Point3d)

    @mark_hardware
    def test_limits(self, stage: XYZStage):
        limits = stage.limits

        assert isinstance(limits, Limits)
        assert isinstance(limits.x, ScalarLimits)
        assert isinstance(limits.y, ScalarLimits)
        assert isinstance(limits.z, ScalarLimits)
        assert isinstance(limits.v, ScalarLimits)
        assert limits.x.min < limits.x.max
        assert limits.y.min < limits.y.max
        assert limits.z.min < limits.z.max
        assert limits.v.min < limits.v.max

    def test_max_velocity(self, mock_stage: XYZStage):
        assert mock_stage.max_velocity == 300.0

    def test_limits_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"printer": {"max_velocity": "300"}},
        )
        stage = XYZStage(klipper.readonly)

        with pytest.raises(KeyError, match=r"printer\.cfgに\[stepper_x\]セクション"):
            _ = stage.limits

    def test_limits_missing_position_keys(self, mocker: MockerFixture):
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
            _ = stage.limits

    def test_limits_missing_printer_section(self, mocker: MockerFixture):
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
            _ = stage.limits

    def test_limits_missing_max_velocity(self, mocker: MockerFixture):
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
            _ = stage.limits

    def test_validate_is_valid_when_all_in_limits(self, mock_stage: XYZStage):
        trajectory = Trajectory(
            origin=Point3d(10.0, 10.0, 10.0), initial_velocity=100.0
        )
        trajectory.add(Move(x=50.0, y=100.0, z=25.0))

        result = mock_stage.validate(trajectory)

        assert result.is_valid
        assert result.invalid_points == []

    def test_validate_returns_invalid_waypoints(self, mock_stage: XYZStage):
        trajectory = Trajectory(
            origin=Point3d(10.0, 10.0, 10.0), initial_velocity=100.0
        )
        trajectory.add(
            Move(x=50.0, y=100.0, z=25.0),  # 制限内
            Move(x=150.0, y=100.0, z=25.0),  # x範囲外
        )

        result = mock_stage.validate(trajectory)

        assert not result.is_valid
        assert len(result.invalid_points) == 1
        assert result.invalid_points[0].x == 150.0

    def test_move_returns_gcode(self, mock_stage: XYZStage):
        trajectory = Trajectory(
            origin=Point3d(10.0, 10.0, 10.0), initial_velocity=100.0
        )
        trajectory.add(
            Move(x=50.0, y=100.0, z=25.0),
            Move(x=60.0),
        )

        result = mock_stage.move(trajectory)

        commands = result.to_list()
        assert len(commands) == 2
        assert commands[0] == "G1 X50.0 Y100.0 Z25.0 F6000.0"
        assert commands[1] == "G1 X60.0 Y100.0 Z25.0 F6000.0"

    def test_move_raises_when_out_of_limits(self, mock_stage: XYZStage):
        trajectory = Trajectory(
            origin=Point3d(10.0, 10.0, 10.0), initial_velocity=100.0
        )
        trajectory.add(Move(x=150.0))  # x範囲外

        with pytest.raises(ValueError, match="制限外の経由点"):
            mock_stage.move(trajectory)


class TestScalarLimits:
    """ScalarLimitsクラスのテスト."""

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
        limits = ScalarLimits(min=0.0, max=100.0)

        assert (value in limits) == expected


class TestLimits:
    """Limitsクラスのテスト."""

    @pytest.fixture
    def limits(self) -> Limits:
        return Limits(
            x=ScalarLimits(min=0.0, max=100.0),
            y=ScalarLimits(min=0.0, max=200.0),
            z=ScalarLimits(min=0.0, max=50.0),
            v=ScalarLimits(min=0.0, max=300.0),
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
