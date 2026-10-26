"""3 パス撮影（全点の塗布前撮影 → 全点塗布 → 全点の塗布後撮影）.

塗布量の校正に使う dataset の収集と、塗布ジョブの運転時補正が同じ手順で撮る。
塗ってすぐ撮ると点ごとにペーストの落ち着き時間が変わるので、撮影と塗布をそれぞれ 1 パスにまとめる。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Protocol

import attrs

from pcbasm.geometry import Point2d, Transform
from pcbasm.pasting.applicator import PasteApplicationResult, PasteApplicator
from pcbasm.pasting.capture import PointCapturer
from pcbasm.pasting.hooks import ProcedureHooks
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.paste_volume.dataset.metadata import DatasetView
from pcbasm.pasting.session import PasteSession
from pcbasm.vision.crop import RectCrop

# prime 後の追加遅延は量に依らず一定量を押し出すので、指令量と塗布痕の対応が歪む。
# 校正の dataset も運転時補正もこの値で塗る（補正の比は指令量だけを分母に取る）。
PRIME_EXTRA_DELAY = 0.0

# 中心の 1 view だけを撮る
CENTER_VIEW = DatasetView(number=0)

# 静定待ちを刻む間隔 [秒]。この粒度で中断を拾う
_SETTLE_TICK_SEC = 1.0


@attrs.frozen
class DotSpot:
    """3 パス撮影で撮る 1 か所.

    Attributes:
        index: 結果の受け取り口へ渡す番号
        point: 撮影と塗布の中心（board 座標）
        transform: 塗布に使う board → machine 変換
        amount_ul: 塗布量 [μL]（``None`` は撮るだけ。blank セル）
        correction: 撮影に当てる機械座標の位置合わせ補正（位置合わせしない銅板は ``None``）
    """

    index: int
    point: Point2d
    transform: Transform
    amount_ul: float | None = None
    correction: Transform | None = None


class DotCaptureSink(Protocol):
    """3 パス撮影の結果の受け取り口."""

    def record_pre(self, index: int, view: DatasetView, crop: RectCrop) -> None: ...

    def record_execution(self, index: int, result: PasteApplicationResult) -> None: ...

    def record_post(
        self, index: int, view: DatasetView, crop: RectCrop
    ) -> str | None: ...


class DotCaptureRun:
    """3 パス撮影 1 回分.

    塗布前と塗布後は同じ ``views`` を同じ crop 寸法で撮る。
    塗布パスの直前に ``before_deposit`` の文脈へ入り、その applicator で塗る。
    AirPump を入れたまま撮影パスを回すとノズルからペーストが垂れるので、applicator を開くのは塗布パスだけにできる。
    """

    def __init__(
        self,
        session: PasteSession,
        hooks: ProcedureHooks,
        *,
        crop_size_px: int,
        params: PasteParams,
        views: Sequence[DatasetView] = (CENTER_VIEW,),
        settle_sec: float = 0.0,
        stage_prefix: str = "",
    ) -> None:
        """撮影器を組み、塗布パラメータの prime 後の追加遅延を :data:`PRIME_EXTRA_DELAY` にする.

        Args:
            session: Board 計測済みのセッション
            hooks: 経過の出力と中断の確認
            crop_size_px: 全 crop 共通のピクセル寸法
            params: 塗布高さなどの塗布パラメータ
            views: 1 か所ごとに撮る view（周辺 view はカメラ中心をずらして撮る）
            settle_sec: 全点を塗ってから塗布後の撮影に入るまでの待ち [秒]（0 で待たない）
            stage_prefix: 進捗の段階名と静定待ちのログに付ける接頭辞（塗布ジョブの中で、どの撮影かを示す）
        """
        self._hooks = hooks
        self._capturer = PointCapturer(
            session, crop_size_px=crop_size_px, frame_sink=hooks.frame
        )
        self._params = attrs.evolve(params, prime_extra_delay=PRIME_EXTRA_DELAY)
        self._views = tuple(views)
        self._settle_sec = settle_sec
        self._stage_prefix = stage_prefix

    @property
    def params(self) -> PasteParams:
        """実際に塗るパラメータ."""
        return self._params

    def run(
        self,
        spots: Sequence[DotSpot],
        sink: DotCaptureSink,
        *,
        before_deposit: Callable[[], AbstractContextManager[PasteApplicator]],
    ) -> str | None:
        """全点を撮り、塗り、静定を待ってからもう一度撮る.

        Returns:
            撮影に失敗した、または ``sink`` が塗布後の画像を拒んだときの理由（通しで撮れたら ``None``）
        """
        error = self._capture_pass(spots, sink.record_pre, "塗布前", 0.0, 45.0)
        if error is not None:
            return error

        self._hooks.checkpoint()
        deposits = [
            (spot, amount) for spot in spots if (amount := spot.amount_ul) is not None
        ]
        with before_deposit() as applicator:
            for position, (spot, amount) in enumerate(deposits):
                self._hooks.progress(
                    f"{self._stage_prefix}塗布",
                    _percent(position, len(deposits), 47, 55),
                )
                self._hooks.checkpoint()
                sink.record_execution(
                    spot.index,
                    applicator.deposit_at(
                        spot.point,
                        amount_ul=amount,
                        transform=spot.transform,
                        params=self._params,
                    ),
                )

        self._settle()
        return self._capture_pass(spots, sink.record_post, "塗布後", 55.0, 100.0)

    def _capture_pass(
        self,
        spots: Sequence[DotSpot],
        record: Callable[[int, DatasetView, RectCrop], str | None],
        phase: str,
        start: float,
        end: float,
    ) -> str | None:
        """全点の全 view を 1 パスで撮る（1 枚でも撮れなければ理由を返す）."""
        for position, spot in enumerate(spots):
            self._hooks.progress(
                f"{self._stage_prefix}{phase}撮影",
                _percent(position, len(spots), start, end),
            )
            self._hooks.checkpoint()
            for view in self._views:
                crop, error = self._capturer.capture(
                    spot.point,
                    offset=Point2d(view.offset_x_mm, view.offset_y_mm),
                    correction=spot.correction,
                )
                if crop is None:
                    return f"点 {spot.index} view {view.number} の{phase}撮影に失敗: {error}"
                error = record(spot.index, view, crop)
                if error is not None:
                    return error
        return None

    def _settle(self) -> None:
        """ペーストが広がりきるまで、中断を拾いながら待つ."""
        seconds = self._settle_sec
        if seconds <= 0:
            return
        self._hooks.progress(f"{self._stage_prefix}静定待ち {seconds:.0f} 秒")
        self._hooks.log(
            f"{self._stage_prefix}ペーストの静定を {seconds:.1f} 秒待ちます"
        )
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            self._hooks.checkpoint()
            time.sleep(min(_SETTLE_TICK_SEC, remaining))
        self._hooks.checkpoint()


def _percent(position: int, total: int, start: float, end: float) -> float:
    """パス内の進み具合を、3 パス全体の進捗 ``[start, end]`` へ写す."""
    return start + (end - start) * position / total
