"""流量キャリブレーションの機械手順（銅板・transform・applicator を束ねる）.

共通土台（その場生成した銅板 → Board 計測 → 高さ計測 → transform）を確立し、
メニューループ中保持し続ける applicator を管理する。① の検証ループは新
``rotations_per_ul`` で applicator を作り直すため、現在の applicator と現在の
``rotations_per_ul`` / ``dispense_accel`` をここで一元管理する。

ユーザー対話・進捗・中断・ログ文言は持たない（web ジョブ側の責務）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Self, cast

from pcbasm.gcode import GCode
from pcbasm.geometry import Compose, Point2d, Transform
from pcbasm.pasting.applicator import DispenseExecution, PasteApplicator
from pcbasm.pasting.flowcalib.flow import RotationsPerUlRound
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Copper, Layer
from pcbasm.posctrl import BoardCalibrationResult
from pcbasm.utils import get_class_module_path


class FlowCalibrationProcedure:
    """銅板上に線を引いて計量するための HAL 側手順.

    コンテキストマネージャとして使い、``with`` の間だけ applicator（ディスペンサー）が
    有効になる。:meth:`adopt` は ``with`` の中で呼ぶ。

    Example:
        with FlowCalibrationProcedure.setup(result) as procedure:
            executions = procedure.draw_lines(layout.lines, amount_ul=0.5)
            procedure.move_to_removal_z(0.0)
    """

    def __init__(self, session: PasteSession, transform: Transform) -> None:
        """Board 計測済みのセッションと、銅板 board 座標 → 機械座標（高さ面込み）の変換で組む."""
        self._session = session
        self._transform = transform
        config = session.machine.paste_dispenser
        self._rotations_per_ul = config.rotations_per_ul
        self._dispense_accel = config.dispense_accel
        self._applicator = session.make_applicator()
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    @classmethod
    def setup(cls, result: BoardCalibrationResult) -> Self:
        """Board 計測結果から銅板外形を銅箔とみなして高さ計測し、手順を組む."""
        session = PasteSession.from_calibration(result)
        outline = session.pcb.outline.polygon
        height_plane = session.measure_height_plane(
            [Copper(layer=Layer.TOP, polygon=outline)]
        )
        transform = Compose(
            [session.board_transform, session.toolhead_offset, height_plane]
        )
        return cls(session, transform)

    def __enter__(self) -> Self:
        self._applicator.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._applicator.__exit__(*args)

    @property
    def session(self) -> PasteSession:
        return self._session

    @property
    def applicator(self) -> PasteApplicator:
        return self._applicator

    @property
    def rotations_per_ul(self) -> float:
        """現在の applicator が使っている rotations_per_ul [rev/μL]."""
        return self._rotations_per_ul

    @property
    def dispense_accel(self) -> float:
        """現在の rotations_per_ul に対応する吐出加速度 [μL/sec²]."""
        return self._dispense_accel

    @property
    def transform(self) -> Transform:
        """銅板 board 座標 → 機械座標（高さ面込み）."""
        return self._transform

    def move_to_loading_z(self) -> None:
        """プライム/ふき取りのためヘッドを Z=0 へ上げる（ブロッキング）."""
        self._send_move(z=0.0)

    def removal_z(self, offset: float) -> float:
        """計量で基板を取り出すときの退避 Z = max(z_min, z_max - offset)."""
        z = self._session.stage.limits.z
        return max(z.min, z.max - offset)

    def move_to_removal_z(self, offset: float) -> float:
        """ヘッドを退避 Z へ上げ、その Z を返す（ブロッキング）."""
        z = self.removal_z(offset)
        self._logger.info("計量退避: Z=%.3f", z)
        self._send_move(z=z)
        return z

    def draw_lines(
        self,
        lines: Sequence[tuple[Point2d, Point2d]],
        *,
        amount_ul: float | Sequence[float],
        fill_speed: float | Sequence[float] | None = None,
        rate_cap: float | Sequence[float] | None = None,
        checkpoint: Callable[[int], None] | None = None,
        retract: bool = True,
    ) -> tuple[DispenseExecution, ...]:
        """Retract してから各線を順に塗布する（ブロッキング）.

        各 ``draw_line`` の ``FillSequence`` が prime → 吐出 → retract を内包するため、
        線間・線後の追加 retract は不要。先頭の retract はプライム済みのペーストを
        baseline まで引き戻すためのもので、直前の ``draw_lines`` から追加のプライムを
        していない続きの線（② の点ごとの計量）では ``retract=False`` で省く
        （retract は相対移動なので繰り返すと累積する）。

        Args:
            lines: 各線の (始点, 終点)（``transform`` 適用前の board 座標）
            amount_ul: 塗布量 [μL]（スカラーか線ごとの列）
            fill_speed: 塗布移動速度 [mm/sec]（``None`` で ``max_fill_speed``。スカラーか列）
            rate_cap: 吐出レートの頭打ち [μL/sec]（``None`` で ``max_dispense_rate``。スカラーか列）
            checkpoint: 各線を引く直前に線番号（0 始まり）で呼ぶコールバック
                （web ジョブが中断確認・進捗表示に使う）
            retract: 先頭で baseline まで retract するか（既定 True）

        Raises:
            ValueError: 列で渡した ``amount_ul`` / ``fill_speed`` / ``rate_cap`` の長さが
                ``lines`` と一致しない場合
        """
        amounts = _per_line(amount_ul, len(lines), "amount_ul")
        speeds = _per_line(fill_speed, len(lines), "fill_speed")
        caps = _per_line(rate_cap, len(lines), "rate_cap")
        if retract:
            self._applicator.retract()
        executions: list[DispenseExecution] = []
        for index, (start, end) in enumerate(lines):
            if checkpoint is not None:
                checkpoint(index)
            executions.append(
                self._applicator.draw_line(
                    start,
                    end,
                    amount_ul=amounts[index],
                    transform=self._transform,
                    fill_speed=speeds[index],
                    rate_cap=caps[index],
                )
            )
        return tuple(executions)

    def adopt(self, round: RotationsPerUlRound) -> None:
        """① の算出値を採用し、新 rotations_per_ul で applicator を作り直す.

        古い applicator は無効化し、新しい applicator を有効化する（``with`` の中で呼ぶ）。
        """
        self._applicator.__exit__(None, None, None)
        self._rotations_per_ul = round.computed
        self._dispense_accel = round.dispense_accel
        self._applicator = self._session.make_applicator(
            rotations_per_ul=round.computed
        )
        self._applicator.__enter__()
        self._logger.info(
            "rotations_per_ul を採用: %.6f rev/uL (dispense_accel %.6f uL/s^2)",
            round.computed,
            round.dispense_accel,
        )

    def _send_move(self, *, z: float) -> None:
        self._session.klipper.send_gcode(
            self._session.stage.move(z=z) + GCode.wait_for_done()
        )


def _per_line[T](value: T | Sequence[T], count: int, name: str) -> tuple[T, ...]:
    """スカラーは線数ぶん複製し、列は長さを検証してそのまま返す."""
    if isinstance(value, Sequence):
        items = cast("Sequence[T]", value)
        if len(items) != count:
            raise ValueError(
                f"{name} は線数 {count} と同じ長さが必要です: {len(items)}"
            )
        return tuple(items)
    return (cast("T", value),) * count
