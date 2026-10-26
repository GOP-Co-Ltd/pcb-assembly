"""3 パス撮影 DotCaptureRun の公開契約.

全点の塗布前撮影 → 全点塗布 → 全点の塗布後撮影の並びを固定する。

applicator を開く文脈は塗布パスだけを包み、受け取り口が拒んだらそこで打ち切る。

装置側は自前 HAL の fake。
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import attrs
import pytest

from pcbasm.geometry import Point2d
from pcbasm.hal.fake import FakeCamera, FakeKlipper
from pcbasm.pasting.applicator import PasteApplicationResult, PasteApplicator
from pcbasm.pasting.dot_capture import PRIME_EXTRA_DELAY, DotCaptureRun, DotSpot
from pcbasm.pasting.paste_volume.dataset.metadata import DatasetView
from pcbasm.pasting.session import PasteSession
from pcbasm.vision.crop import RectCrop
from tests.support.paste_session import (
    RecordingHooks,
    full_board_correction,
    paste_session,
    uniform_frame,
)

POINTS = (Point2d(10.0, 6.0), Point2d(13.0, 6.0))


@attrs.define
class _Sink:
    """受け取った結果と applicator の文脈の出入りを 1 本の列に記録する."""

    events: list[str] = attrs.Factory(list)
    post_error: str | None = None

    def record_pre(self, index: int, view: DatasetView, crop: RectCrop) -> None:
        self.events.append(f"pre {index}")

    def record_execution(self, index: int, result: PasteApplicationResult) -> None:
        self.events.append(f"deposit {index}")

    def record_post(self, index: int, view: DatasetView, crop: RectCrop) -> str | None:
        self.events.append(f"post {index}")
        return self.post_error


@pytest.fixture
def session() -> PasteSession:
    return paste_session(FakeCamera([uniform_frame()]), FakeKlipper())


def _spots(session: PasteSession, amounts: Sequence[float | None]) -> list[DotSpot]:
    correction = full_board_correction()
    return [
        DotSpot(
            index=index,
            point=point,
            transform=session.point_transform(point, correction),
            amount_ul=amount,
            correction=correction.alignment.correction_for(point),
        )
        for index, (point, amount) in enumerate(zip(POINTS, amounts, strict=True))
    ]


def _run(
    session: PasteSession,
    sink: _Sink,
    amounts: Sequence[float | None] = (0.05, 0.05),
    hooks: RecordingHooks | None = None,
    settle_sec: float = 0.0,
    stage_prefix: str = "",
) -> str | None:
    @contextmanager
    def deposit_pass() -> Iterator[PasteApplicator]:
        sink.events.append("open")
        with session.make_applicator() as applicator:
            yield applicator
        sink.events.append("close")

    run = DotCaptureRun(
        session,
        hooks or RecordingHooks(),
        crop_size_px=20,
        params=session.machine.require_solder().defaults,
        settle_sec=settle_sec,
        stage_prefix=stage_prefix,
    )
    return run.run(_spots(session, amounts), sink, before_deposit=deposit_pass)


class TestRun:
    def test_opens_the_applicator_only_between_the_capture_passes(
        self, session: PasteSession
    ):
        """AirPump を入れたまま撮影パスを回すと、ノズルからペーストが垂れる."""
        sink = _Sink()

        assert _run(session, sink) is None
        assert sink.events == [
            "pre 0",
            "pre 1",
            "open",
            "deposit 0",
            "deposit 1",
            "close",
            "post 0",
            "post 1",
        ]

    def test_a_spot_without_amount_is_captured_but_not_deposited(
        self, session: PasteSession
    ):
        """塗布量の無い点（blank セル）は撮るだけ."""
        sink = _Sink()

        assert _run(session, sink, amounts=(0.05, None)) is None
        assert sink.events == [
            "pre 0",
            "pre 1",
            "open",
            "deposit 0",
            "close",
            "post 0",
            "post 1",
        ]

    def test_stops_at_the_first_post_crop_the_sink_rejects(self, session: PasteSession):
        sink = _Sink(post_error="塗布後の画像が塗布前と同じ寸法ではありません")

        error = _run(session, sink)

        assert error == "塗布後の画像が塗布前と同じ寸法ではありません"
        assert sink.events[-1] == "post 0"

    def test_prefixes_the_stage_names_and_the_settle_log(self, session: PasteSession):
        """塗布ジョブの中で、どの撮影の段階かを運転者が見分けられる."""
        hooks = RecordingHooks()

        _run(
            session,
            _Sink(),
            hooks=hooks,
            settle_sec=0.01,
            stage_prefix="流量キャリブレーション: ",
        )

        assert hooks.stages
        assert all(s.startswith("流量キャリブレーション: ") for s in hooks.stages)
        assert "流量キャリブレーション: ペーストの静定を 0.0 秒待ちます" in hooks.logs


class TestParams:
    def test_only_the_prime_extra_delay_is_fixed(self, session: PasteSession):
        """追加遅延は量に依らず一定量を押し出し、指令量と塗布痕の対応を歪める."""
        params = attrs.evolve(
            session.machine.require_solder().defaults, prime_extra_delay=0.5
        )

        run = DotCaptureRun(session, RecordingHooks(), crop_size_px=20, params=params)

        assert run.params == attrs.evolve(params, prime_extra_delay=PRIME_EXTRA_DELAY)
