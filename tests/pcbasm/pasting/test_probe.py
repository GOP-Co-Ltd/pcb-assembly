"""ProbeExecutor のテスト.

契約:

- __init__: printer.cfg に [load_cell_probe] セクションが無ければ RuntimeError
- probe(): PROBE 送信 → last_z_result を接触 Z として返す →
  接触 Z + lift_height へ退避する
- settle_sec > 0 のとき PROBE の後に dwell を挟む
"""

import pytest

from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FAKE_PRINTER_CONFIG, FakeKlipper
from pcbasm.pasting.probe import ProbeExecutor


class TestProbeExecutor:
    """[load_cell_probe] 検証と PROBE → last_z_result → 退避の契約."""

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.set_status("probe", "last_z_result", -2.0)
        return klipper

    def test_init_without_load_cell_probe_section_raises(self):
        config = {
            section: values
            for section, values in FAKE_PRINTER_CONFIG.items()
            if section != "load_cell_probe"
        }
        klipper = FakeKlipper(config)

        with pytest.raises(RuntimeError) as exc:
            ProbeExecutor(klipper, XYZStage(klipper), settle_sec=0.0)

        assert "[load_cell_probe]" in str(exc.value)

    def test_probe_returns_contact_z_and_lifts_by_lift_height(
        self, klipper: FakeKlipper
    ):
        executor = ProbeExecutor(
            klipper, XYZStage(klipper), lift_height=3.25, settle_sec=0.0
        )

        z = executor.probe()

        # 接触 Z (-2.0) + lift_height (3.25) = 1.25 へ退避する
        assert z == -2.0
        probe, lift = klipper.sent
        assert "PROBE" in probe.gcode.to_list()
        assert klipper.g1_moves() == [{"z": 1.25, "f": 6000.0}]
        assert lift.gcode.to_list()[-1] == "M400"

    def test_settle_sec_dwell_follows_probe(self, klipper: FakeKlipper):
        executor = ProbeExecutor(klipper, XYZStage(klipper), settle_sec=0.5)

        executor.probe()

        commands = klipper.sent[0].gcode.to_list()
        assert commands.index("PROBE") < commands.index("G4 P500")
