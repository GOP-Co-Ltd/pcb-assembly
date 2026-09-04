"""ProbeExecutor のテスト.

計画書 claude-probe-gnd-probe-load-cell-probe.md「新 ProbeExecutor」節が契約:

- __init__: printer.cfg に [load_cell_probe] セクションが無ければ RuntimeError
- probe(): PROBE 送信 → last_z_result を接触 Z として返す →
  接触 Z + lift_height へ退避する
- settle_time > 0 のとき PROBE の後に dwell を挟む
"""

import pytest
from pytest_mock import MockerFixture

from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.hal.klipper import Klipper
from pcbasm.pasting.probe import ProbeExecutor
from tests.helpers import mark_hardware


def _patch_config(mocker: MockerFixture, klipper: Klipper, config: dict) -> None:
    """get_config を Klipper 本体と readonly の両面で差し替える."""
    get_config = mocker.Mock(return_value=config)
    mocker.patch.object(klipper, "get_config", get_config)
    mocker.patch.object(klipper.readonly, "get_config", get_config)


class TestProbeExecutor:
    """[load_cell_probe] 検証と PROBE → last_z_result → 退避の契約."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        klipper = Klipper()
        _patch_config(mocker, klipper, {"load_cell_probe": {}})
        get_status = mocker.Mock(return_value=-2.0)
        mocker.patch.object(klipper, "get_status", get_status)
        mocker.patch.object(klipper.readonly, "get_status", get_status)
        mocker.patch.object(klipper, "send_gcode", mocker.Mock(return_value={}))
        return klipper

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        # stage.move の戻り値は `+ GCode.wait_for_done()` で連結されるため実体を返す
        stage = mocker.Mock()
        stage.move.return_value = GCode("G1 Z1.25")
        return stage

    @mark_hardware
    def test_init(self):
        """実機の printer.cfg に [load_cell_probe] が構成済みであること."""
        klipper = Klipper()
        ProbeExecutor(klipper, XYZStage(klipper.readonly))

    def test_init_without_load_cell_probe_section_raises(self, mocker: MockerFixture):
        klipper = Klipper()
        _patch_config(mocker, klipper, {"probe": {}})

        with pytest.raises(RuntimeError) as exc:
            ProbeExecutor(klipper, mocker.Mock())

        assert "[load_cell_probe]" in str(exc.value)

    def test_probe_returns_last_z_result(self, klipper, stage):
        executor = ProbeExecutor(klipper, stage)

        assert executor.probe() == -2.0

    def test_probe_sends_probe_command(self, klipper, stage):
        executor = ProbeExecutor(klipper, stage)

        executor.probe()

        sent = GCode(klipper.send_gcode.call_args_list[0].args[0]).to_list()
        assert "PROBE" in sent

    def test_probe_lifts_to_contact_z_plus_lift_height(self, klipper, stage):
        executor = ProbeExecutor(klipper, stage, lift_height=3.25)

        executor.probe()

        # 接触 Z (-2.0) + lift_height (3.25) = 1.25 へ退避する
        stage.move.assert_called_once_with(z=1.25)

    def test_settle_time_dwell_follows_probe(self, klipper, stage):
        executor = ProbeExecutor(klipper, stage, settle_time=0.5)

        executor.probe()

        commands = GCode(klipper.send_gcode.call_args_list[0].args[0]).to_list()
        assert commands.index("PROBE") < commands.index("G4 P500")
