"""`webui.jobs.board_ops` の仕様テスト.

計画書 memory/agents/orchestrator/region-pad-align-plan.md「凍結する公開 IF」
「webui.jobs.board_ops」に基づく。

pad_align_abort_message:

- max_failures is None → None（無制限。board_tour が使用）
- 失敗数 <= 許容数 → None（境界: 失敗数 == 許容数は許容）
- 超過 → 失敗数・許容数・全失敗領域ラベルを含む日本語メッセージ文字列
  （メッセージは部分一致で検証する。完全一致は禁止）

align_pad_regions は領域単位の銅箔照合ループの共通骨格（progress → checkpoint
→ session.align → 集計）。JobContext は JobManager だけが生成する契約
（test_context.py の踏襲）のため、合成ジョブを manager 経由で実行し worker に
渡された ctx で検証する。PadAlignmentSession は自前クラスのためこの粒度の
テストでは mocker.Mock に置き換える（ループ骨格の検証が主眼で、照合自体の
振る舞いは test_alignment.py がカバーする）。
"""

from __future__ import annotations

import pytest
import shapely
from pytest_mock import MockerFixture

from pcbasm.geometry import Point2d, Rotation, Shift, Transform
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl.copper import RigidEdgeMatch
from pcbasm.posctrl.pad import PadAlignmentResult, PadRegion
from pcbasm.vision import Offset
from webui.jobs.board_ops import align_pad_regions, pad_align_abort_message
from webui.jobs.catalog import JobCatalog
from webui.jobs.context import JobContext
from webui.jobs.manager import JobManager, JobStatus

from .conftest import WaitUntil, register_synthetic as _register

PPM = 10.0  # pixel/mm


class TestPadAlignAbortMessage:
    """pad_align_abort_message: 失敗領域数が許容数を超えたときだけ中止メッセージを返す."""

    @pytest.mark.parametrize(
        ("failed", "max_failures"),
        [
            ([], 0),
            (["C0R0"], 1),
            (["C0R0", "C1R0"], 2),  # 境界: 失敗数 == 許容数は許容
        ],
    )
    def test_within_limit_returns_none(self, failed: list[str], max_failures: int):
        assert pad_align_abort_message(failed, max_failures) is None

    def test_none_max_failures_means_unlimited(self):
        assert pad_align_abort_message(["C0R0", "C1R0", "C2R0"], None) is None

    def test_exceeding_limit_returns_message_with_counts_and_region_label(self):
        message = pad_align_abort_message(["C0R0"], 0)

        assert message is not None
        assert "失敗 1" in message
        assert "許容 0" in message
        assert "C0R0" in message
        assert "領域" in message  # 部品単位から領域単位への文言更新

    def test_message_lists_every_failed_region_label(self):
        message = pad_align_abort_message(["C0R0", "C1R0", "C2R1"], 2)

        assert message is not None
        assert "失敗 3" in message
        assert "許容 2" in message
        assert "C0R0" in message
        assert "C1R0" in message
        assert "C2R1" in message


def _pad(designator: str, x: float, y: float, half: float = 0.4) -> Pad:
    """中心 (x, y) の正方形padを作る."""
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=shapely.Polygon(
            [
                (x - half, y - half),
                (x + half, y - half),
                (x + half, y + half),
                (x - half, y + half),
            ]
        ),
    )


def _region(key: tuple[int, int], pad: Pad) -> PadRegion:
    """`key`のセル（10mm角、board原点固定グリッド）に`pad`を1つ持つPadRegion."""
    x0, y0 = key[0] * 10.0, key[1] * 10.0
    return PadRegion(key=key, bounds=(x0, y0, x0 + 10.0, y0 + 10.0), pads=(pad,))


def _dummy_match() -> RigidEdgeMatch:
    """表示用フィールドを埋めるだけの照合結果."""
    return RigidEdgeMatch(
        offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
        rotation=Rotation(0.0),
        center_mm=Point2d(0.0, 0.0),
        mean_distance_px=0.0,
    )


def _result(machine_transform: Transform, anchor: Point2d) -> PadAlignmentResult:
    """machine_transform と anchor のみ可変の PadAlignmentResult を作る."""
    return PadAlignmentResult(
        machine_transform=machine_transform,
        match=_dummy_match(),
        anchor=anchor,
        adjusted_position=anchor,
        roi=(0, 0, 10, 10),
    )


class TestAlignPadRegions:
    """align_pad_regions のループ骨格（成功収集・on_failure・max_failures 中止）のテスト."""

    def test_collects_successful_alignments_in_order(
        self,
        catalog: JobCatalog,
        manager: JobManager,
        wait_until: WaitUntil,
        mocker: MockerFixture,
    ):
        """成功した(region, alignment)を対象領域の順にすべて集めて返す."""
        region_a = _region((0, 0), _pad("R1", 5.0, 5.0))
        region_b = _region((1, 0), _pad("R2", 15.0, 5.0))
        result_a = _result(Shift(0.1, 0.0), anchor=Point2d(5.0, 5.0))
        result_b = _result(Shift(0.2, 0.0), anchor=Point2d(15.0, 5.0))
        session = mocker.Mock()
        session.align.side_effect = [result_a, result_b]
        captured: list[list[tuple[PadRegion, PadAlignmentResult]]] = []

        def run(ctx: JobContext) -> None:
            captured.append(align_pad_regions(ctx, session, [region_a, region_b]))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured == [[(region_a, result_a), (region_b, result_b)]]
        assert session.align.call_args_list == [
            mocker.call(region_a),
            mocker.call(region_b),
        ]

    def test_calls_on_failure_with_region_and_index_and_excludes_it_from_result(
        self,
        catalog: JobCatalog,
        manager: JobManager,
        wait_until: WaitUntil,
        mocker: MockerFixture,
    ):
        """失敗した領域は on_failure(region, index) を呼び、戻り値には含めない."""
        region_a = _region((0, 0), _pad("R1", 5.0, 5.0))
        region_b = _region((1, 0), _pad("R2", 15.0, 5.0))
        result_a = _result(Shift(0.1, 0.0), anchor=Point2d(5.0, 5.0))
        session = mocker.Mock()
        session.align.side_effect = [result_a, None]
        on_failure = mocker.Mock()
        captured: list[list[tuple[PadRegion, PadAlignmentResult]]] = []

        def run(ctx: JobContext) -> None:
            captured.append(
                align_pad_regions(
                    ctx,
                    session,
                    [region_a, region_b],
                    on_failure=on_failure,
                    max_failures=None,
                )
            )

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured == [[(region_a, result_a)]]
        on_failure.assert_called_once_with(region_b, 1)

    def test_raises_value_error_and_stops_further_aligns_when_failures_exceed_max(
        self,
        catalog: JobCatalog,
        manager: JobManager,
        wait_until: WaitUntil,
        mocker: MockerFixture,
    ):
        """失敗数がmax_failuresを超えた時点でValueErrorとなり、以降のalignは呼ばれない."""
        regions = [
            _region((0, 0), _pad("R1", 5.0, 5.0)),
            _region((1, 0), _pad("R2", 15.0, 5.0)),
            _region((2, 0), _pad("R3", 25.0, 5.0)),
        ]
        session = mocker.Mock()
        session.align.return_value = None  # 全領域で照合失敗

        def run(ctx: JobContext) -> None:
            align_pad_regions(ctx, session, regions, max_failures=0)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert session.align.call_count == 1

    def test_completes_with_empty_result_when_every_region_fails_and_max_failures_is_none(
        self,
        catalog: JobCatalog,
        manager: JobManager,
        wait_until: WaitUntil,
        mocker: MockerFixture,
    ):
        """max_failures=None（board_tourが使用）なら全領域失敗でも例外を出さず完走する."""
        regions = [
            _region((0, 0), _pad("R1", 5.0, 5.0)),
            _region((1, 0), _pad("R2", 15.0, 5.0)),
        ]
        session = mocker.Mock()
        session.align.return_value = None
        captured: list[list[tuple[PadRegion, PadAlignmentResult]]] = []

        def run(ctx: JobContext) -> None:
            captured.append(align_pad_regions(ctx, session, regions, max_failures=None))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured == [[]]
        assert session.align.call_count == 2
