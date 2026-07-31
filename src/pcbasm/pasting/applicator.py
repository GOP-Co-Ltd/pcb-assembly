"""ペースト塗布の制御."""

import logging
from collections.abc import Iterable
from typing import Self

from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import (
    DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR,
    DEFAULT_AUTO_LINE_ASPECT_RATIO,
    DispenseMode,
    PasteDispenser as PasteDispenserConfig,
    PasteHeight,
    resolve_paste_height,
)
from pcbasm.geometry import (
    Identity,
    Path,
    Point2d,
    Transform,
)
from pcbasm.hal import Klipper, PasteDispenser, Speed, XYZStage
from pcbasm.pasting.fill_path import build_pad_fill_plan_for
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.settings import ResolvedPaste
from pcbasm.utils import get_class_module_path


class PasteApplicator:
    """ポリゴンへのフィル塗布によるペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    各ポリゴンに対して面（外周トレース＋牛耕式ジグザグ）／線／点の
    フォールバック階層で成分別のフィル経路を生成し、各成分ごとにステージ
    移動と同期した連続吐出を行う。プライムと吐出は1つの連続ステッパー動作
    として実行し、G4でプライム時間分待機した後にステージ移動を開始する。

    Example:
        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_diameter=0.34,
            max_fill_speed=1.0,
            max_dispense_rate=5.0,
            dispense_accel=1.0,
            ul_per_mm2=0.05,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_height=0.5,
            dispense_mode="auto",
        ) as applicator:
            applicator.load(2.0)
            applicator.retract()
            applicator.apply([polygon], transform=Identity())
    """

    def __init__(
        self,
        klipper: Klipper,
        paste_dispenser: PasteDispenser,
        stage: XYZStage,
        nozzle_diameter: float,
        max_fill_speed: float,
        max_dispense_rate: float,
        dispense_accel: float,
        ul_per_mm2: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        transform: Transform = Identity(),
        paste_height: PasteHeight = "auto",
        lift_height: float = 2.0,
        dispense_mode: DispenseMode = "auto",
        auto_line_aspect_ratio: float = DEFAULT_AUTO_LINE_ASPECT_RATIO,
        auto_area_short_side_factor: float = DEFAULT_AUTO_AREA_SHORT_SIDE_FACTOR,
        prime_extra_delay: float = 0.0,
        bead_width_factor: float = 1.0,
        overlap: float = 0.0,
        boundary_margin: float = 0.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_diameter: ノズル内径 [mm]（例: 0.34）
            max_fill_speed: 連続塗布できる移動速度上限 [mm/sec]（主設定）
            max_dispense_rate: 吐出レート上限 [μL/sec]
            dispense_accel: 吐出加速度 [μL/sec²]
            ul_per_mm2: 1mm²あたりの塗布量 [μL/mm²]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]、または auto
            lift_height: 塗布後の上昇高さ [mm]
            dispense_mode: 塗布方式 auto / dot / line / area
            auto_line_aspect_ratio: Auto 時に線塗布へ切り替える縦横比
            auto_area_short_side_factor: Auto 時に面塗布へ切り替える短辺のノズル径倍率
            prime_extra_delay: プライム後の追加遅延 [sec]（デフォルト: 0.0）
            bead_width_factor: ビード幅係数（w = nozzle_diameter * bead_width_factor）
            overlap: ジグザグ行間オーバーラップ [0, 1)
            boundary_margin: 外周マージン [mm]

        Raises:
            ValueError: retraction_accel_factorが1.0以下の場合
        """
        if retraction_accel_factor <= 1.0:
            raise ValueError(
                f"retraction_accel_factorは1.0より大きい必要があります: "
                f"{retraction_accel_factor}"
            )
        if max_fill_speed <= 0:
            raise ValueError(
                f"max_fill_speedは正の値である必要があります: {max_fill_speed}"
            )
        if max_dispense_rate <= 0:
            raise ValueError(
                f"max_dispense_rateは正の値である必要があります: {max_dispense_rate}"
            )
        if auto_line_aspect_ratio <= 1.0:
            raise ValueError(
                "auto_line_aspect_ratioは1.0より大きい必要があります: "
                f"{auto_line_aspect_ratio}"
            )
        if auto_area_short_side_factor <= 0:
            raise ValueError(
                "auto_area_short_side_factorは正の値である必要があります: "
                f"{auto_area_short_side_factor}"
            )

        self._klipper = klipper
        self._paste_dispenser = paste_dispenser
        self._stage = stage
        self._nozzle_diameter = nozzle_diameter
        self._max_fill_speed = max_fill_speed
        self._max_dispense_rate = max_dispense_rate
        self._dispense_accel = dispense_accel
        self._ul_per_mm2 = ul_per_mm2
        self._transform = transform
        self._paste_height: PasteHeight = paste_height
        self._dispense_mode: DispenseMode = dispense_mode
        self._auto_line_aspect_ratio = auto_line_aspect_ratio
        self._auto_area_short_side_factor = auto_area_short_side_factor
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._lift_height = lift_height
        self._prime_extra_delay = prime_extra_delay
        self._bead_width_factor = bead_width_factor
        self._overlap = overlap
        self._boundary_margin = boundary_margin
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    @classmethod
    def from_config(
        cls,
        klipper: Klipper,
        paste_dispenser: PasteDispenser,
        stage: XYZStage,
        config: PasteDispenserConfig,
        *,
        transform: Transform = Identity(),
        lift_height: float | None = None,
    ) -> Self:
        """Machine 設定の paste_dispenser セクションから構築する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            config: ``Machine.paste_dispenser`` の設定
            transform: 座標変換
            lift_height: 塗布後の上昇高さ [mm]。None のとき config の値を使う

        Raises:
            ValueError: 設定値が ``__init__`` の検証に通らない場合
        """
        return cls(
            klipper=klipper,
            paste_dispenser=paste_dispenser,
            stage=stage,
            nozzle_diameter=config.nozzle_diameter,
            max_fill_speed=config.max_fill_speed,
            max_dispense_rate=config.max_dispense_rate,
            dispense_accel=config.dispense_accel,
            ul_per_mm2=config.ul_per_mm2,
            retraction=config.retract_amount,
            retraction_rate=config.effective_retract_rate,
            retraction_accel_factor=config.retract_accel_factor,
            transform=transform,
            paste_height=config.paste_height,
            lift_height=config.lift_height if lift_height is None else lift_height,
            dispense_mode=config.dispense_mode,
            auto_line_aspect_ratio=config.auto_line_aspect_ratio,
            auto_area_short_side_factor=config.auto_area_short_side_factor,
            prime_extra_delay=config.prime_extra_delay,
            bead_width_factor=config.bead_width_factor,
            overlap=config.overlap,
            boundary_margin=config.boundary_margin,
        )

    def __enter__(self) -> Self:
        """ディスペンサーを有効化する（AirPump ON + Stepper Enable）."""
        self._klipper.send_gcode(self._paste_dispenser.enable())
        return self

    def __exit__(self, *args: object) -> None:
        """ディスペンサーを無効化する（AirPump OFF + Stepper Disable）."""
        self._klipper.send_gcode(self._paste_dispenser.disable())

    @property
    def _retraction_accel(self) -> float:
        """リトラクション加速度 a_R [μL/sec²]."""
        return (
            self._retraction_accel_factor * self._retraction_rate**2 / self._retraction
        )

    def load(self, amount: float) -> None:
        """指定量のペーストを押し出す.

        GCodeを生成・送信し、動作完了まで待機（ブロッキング）する。

        Args:
            amount: 押し出し量 [μL]（正: 吐出、負: リトラクション）
        """
        self._logger.info(f"ペーストローディング: {amount} μL")
        push_gcode = self._paste_dispenser.pushpull(
            amount, self._retraction_rate, self._retraction_accel
        )
        self._klipper.send_gcode(push_gcode + gcode.wait_for_done())
        self._logger.info("ローディング完了")

    def load_rotations(self, rotations: float, rate: float, accel: float) -> None:
        """指定回転数でペーストを押し出す.

        初期 `rotations_per_ul` が未確定のローディング・質量計測用に、
        μL単位を経由せず raw rotation で動かす。

        Args:
            rotations: 回転数 [rev]（正: 吐出、負: 吸引）
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
        """
        self._logger.info(
            "ペーストローディング: %s rev @ %s rev/s, accel=%s rev/s^2",
            rotations,
            rate,
            accel,
        )
        gc = self._paste_dispenser.rotate_revolutions(rotations, rate, accel)
        self._klipper.send_gcode(gc + gcode.wait_for_done())
        self._logger.info("ローディング完了")

    def retract(self) -> None:
        """リトラクションを実行する.

        retraction量分だけペーストを引き戻す（ブロッキング）。
        """
        self.load(-self._retraction)

    def calibrate(self, rotations: float, rate: float, accel: float) -> None:
        """流量キャリブレーション用にN回転を実行する（ブロッキング）.

        Args:
            rotations: 回転数 [rev]
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
        """
        self.load_rotations(rotations, rate, accel)

    def apply(
        self,
        polygons: Iterable[Polygon],
        *,
        transform: Transform,
        paste_height: PasteHeight | None = None,
        ul_per_mm2: float | None = None,
        dispense_mode: DispenseMode | None = None,
        prime_extra_delay: float | None = None,
        bead_width_factor: float | None = None,
        overlap: float | None = None,
        boundary_margin: float | None = None,
    ) -> None:
        """複数ポリゴンへペースト塗布を実行する.

        各ポリゴンに対して成分別フィル経路を生成し、成分ごとに
        ステージ移動と同期して連続吐出を行う（ブロッキング）。

        キーワード引数は pad ごとの塗布設定の上書きで、``None`` の項目は
        ``__init__`` で与えた既定値を使う（後方互換）。渡した値は
        ``polygons`` の全ポリゴンに適用される。

        Args:
            polygons: 塗布対象のポリゴン群
            transform: このpadのboard座標→機械座標変換
            paste_height: 塗布面のZ高さ [mm]、または auto
            ul_per_mm2: 面積あたりのペースト量 [μL/mm²]
            dispense_mode: 塗布方式 auto / dot / line / area
            prime_extra_delay: プライム後の追加遅延 [sec]
            bead_width_factor: ビード幅係数（w = nozzle_diameter * factor）
            overlap: ジグザグ行間オーバーラップ [0, 1)
            boundary_margin: 外周マージン [mm]
        """
        paste = ResolvedPaste(
            enabled=True,
            dispense_mode=(
                self._dispense_mode if dispense_mode is None else dispense_mode
            ),
            paste_height=(self._paste_height if paste_height is None else paste_height),
            ul_per_mm2=self._ul_per_mm2 if ul_per_mm2 is None else ul_per_mm2,
            prime_extra_delay=(
                self._prime_extra_delay
                if prime_extra_delay is None
                else prime_extra_delay
            ),
            bead_width_factor=(
                self._bead_width_factor
                if bead_width_factor is None
                else bead_width_factor
            ),
            overlap=self._overlap if overlap is None else overlap,
            boundary_margin=(
                self._boundary_margin if boundary_margin is None else boundary_margin
            ),
        )
        for polygon in polygons:
            self._fill(polygon, paste=paste, transform=transform)

    def draw_line(
        self,
        start: Point2d,
        end: Point2d,
        *,
        amount: float,
        paste_height: PasteHeight | None = None,
        prime_extra_delay: float | None = None,
        max_fill_speed: float | None = None,
        rate_cap: float | None = None,
    ) -> Speed | None:
        """1 本の直線を ``amount`` [μL] で塗布する（キャリブ用プリミティブ）.

        ``apply`` のポリゴン経路生成を通さず、``[start, end]`` を直接 1 本の
        ``FillSequence`` として送信する。Z 補正・transform 適用・吐出同期は
        ``apply`` の塗布と同一機構（``_draw_polyline``）を共用する。

        Args:
            start: 線の始点（board 座標, mm）
            end: 線の終点（board 座標, mm）
            amount: 塗布量 [μL]
            paste_height: 塗布面のZ高さ [mm]、または auto。``None`` で既定値
            prime_extra_delay: プライム後の追加遅延 [sec]。``None`` で既定値
            max_fill_speed: 連続塗布できる移動速度上限 [mm/sec]。``None`` で既定値。
                ③ の速度スイープのように既定値を超える速度を測りたいとき上書きする
            rate_cap: 吐出レートの頭打ち値 [μL/sec]。``None``=max_dispense_rate、
                ``math.inf``=cap 無効（移動速度のみで律速）

        Returns:
            実効塗布移動速度（``Speed``）。経路長 0 などで引けないとき ``None``
        """
        resolved_paste_height = (
            self._paste_height if paste_height is None else paste_height
        )
        resolved_prime_extra_delay = (
            self._prime_extra_delay if prime_extra_delay is None else prime_extra_delay
        )
        return self._draw_polyline(
            [start, end],
            total_amount=amount,
            paste_height=resolved_paste_height,
            ul_per_mm2=self._ul_per_mm2,
            prime_extra_delay=resolved_prime_extra_delay,
            max_fill_speed=max_fill_speed,
            rate_cap=rate_cap,
            transform=self._transform,
        )

    def deposit_at(
        self,
        point: Point2d,
        *,
        amount: float,
        transform: Transform,
        paste_height: PasteHeight | None = None,
        prime_extra_delay: float | None = None,
        rate_cap: float | None = None,
    ) -> Speed | None:
        """指定点へ ``amount`` [μL] を点塗布する.

        通常塗布と同じ ``FillSequence`` を使い、接近→下降→prime+吐出→
        リトラクション→上昇の protocol で実行する。
        """
        if amount <= 0:
            raise ValueError(f"amountは正の値である必要があります: {amount}")
        resolved_paste_height = (
            self._paste_height if paste_height is None else paste_height
        )
        resolved_prime_extra_delay = (
            self._prime_extra_delay if prime_extra_delay is None else prime_extra_delay
        )
        return self._draw_polyline(
            [point],
            total_amount=amount,
            paste_height=resolved_paste_height,
            ul_per_mm2=self._ul_per_mm2,
            prime_extra_delay=resolved_prime_extra_delay,
            rate_cap=rate_cap,
            transform=transform,
        )

    def _fill(
        self, polygon: Polygon, *, paste: ResolvedPaste, transform: Transform
    ) -> None:
        """ポリゴンを成分別フィル経路で塗布する.

        各成分は独立した ``FillSequence`` として送信する。
        ``FillSequence.to_gcode`` が先頭点上空への travel → 下降 → 吐出 →
        retract → ``lift_height`` 上昇を1本に組むため、成分間の移動は
        ``FillSequence`` の連続送信だけで自然に実現される。``total_amount``
        は元ポリゴン面積ベース（``polygon.area * ul_per_mm2``）を成分数で
        均等配分する。塗布設定は呼び出し元（``apply``）が解決した実効値
        （:class:`ResolvedPaste`）を受け取り、経路生成はプレビューと共通の
        :func:`build_pad_fill_plan_for` に委譲する。
        """
        plan = build_pad_fill_plan_for(
            polygon,
            nozzle_diameter=self._nozzle_diameter,
            auto_line_aspect_ratio=self._auto_line_aspect_ratio,
            auto_area_short_side_factor=self._auto_area_short_side_factor,
            paste=paste,
        )
        if not plan.paths:
            self._logger.warning("フィルパスが空です。スキップします。")
            return

        total_amount = polygon.area * paste.ul_per_mm2
        per_component_amount = total_amount / len(plan.paths)

        for raw in plan.paths:
            self._draw_polyline(
                raw,
                total_amount=per_component_amount,
                paste_height=paste.paste_height,
                ul_per_mm2=paste.ul_per_mm2,
                prime_extra_delay=paste.prime_extra_delay,
                transform=transform,
            )

    def _draw_polyline(
        self,
        raw: list[Point2d],
        *,
        total_amount: float,
        paste_height: PasteHeight,
        ul_per_mm2: float,
        prime_extra_delay: float,
        transform: Transform,
        max_fill_speed: float | None = None,
        rate_cap: float | None = None,
    ) -> Speed | None:
        """1 本のポリラインを ``total_amount`` [μL] で塗布する（共通プリミティブ）.

        ``paste_height`` 解決 → 各点に Z 付与 → 渡された ``transform`` 適用 →
        ``FillSequence`` 送信を 1 本ぶん行う。``_fill`` の各成分と公開
        ``draw_line`` が共用する（塗布挙動を二重化しないためのキモ）。

        ``max_fill_speed`` は ``None`` で既定値（``self._max_fill_speed``）。③ の
        速度スイープのように per-line で既定値を超える速度を出したいとき上書きする。

        Returns:
            実効塗布移動速度（``Speed``）。経路長 0 などで塗布移動が無いとき ``None``
        """
        resolved_height = resolve_paste_height(paste_height, ul_per_mm2)
        path = Path(p.to3d(resolved_height) for p in raw).transformed(transform)
        sequence = FillSequence(
            path=path,
            total_amount=total_amount,
            retraction=self._retraction,
            max_fill_speed=(
                self._max_fill_speed if max_fill_speed is None else max_fill_speed
            ),
            max_dispense_rate=self._max_dispense_rate,
            dispense_accel=self._dispense_accel,
            retraction_rate=self._retraction_rate,
            retraction_accel=self._retraction_accel,
            prime_extra_delay=prime_extra_delay,
            lift_height=self._lift_height,
            travel_speed=Speed.rate(1.0),
            rate_cap=rate_cap,
        )
        self._klipper.send_gcode(
            sequence.to_gcode(self._stage, self._paste_dispenser)
            + gcode.wait_for_done()
        )
        return sequence.fill_speed_actual()
