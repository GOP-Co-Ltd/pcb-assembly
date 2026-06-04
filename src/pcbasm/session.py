"""塗布実行のための HAL 配線をまとめたセッション.

各運用スクリプトが繰り返していた「マシン設定読み込み → Board 計測 → probe / height_measurer /
dispenser の構築」をまとめ、コンテキストマネージャ として終了時のクリーンアップ（M84 + ウィンドウ破棄）まで面倒を見る。
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import attrs
import cv2

from pcbasm.config import Machine, get_machine_config
from pcbasm.geometry import Compose, Identity, Transform
from pcbasm.hal import Camera, Klipper, PasteDispenser, ServoGroundProbe, XYZStage
from pcbasm.pasting import HeightPlaneMeasurer, PasteApplicator, ProbeExecutor
from pcbasm.pcb import PcbFile
from pcbasm.posctrl import BoardCalibrationResult, setup_board_calibration
from pcbasm.vision import CalibrationResult


@attrs.frozen
class PasteSession:
    """マシン初期化〜Board 計測〜塗布用 HAL の配線をまとめた実行セッション.

    ``PasteSession.setup(...)`` で構築し、コンテキストマネージャとして使う
    （終了時に M84 とウィンドウ破棄を行う）。
    """

    machine: Machine
    klipper: Klipper
    stage: XYZStage
    camera: Camera
    calibration: CalibrationResult
    board_transform: Transform
    toolhead_offset: Transform
    pcb: PcbFile
    probe_executor: ProbeExecutor
    height_measurer: HeightPlaneMeasurer
    paste_dispenser: PasteDispenser

    @classmethod
    def setup(
        cls,
        machine_name: str,
        pcb_file_path: Path,
        tolerance: float = 0.1,
        window_name: str = "Calibration",
    ) -> Self:
        """マシン設定読み込み〜Board 計測〜塗布用 HAL 構築をまとめて実行する."""
        machine = get_machine_config(machine_name)
        result = setup_board_calibration(
            machine=machine,
            pcb_file_path=pcb_file_path,
            tolerance=tolerance,
            window_name=window_name,
        )
        return cls.from_calibration(result)

    @classmethod
    def from_calibration(cls, result: BoardCalibrationResult) -> Self:
        """既存の BoardCalibrationResult から塗布用 HAL を組み立てる."""
        machine = result.machine
        probe_config = machine.probe
        probe = ServoGroundProbe(
            result.klipper.readonly,
            servo_name=probe_config.servo_name,
            revolution_distance=probe_config.revolution_distance,
            down_distance=probe_config.down_distance,
        )
        probe_executor = ProbeExecutor(
            klipper=result.klipper, probe=probe, stage=result.stage
        )
        height_measurer = HeightPlaneMeasurer(
            probe_executor=probe_executor,
            klipper=result.klipper,
            stage=result.stage,
            min_radius=probe_config.min_radius,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
        )
        paste_dispenser = PasteDispenser(
            klipper=result.klipper.readonly,
            rotations_per_ul=machine.paste_dispenser.rotations_per_ul,
        )
        return cls(
            machine=machine,
            klipper=result.klipper,
            stage=result.stage,
            camera=result.camera,
            calibration=result.calibration,
            board_transform=result.board_transform,
            toolhead_offset=machine.paste_dispenser.toolhead.to_transform(),
            pcb=result.pcb,
            probe_executor=probe_executor,
            height_measurer=height_measurer,
            paste_dispenser=paste_dispenser,
        )

    @property
    def board_to_machine(self) -> Transform:
        """Board 座標 → machine 座標の変換（board_transform + toolhead_offset）."""
        return Compose([self.board_transform, self.toolhead_offset])

    def make_applicator(self, transform: Transform = Identity()) -> PasteApplicator:
        """Machine 設定のパラメータで PasteApplicator を構築する."""
        cfg = self.machine.paste_dispenser
        return PasteApplicator(
            klipper=self.klipper,
            paste_dispenser=self.paste_dispenser,
            stage=self.stage,
            nozzle_diameter=cfg.nozzle_diameter,
            dispense_rate=cfg.dispense_rate,
            dispense_accel=cfg.dispense_accel,
            ul_per_mm2=cfg.ul_per_mm2,
            retraction=cfg.retract_amount,
            retraction_rate=cfg.retract_rate,
            retraction_accel_factor=cfg.retract_accel_factor,
            paste_height=cfg.paste_height,
            prime_extra_delay=cfg.prime_extra_delay,
            transform=transform,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.klipper.send_gcode("M84")
        cv2.destroyAllWindows()
