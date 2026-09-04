"""ペースト塗布の HAL オーケストレーション.

:class:`PasteApplicator` はディスペンサー・ステージ・Klipper を束ね、pad 1 枚の塗布
（:meth:`PasteApplicator.apply`）とキャリブレーション用のプリミティブ
（:meth:`draw_line` / :meth:`deposit_at` / :meth:`load_rotations`）を提供する。
経路生成は :mod:`pcbasm.pasting.fill_path`、1 ポリラインの GCode 組み立ては
:mod:`pcbasm.pasting.fill_sequence` に委譲し、ここは座標変換と送信・結果集約だけを担う。

座標変換（board → machine）は呼び出しごとに受け取る。applicator は基板を知らない。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal, Self

import attrs
from shapely import Polygon

from pcbasm.config import PasteDispenser as PasteDispenserConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point2d, Transform
from pcbasm.hal import Klipper, PasteDispenser, Speed, XYZStage
from pcbasm.pasting.fill_path import AppliedDispenseMode, FillPlan
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.params import DispenseSettings, PasteParams
from pcbasm.utils import get_class_module_path


@attrs.frozen
class DispenseExecution:
    """1 本のポリライン（``FillSequence`` 1 回）の吐出実績.

    Attributes:
        applied_mode: 実際に使った塗布方式
        path_length_mm: 塗布経路長 [mm]
        commanded_volume_ul: 指令した塗布量 [μL]
        prime_extra_volume_ul: プライム追加遅延で余分に押し出した量 [μL]
        effective_rate_ul_s: 実効吐出レート [μL/sec]
        rotations: 吐出に使った回転数 [rev]（プライム追加分込み）
        fill_speed: 実効塗布移動速度（点塗布など移動が無いときは ``None``）
    """

    applied_mode: AppliedDispenseMode
    path_length_mm: float
    commanded_volume_ul: float
    prime_extra_volume_ul: float
    effective_rate_ul_s: float
    rotations: float
    fill_speed: Speed | None


@attrs.frozen
class DispenseSummary:
    """複数 ``DispenseExecution`` の集計（dataset metadata の ``execution`` にもそのまま使う）.

    Attributes:
        applied_mode: 全成分が同一方式ならその方式、混在なら ``"mixed"``、成分無しは ``None``
        path_length_mm: 経路長の合計 [mm]
        commanded_volume_ul: 指令量の合計 [μL]
        prime_extra_volume_ul: プライム追加量の合計 [μL]
        effective_rate_ul_s: 体積加重の実効吐出レート [μL/sec]
        rotations: 回転数の合計 [rev]
    """

    applied_mode: AppliedDispenseMode | Literal["mixed"] | None
    path_length_mm: float
    commanded_volume_ul: float
    prime_extra_volume_ul: float
    effective_rate_ul_s: float
    rotations: float


@attrs.frozen
class PasteApplicationResult:
    """1 回の ``apply`` / ``deposit_at`` の実績（成分ごとの ``DispenseExecution`` の列）."""

    sequences: tuple[DispenseExecution, ...]

    @property
    def summary(self) -> DispenseSummary:
        modes: set[AppliedDispenseMode] = {s.applied_mode for s in self.sequences}
        applied_mode: AppliedDispenseMode | Literal["mixed"] | None
        if not modes:
            applied_mode = None
        elif len(modes) == 1:
            applied_mode = next(iter(modes))
        else:
            applied_mode = "mixed"
        commanded = sum(s.commanded_volume_ul for s in self.sequences)
        duration = sum(
            s.commanded_volume_ul / s.effective_rate_ul_s
            for s in self.sequences
            if s.effective_rate_ul_s > 0
        )
        return DispenseSummary(
            applied_mode=applied_mode,
            path_length_mm=sum(s.path_length_mm for s in self.sequences),
            commanded_volume_ul=commanded,
            prime_extra_volume_ul=sum(s.prime_extra_volume_ul for s in self.sequences),
            effective_rate_ul_s=commanded / duration if duration > 0 else 0.0,
            rotations=sum(s.rotations for s in self.sequences),
        )


def build_applicator(
    klipper: Klipper,
    stage: XYZStage,
    config: PasteDispenserConfig,
    *,
    rotations_per_ul: float | None = None,
    lift_height: float | None = None,
) -> PasteApplicator:
    """Machine 設定からディスペンサー HAL ごと :class:`PasteApplicator` を組み立てる.

    Args:
        klipper: Klipper クライアント
        stage: XYZ ステージ
        config: ``Machine.paste_dispenser``
        rotations_per_ul: μL → 回転数の係数 [rev/μL] の上書き（キャリブ検証ループで
            新値を反映した applicator を作り直す経路）。``None`` で config の値
        lift_height: 塗布後の上昇高さ [mm] の上書き。``None`` で config の値
    """
    dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=(
            config.rotations_per_ul if rotations_per_ul is None else rotations_per_ul
        ),
    )
    return PasteApplicator(klipper, dispenser, stage, config, lift_height=lift_height)


class PasteApplicator:
    """ポリゴンへのフィル塗布によるペースト塗布を制御する.

    ローディング・リトラクション・塗布を一貫して提供する。各ポリゴンに対して
    面（外周トレース＋牛耕式ジグザグ）／線／点のフォールバック階層で成分別のフィル経路を
    生成し、成分ごとにステージ移動と同期した連続吐出を行う。プライムと吐出は 1 つの
    連続ステッパー動作として実行し、G4 でプライム時間分待機した後にステージ移動を開始する。

    Example:
        with build_applicator(klipper, stage, machine.paste_dispenser) as applicator:
            applicator.retract()
            applicator.apply(pad.polygon, params=params, transform=pad_transform)
    """

    def __init__(
        self,
        klipper: Klipper,
        dispenser: PasteDispenser,
        stage: XYZStage,
        config: PasteDispenserConfig,
        *,
        lift_height: float | None = None,
    ) -> None:
        """設定は検証済みの ``Machine.paste_dispenser`` から写す（ここでは再検証しない）."""
        self._klipper = klipper
        self._dispenser = dispenser
        self._stage = stage
        self._config = config
        self._settings = DispenseSettings.from_config(config, lift_height=lift_height)
        self._default_params = PasteParams.from_config(config)
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    @property
    def default_params(self) -> PasteParams:
        """Machine 設定由来の塗布パラメータ（pad 個別設定が無いときに使う）."""
        return self._default_params

    @property
    def rotations_per_ul(self) -> float:
        return self._dispenser.rotations_per_ul

    def __enter__(self) -> Self:
        """ディスペンサーを有効化する（AirPump ON + Stepper Enable）."""
        self._klipper.send_gcode(self._dispenser.enable())
        return self

    def __exit__(self, *args: object) -> None:
        """ディスペンサーを無効化する（AirPump OFF + Stepper Disable）."""
        self._klipper.send_gcode(self._dispenser.disable())

    def load(self, amount_ul: float) -> None:
        """指定量のペーストを押し出す（ブロッキング）.

        Args:
            amount_ul: 押し出し量 [μL]（正: 吐出、負: リトラクション）
        """
        self._logger.info("ペーストローディング: %s μL", amount_ul)
        push = self._dispenser.pushpull(
            amount_ul, self._settings.retract_rate, self._settings.retract_accel
        )
        self._klipper.send_gcode(push + GCode.wait_for_done())
        self._logger.info("ローディング完了")

    def load_rotations(
        self,
        rotations: float,
        rate: float,
        accel: float,
        *,
        retract_rotations: float = 0.0,
    ) -> None:
        """指定回転数でペーストを押し出す（ブロッキング）.

        初期 ``rotations_per_ul`` が未確定のローディング・質量計測用に、μL 単位を経由せず
        raw rotation で動かす。``retract_rotations > 0`` なら押出（``rotations > 0``）直後に
        同じ速度で逆回転して引き戻す（吸引には適用しない）。

        Args:
            rotations: 回転数 [rev]（正: 吐出、負: 吸引）
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
            retract_rotations: 押出直後に引き戻す回転数 [rev]（0 で無効）
        """
        self._rotate(rotations, rate, accel)
        if rotations > 0 and retract_rotations > 0:
            self._rotate(-retract_rotations, rate, accel)

    def _rotate(self, rotations: float, rate: float, accel: float) -> None:
        self._logger.info(
            "ペーストローディング: %s rev @ %s rev/s, accel=%s rev/s^2",
            rotations,
            rate,
            accel,
        )
        gc = self._dispenser.rotate_revolutions(rotations, rate, accel)
        self._klipper.send_gcode(gc + GCode.wait_for_done())
        self._logger.info("ローディング完了")

    def retract(self) -> None:
        """リトラクション量分だけペーストを引き戻す（ブロッキング）."""
        self.load(-self._settings.retract_amount)

    def apply(
        self,
        polygon: Polygon,
        *,
        params: PasteParams,
        transform: Transform,
        line_reference: Point2d | None = None,
    ) -> PasteApplicationResult:
        """Pad 1 枚（ポリゴン 1 つ）へペースト塗布を実行する（ブロッキング）.

        成分別フィル経路を生成し、各成分を独立した ``FillSequence`` として送信する。
        ``FillSequence.to_gcode`` が先頭点上空への travel → 下降 → 吐出 → retract →
        ``lift_height`` 上昇を 1 本に組むため、成分間の移動は連続送信だけで実現される。
        塗布量はポリゴン面積 × ``ul_per_mm2`` を成分数で均等配分する。

        Args:
            polygon: 塗布対象のポリゴン（board 座標, mm）
            params: この pad の解決済み塗布パラメータ
            transform: この pad の board 座標 → 機械座標変換
            line_reference: 線走行方向の基準にする部品位置（board 座標）
        """
        plan = FillPlan.for_pad(
            polygon, config=self._config, params=params, line_reference=line_reference
        )
        if not plan.paths:
            self._logger.warning("フィルパスが空です。スキップします。")
            return PasteApplicationResult(())

        per_component_ul = polygon.area * params.ul_per_mm2 / len(plan.paths)
        return PasteApplicationResult(
            tuple(
                self._draw_polyline(
                    path,
                    applied_mode=plan.dispense_mode,
                    amount_ul=per_component_ul,
                    paste_height_mm=params.paste_height_mm,
                    prime_extra_delay=params.prime_extra_delay,
                    transform=transform,
                )
                for path in plan.paths
            )
        )

    def deposit_at(
        self,
        point: Point2d,
        *,
        amount_ul: float,
        transform: Transform,
        params: PasteParams | None = None,
    ) -> PasteApplicationResult:
        """指定点へ ``amount_ul`` [μL] を点塗布する.

        通常塗布と同じ ``FillSequence`` を使い、接近→下降→prime+吐出→速度 0 から
        リトラクション・上昇同時開始の protocol で実行する。

        Args:
            point: 塗布点（``transform`` 適用前の座標）
            amount_ul: 塗布量 [μL]（正）
            transform: 塗布点に適用する座標変換
            params: 塗布高さ・プライム遅延に使うパラメータ（``None`` で machine 既定）
        """
        params = self._default_params if params is None else params
        return PasteApplicationResult(
            (
                self._draw_polyline(
                    (point,),
                    applied_mode="dot",
                    amount_ul=amount_ul,
                    paste_height_mm=params.paste_height_mm,
                    prime_extra_delay=params.prime_extra_delay,
                    transform=transform,
                ),
            )
        )

    def draw_line(
        self,
        start: Point2d,
        end: Point2d,
        *,
        amount_ul: float,
        transform: Transform,
        fill_speed: float | None = None,
        rate_cap: float | None = None,
    ) -> DispenseExecution:
        """1 本の直線を ``amount_ul`` [μL] で塗布する（キャリブ用プリミティブ）.

        ``apply`` のポリゴン経路生成を通さず ``[start, end]`` を直接 1 本の
        ``FillSequence`` として送信する。塗布高さ・プライム遅延は machine 既定を使う。

        Args:
            start: 線の始点（``transform`` 適用前の座標）
            end: 線の終点
            amount_ul: 塗布量 [μL]
            transform: 座標変換
            fill_speed: この線の塗布移動速度 [mm/sec]（``None`` で ``max_fill_speed``。
                速度スイープのように既定値を超える速度を測りたいとき上書きする）
            rate_cap: 吐出レートの頭打ち値 [μL/sec]（``None`` で ``max_dispense_rate``、
                ``math.inf`` で cap 無効）
        """
        return self._draw_polyline(
            (start, end),
            applied_mode="line",
            amount_ul=amount_ul,
            paste_height_mm=self._default_params.paste_height_mm,
            prime_extra_delay=self._default_params.prime_extra_delay,
            transform=transform,
            fill_speed=fill_speed,
            rate_cap=rate_cap,
        )

    def _draw_polyline(
        self,
        raw: Sequence[Point2d],
        *,
        applied_mode: AppliedDispenseMode,
        amount_ul: float,
        paste_height_mm: float,
        prime_extra_delay: float,
        transform: Transform,
        fill_speed: float | None = None,
        rate_cap: float | None = None,
    ) -> DispenseExecution:
        """1 本のポリラインを ``amount_ul`` [μL] で塗布する共通プリミティブ.

        各点に Z を付与 → ``transform`` 適用 → ``FillSequence`` 送信を 1 本ぶん行う。
        ``apply`` の各成分・``deposit_at``・``draw_line`` が共用する。
        """
        path = Path(p.to3d(paste_height_mm) for p in raw).transformed(transform)
        sequence = FillSequence(
            path=path,
            total_amount_ul=amount_ul,
            settings=self._settings,
            prime_extra_delay=prime_extra_delay,
            fill_speed=fill_speed,
            rate_cap=rate_cap,
        )
        self._klipper.send_gcode(
            sequence.to_gcode(self._stage, self._dispenser) + GCode.wait_for_done()
        )
        prime_extra = sequence.prime_extra_volume_ul
        return DispenseExecution(
            applied_mode=applied_mode,
            path_length_mm=path.length(),
            commanded_volume_ul=amount_ul,
            prime_extra_volume_ul=prime_extra,
            effective_rate_ul_s=sequence.effective_rate,
            rotations=(amount_ul + prime_extra) * self._dispenser.rotations_per_ul,
            fill_speed=sequence.actual_fill_speed(),
        )
