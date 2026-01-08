from collections.abc import Generator

import pytest

from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import AxisLimits, Limits, Position, XYZStage
from tests.helpers import mark_hardware


class TestXYZStage:
    """XYZStageクラスのテスト."""

    @pytest.fixture
    def stage(self) -> Generator[XYZStage]:
        """ホーミング済みXYZStageインスタンスを提供し、終了時にリラックスする."""
        klipper = Klipper()
        stage = XYZStage(klipper, default_speed=50.0)
        stage.home()
        yield stage
        klipper.relax()

    @mark_hardware
    def test_home(self):
        klipper = Klipper()
        stage = XYZStage(klipper, default_speed=50.0)

        stage.home()

        position = stage.get_position()
        assert position.x == 0.0
        assert position.y == 0.0
        assert position.z == 0.0
        klipper.relax()

    @mark_hardware
    def test_move_absolute(self, stage: XYZStage):
        stage.move(x=10.0, y=20.0, z=5.0)

        position = stage.get_position()
        assert position.x == pytest.approx(10.0, abs=0.1)
        assert position.y == pytest.approx(20.0, abs=0.1)
        assert position.z == pytest.approx(5.0, abs=0.1)

    @mark_hardware
    def test_move_partial(self, stage: XYZStage):
        stage.move(x=10.0)
        stage.move(y=20.0)

        position = stage.get_position()
        assert position.x == pytest.approx(10.0, abs=0.1)
        assert position.y == pytest.approx(20.0, abs=0.1)
        assert position.z == pytest.approx(0.0, abs=0.1)

    @mark_hardware
    def test_move_relative(self, stage: XYZStage):
        stage.move(x=10.0, y=10.0, z=5.0)
        stage.move(x=5.0, y=-3.0, z=2.0, relative=True)

        position = stage.get_position()
        assert position.x == pytest.approx(15.0, abs=0.1)
        assert position.y == pytest.approx(7.0, abs=0.1)
        assert position.z == pytest.approx(7.0, abs=0.1)

    @mark_hardware
    def test_move_with_speed(self, stage: XYZStage):
        stage.move(x=10.0, speed=100.0)

        position = stage.get_position()
        assert position.x == pytest.approx(10.0, abs=0.1)

    @mark_hardware
    def test_move_no_args_does_nothing(self, stage: XYZStage):
        stage.move(x=10.0)
        stage.move()

        position = stage.get_position()
        assert position.x == pytest.approx(10.0, abs=0.1)

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
        assert limits.x.min < limits.x.max
        assert limits.y.min < limits.y.max
        assert limits.z.min < limits.z.max

    def test_get_limits_missing_stepper_section(self, mocker):
        klipper = Klipper()
        stage = XYZStage(klipper, default_speed=50.0)
        mocker.patch.object(klipper, "get_config", return_value={})

        with pytest.raises(KeyError, match=r"printer\.cfgに\[stepper_x\]セクション"):
            stage.get_limits()

    def test_get_limits_missing_position_keys(self, mocker):
        klipper = Klipper()
        stage = XYZStage(klipper, default_speed=50.0)
        mocker.patch.object(
            klipper,
            "get_config",
            return_value={
                "stepper_x": {"step_pin": "PC0"},
                "stepper_y": {"step_pin": "PC1"},
                "stepper_z": {"step_pin": "PC2"},
            },
        )

        with pytest.raises(KeyError, match=r"position_minとposition_max"):
            stage.get_limits()
