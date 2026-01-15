import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import AxisLimits, Limits, XYZStage
from pcb_assembly.transform import Position
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
        assert limits.x.min < limits.x.max
        assert limits.y.min < limits.y.max
        assert limits.z.min < limits.z.max

    def test_get_limits_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
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
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"position_minとposition_max"):
            stage.get_limits()
