"""塗布実行のための HAL 配線をまとめたセッション.

各運用スクリプトが繰り返していた「マシン設定読み込み → Board 計測 → probe / height_measurer /
dispenser の構築」をまとめ、コンテキストマネージャ として終了時のクリーンアップまで面倒を見る。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

import attrs

from pcbasm.config import Machine, get_machine_config
from pcbasm.geometry import Compose, Identity, Point2d, Transform
from pcbasm.hal import (
    Camera,
    Klipper,
    PasteDispenser,
    XYZStage,
)
from pcbasm.parking import park_or_present
from pcbasm.pasting import HeightPlaneMeasurer, PasteApplicator, ProbeExecutor
from pcbasm.pcb import Pad, PcbFile
from pcbasm.posctrl import (
    BoardAlignment,
    BoardCalibrationResult,
    corrected_board_transform,
    setup_board_calibration,
)
from pcbasm.vision import CalibrationResult, FrameSink


@attrs.frozen
class PasteSession:
    """マシン初期化〜Board 計測〜塗布用 HAL の配線をまとめた実行セッション.

    ``PasteSession.setup(...)`` で構築し、コンテキストマネージャとして使う
    （終了時にノズルキャップへ駐機し、できなければ PRESENT / M84 に退避する）。
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
        pcb_file_path: Path,
        tolerance: float = 0.1,
        *,
        camera: Camera | None = None,
        frame_sink: FrameSink | None = None,
    ) -> Self:
        """マシン設定読み込み〜Board 計測〜塗布用 HAL 構築をまとめて実行する."""
        machine = get_machine_config()
        result = setup_board_calibration(
            machine=machine,
            pcb_file_path=pcb_file_path,
            tolerance=tolerance,
            camera=camera,
            frame_sink=frame_sink,
        )
        return cls.from_calibration(result)

    @classmethod
    def from_calibration(cls, result: BoardCalibrationResult) -> Self:
        """既存の BoardCalibrationResult から塗布用 HAL を組み立てる."""
        machine = result.machine
        probe_config = machine.probe
        probe_executor = ProbeExecutor(
            klipper=result.klipper,
            stage=result.stage,
            lift_height=probe_config.lift_height,
        )
        height_measurer = HeightPlaneMeasurer(
            probe_executor=probe_executor,
            klipper=result.klipper,
            stage=result.stage,
            min_radius=probe_config.min_radius,
            board_edge_margin=probe_config.board_edge_margin,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
        )
        paste_dispenser = PasteDispenser(
            klipper=result.klipper.readonly,
            rotations_per_ul=machine.paste_dispenser.rotations_per_ul,
            air_pump_enabled=machine.paste_dispenser.air_pump_enabled,
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

    def pad_to_machine(
        self,
        board_point: Point2d,
        *,
        alignment: BoardAlignment,
        height_plane: Transform,
    ) -> Compose:
        """その pad の board 座標 → ノズル機械座標の全変換を組む（局所補正込み）.

        銅箔照合の局所補正は ``toolhead_offset`` の**前**（変位はカメラの機械座標系
        で定義されているため）、``height_plane`` は定義域がノズル機械 XY なので
        **最後尾**。この順序が塗布の座標系規則そのものなので、job 側で組み直さない。

        Args:
            board_point: 補正を引く点（board座標、mm）。塗布なら pad 中心
            alignment: 区ごとの局所補正
            height_plane: 基板高さ面（ノズル機械 XY → Z 補正）

        Returns:
            board座標→ノズル機械座標の合成変換
        """
        return Compose(
            [
                corrected_board_transform(self.board_transform, alignment, board_point),
                self.toolhead_offset,
                height_plane,
            ]
        )

    def pad_transforms(
        self,
        pads: Sequence[Pad],
        *,
        alignment: BoardAlignment,
        height_plane: Transform,
    ) -> list[tuple[Pad, Compose]]:
        """Pad ごとの board 座標 → ノズル機械座標の変換を入力順で返す.

        補正は各 pad の中心で引くので pad ごとに違う。塗布ループも初回パージも
        この 1 本の列から変換を受け取るため、「どの pad にどの変換を当てるか」の
        対応が呼び出し側のループの書き方に依存しない。

        Args:
            pads: 対象pad（塗布順・パージ pad を含めてよい）
            alignment: 区ごとの局所補正
            height_plane: 基板高さ面（ノズル機械 XY → Z 補正）

        Returns:
            入力 pads と同順・同数の (pad, board→ノズル機械座標の変換)
        """
        return [
            (
                pad,
                self.pad_to_machine(
                    pad.center, alignment=alignment, height_plane=height_plane
                ),
            )
            for pad in pads
        ]

    def make_applicator(
        self,
        transform: Transform = Identity(),
        *,
        rotations_per_ul: float | None = None,
    ) -> PasteApplicator:
        """Machine 設定のパラメータで PasteApplicator を構築する.

        Args:
            transform: ``draw_line``（キャリブ用プリミティブ）に使う座標変換。
                ``apply`` / ``deposit_at`` は pad ごとの変換を引数で受け取るので
                これを見ない
            rotations_per_ul: μL → 回転数の係数 [rev/μL] を上書きする値。
                ``None`` のとき machine 設定値を使う。キャリブ検証ループで
                新値を反映した applicator を作り直すための経路。

        Returns:
            構築した :class:`PasteApplicator`
        """
        paste_dispenser = self.paste_dispenser
        if rotations_per_ul is not None:
            paste_dispenser = PasteDispenser(
                klipper=self.klipper.readonly,
                rotations_per_ul=rotations_per_ul,
                air_pump_enabled=self.machine.paste_dispenser.air_pump_enabled,
            )
        return PasteApplicator.from_config(
            klipper=self.klipper,
            paste_dispenser=paste_dispenser,
            stage=self.stage,
            config=self.machine.paste_dispenser,
            transform=transform,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        park_or_present(self.klipper, self.machine)
