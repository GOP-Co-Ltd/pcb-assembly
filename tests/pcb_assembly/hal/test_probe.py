import pytest
from pytest_mock import MockerFixture

from pcb_assembly.gcode import GCode
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.probe import Probe, ProbeGround, ProbeSensor


@pytest.fixture
def mock_klipper(mocker: MockerFixture) -> Klipper:
    klipper = Klipper()
    mocker.patch.object(
        klipper.readonly,
        "get_config",
        return_value={
            "probe": {"pin": "^!PA1"},
            "servo probe_gnd": {},
        },
    )
    return klipper


class TestProbeSensor:
    def test_init_raises_when_no_probe_config(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
        with pytest.raises(RuntimeError, match=r"printer\.cfgに\[probe\]セクション"):
            ProbeSensor(klipper.readonly)

    def test_init_succeeds_with_probe_config(self, mock_klipper: Klipper):
        ProbeSensor(mock_klipper.readonly)

    def test_get_last_z_result(self, mock_klipper: Klipper, mocker: MockerFixture):
        mocker.patch.object(mock_klipper.readonly, "get_status", return_value=-5.123)
        sensor = ProbeSensor(mock_klipper.readonly)
        assert sensor.get_last_z_result() == -5.123


class TestProbeGround:
    def test_init_raises_when_no_servo_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
        with pytest.raises(RuntimeError, match=r"printer\.cfgに\[servo probe_gnd\]"):
            ProbeGround(klipper.readonly, "probe_gnd", 40.0, 5.0)

    @pytest.mark.parametrize(
        ("revolution_distance", "down_distance", "expected_angle"),
        [
            (40.0, 5.0, 45.0),
            (360.0, 180.0, 180.0),
            (10.0, 2.5, 90.0),
        ],
    )
    def test_down(
        self,
        mock_klipper: Klipper,
        revolution_distance: float,
        down_distance: float,
        expected_angle: float,
    ):
        ground = ProbeGround(
            mock_klipper.readonly, "probe_gnd", revolution_distance, down_distance
        )
        assert ground.down().to_list() == [
            f"SET_SERVO SERVO=probe_gnd ANGLE={expected_angle}"
        ]

    def test_up(self, mock_klipper: Klipper):
        ground = ProbeGround(mock_klipper.readonly, "probe_gnd", 40.0, 5.0)
        assert ground.up().to_list() == ["SET_SERVO SERVO=probe_gnd ANGLE=0.0"]


class TestProbe:
    def test_probe_returns_down_probe_up_sequence(self, mock_klipper: Klipper):
        probe = Probe(mock_klipper.readonly, "probe_gnd", 40.0, 5.0)
        assert probe.probe().to_list() == [
            "SET_SERVO SERVO=probe_gnd ANGLE=45.0",
            "PROBE",
            "SET_SERVO SERVO=probe_gnd ANGLE=0.0",
        ]

    def test_probe_is_gcode(self, mock_klipper: Klipper):
        probe = Probe(mock_klipper.readonly, "probe_gnd", 40.0, 5.0)
        assert isinstance(probe.probe(), GCode)

    def test_get_last_z_result(self, mock_klipper: Klipper, mocker: MockerFixture):
        mocker.patch.object(mock_klipper.readonly, "get_status", return_value=-3.5)
        probe = Probe(mock_klipper.readonly, "probe_gnd", 40.0, 5.0)
        assert probe.get_last_z_result() == -3.5
