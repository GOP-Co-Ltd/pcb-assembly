"""`webui.jobs.board_ops` の公開ヘルパの仕様テスト.

region-affine-correction 計画書「公開 IF → measure_regions」「実装ステップ 5」が契約:

- 領域ごとに progress / checkpoint / `session.measure` を回し、成功した
  計測から BoardAlignment（基板全体の単一のアフィン補正）を返す
- 領域ごとの log は dx/dy/rms/sharpness に加えて passes と収束状況を出す
- BoardAlignment 構築後に判定材料をまとめて出す: 補正モデル / 並進 /
  スケール ppm / スキュー deg / 残差 RMS・最大 / 区ごとの残差。実機で
  「アフィンで足りているか、非線形が残っているか」を見る唯一の材料
- 並進へ縮退した（アンカーの広がり不足）ときと、収束しなかった区が
  あるときは警告 log を出すが、いずれも採用して続行する
- 失敗領域は警告 log の後 `on_failure` を呼んで**続行**する
  （board_tour は FAILED overlay の配信に使う）
- 成功数 < min_regions で ValueError（塗布ジョブを中止する根拠）
- 計画領域数 < min_regions なら 1 領域も計測せずに ValueError
  （ステージを動かす前に落とす）
- メッセージは部分一致で検証する。完全一致は禁止

`confirm_next_point` は「直行性テストを巡回先ごとのユーザー確認へ戻す」変更の
公開 IF が契約:

- 巡回先ラベル付きの confirm prompt を出し、「次へ」で True・「終了」で False
- prompt spec は kind="confirm" / default=True / true_label="次へ" /
  false_label="終了"、message に巡回先ラベルを含む（UI のボタン文言が
  「終了で正常終了」という意味を担うため契約として固定する）
- `while_waiting` を受け取ったらそのまま `ctx.prompt` へ渡す（応答待ちの間ライブ
  フレームを流し続けるための委譲。直行性テストの十字線常時表示がこれに依存する）
- prompt 待機中の abort が JobAborted として伝播することは JobContext.prompt
  一般の契約であり `test_manager.py::TestPrompt` が既に固定している（重複回避）。
  ポーリング待機中の abort / コールバック例外も同様に
  `test_manager.py::TestPromptWhileWaiting` が固定している

prompt 往復と measure_regions のループは実 JobManager + 実 JobContext を通す
（合成ジョブ経由。モックなし）。照合セッションだけは ``measure()`` の戻り値を
差し替える手書き stub（`_StubSession`）に置く。
"""

from collections.abc import Sequence
from typing import cast

import pytest

from pcbasm.geometry import Point2d
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    EdgeMatch,
    RegionAlignment,
    RegionAlignmentSession,
)
from pcbasm.vision import Offset
from webui.jobs.board_ops import confirm_next_point, measure_regions
from webui.jobs.catalog import JobCatalog
from webui.jobs.context import JobContext, PromptSpec
from webui.jobs.manager import JobManager, JobRecord, JobStatus

from .conftest import WaitUntil, register_synthetic

PPM = 10.0  # pixel/mm


class _StubSession:
    """``measure(region)`` の戻り値だけを差し替える照合セッションの代替.

    ``measure_regions`` がこのループで使うのは ``session.measure`` の
    戻り値（``RegionAlignment`` か照合失敗の ``None``）だけ。領域ごとの
    成功/失敗を実 session で作り分けるには合成画像を領域数だけ用意する
    必要があり、ループの契約（progress / checkpoint / log / on_failure /
    min_regions 判定）の検証から遠ざかる。実 session 自体の HAL 結合と
    失敗の握りつぶしは tests/pcbasm/posctrl/test_alignment.py が
    実 projector・実 Canny・FakeCamera で押さえている。
    """

    def __init__(self, outcomes: Sequence[RegionAlignment | None]) -> None:
        self._outcomes = list(outcomes)
        self.measured: list[AlignmentRegion] = []

    def measure(self, region: AlignmentRegion) -> RegionAlignment | None:
        index = len(self.measured)
        self.measured.append(region)
        return self._outcomes[index]


# 非共線かつ y 方向にも 2.0mm 以上広がったアンカー（アフィン当てはめが成立する配置）
SPREAD_ANCHORS = [
    Point2d(0.0, 0.0),
    Point2d(20.0, 0.0),
    Point2d(0.0, 15.0),
    Point2d(20.0, 15.0),
]
# 1 行に並んだアンカー（spread = 0 なので並進へ縮退する）
COLLINEAR_ANCHORS = [Point2d(10.0 * index, 5.0) for index in range(4)]


def _region(index: int, anchor: Point2d | None = None) -> AlignmentRegion:
    return AlignmentRegion(
        index=index,
        anchor=anchor if anchor is not None else COLLINEAR_ANCHORS[index],
        roi=(0, 0, 100, 100),
        constraint=120.0,
        edge_point_count=240,
    )


def _alignment(
    index: int,
    displacement: Point2d,
    *,
    anchor: Point2d | None = None,
    passes: int = 2,
    converged: bool = True,
) -> RegionAlignment:
    return RegionAlignment(
        region=_region(index, anchor),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
            rms_distance_px=0.42,
            sharpness=0.678,
        ),
        displacement=displacement,
        # 収束した区の増分は微小、収束しなかった区は converge_tolerance 超
        increment=Point2d(0.002, 0.0) if converged else Point2d(0.05, 0.0),
        passes=passes,
        converged=converged,
    )


def _run_confirm_job(
    manager: JobManager,
    catalog: JobCatalog,
    wait_until: WaitUntil,
    labels: list[str],
    answers: list[bool],
) -> tuple[JobRecord, list[bool], list[PromptSpec]]:
    """合成ジョブ内で label ごとに confirm_next_point を呼び、戻り値列と spec 列を返す."""
    results: list[bool] = []

    def run(ctx: JobContext) -> None:
        for label in labels:
            results.append(confirm_next_point(ctx, label))

    register_synthetic(
        catalog, run, name="confirm", label="confirm_next_point 検証ジョブ"
    )
    record = manager.start("confirm", {})

    specs: list[PromptSpec] = []
    answered: set[str] = set()
    for answer in answers:
        wait_until(
            lambda: (pending := record.pending_prompt) is not None
            and pending[0] not in answered,
            timeout=60.0,
        )
        pending = record.pending_prompt
        assert pending is not None
        specs.append(pending[1])
        manager.respond_prompt(pending[0], answer)
        answered.add(pending[0])

    wait_until(lambda: record.status.terminal, timeout=60.0)
    return record, results, specs


def _run_measure_job(
    manager: JobManager,
    catalog: JobCatalog,
    wait_until: WaitUntil,
    session: _StubSession,
    regions: list[AlignmentRegion],
    *,
    min_regions: int,
    on_failure=None,
) -> tuple[JobRecord, dict[str, object]]:
    """合成ジョブ内で measure_regions を呼び、戻り値または ValueError を持ち帰る."""
    outcome: dict[str, object] = {}

    def run(ctx: JobContext) -> None:
        try:
            outcome["board"] = measure_regions(
                ctx,
                cast(RegionAlignmentSession, session),
                regions,
                min_regions=min_regions,
                on_failure=on_failure,
            )
        except ValueError as exc:
            outcome["error"] = str(exc)

    register_synthetic(catalog, run, name="measure", label="measure_regions 検証ジョブ")
    record = manager.start("measure", {})
    wait_until(lambda: record.status.terminal, timeout=60.0)
    return record, outcome


class TestMeasureRegions:
    """measure_regions: 領域照合ループの成功集約・失敗続行・不足中止・判定材料ログ."""

    @staticmethod
    def _affine_case() -> tuple[list[AlignmentRegion], _StubSession]:
        """アフィン当てはめが成立する 4 区（成功のみ）の入力."""
        displacements = [
            Point2d(0.10, -0.20),
            Point2d(0.20, -0.40),
            Point2d(0.30, -0.30),
            Point2d(0.40, -0.10),
        ]
        regions = [
            _region(index, anchor) for index, anchor in enumerate(SPREAD_ANCHORS)
        ]
        session = _StubSession(
            [
                _alignment(index, displacement, anchor=anchor)
                for index, (anchor, displacement) in enumerate(
                    zip(SPREAD_ANCHORS, displacements, strict=True)
                )
            ]
        )
        return regions, session

    def test_all_regions_succeed_gives_the_affine_correction(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """全領域成功 → 全区の計測から当てはめた BoardAlignment を返す."""
        regions, session = self._affine_case()

        record, outcome = self._run(
            manager, catalog, wait_until, session, regions, min_regions=4
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        board = outcome["board"]
        assert isinstance(board, BoardAlignment)
        assert len(board.results) == 4
        assert board.model == "affine"
        assert board.translation.x == pytest.approx(0.25)
        assert board.translation.y == pytest.approx(-0.25)

    def test_logs_per_region_metrics_including_passes(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """領域ごとに dx/dy/rms/sharpness/passes を log する.

        実機チューニングは全てこのログを見て行う（sharpness が閾値に近ければ min_sharpness
        を下げる、passes の増分が常に微小なら max_passes=1 に落とす）。
        """
        regions, session = self._affine_case()

        record, _ = self._run(
            manager, catalog, wait_until, session, regions, min_regions=1
        )

        text = "\n".join(record.log_lines)
        assert "dx=+0.1000" in text
        assert "dy=-0.2000" in text
        assert "rms=0.42" in text
        assert "sharpness=0.678" in text
        assert "passes=2" in text

    def test_logs_the_correction_model_scale_and_residuals(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """補正モデル・並進・スケール ppm・スキュー・残差（全体と区ごと）を log する.

        「アフィンで足りているか、非線形なひずみが残っているか」をユーザーが
        実機で判定する唯一の材料。残差が出ていなければ判定できない。
        """
        regions, session = self._affine_case()

        record, outcome = self._run(
            manager, catalog, wait_until, session, regions, min_regions=1
        )

        board = outcome["board"]
        assert isinstance(board, BoardAlignment)
        assert board.residual_rms > 0.0  # この入力は純アフィンではない
        text = "\n".join(record.log_lines)
        assert "affine" in text
        assert "dx=+0.2500" in text  # 重心での並進
        assert "ppm" in text  # スケール偏差
        assert "スキュー" in text
        assert "残差" in text
        assert "RMS" in text
        assert "領域 1:" in text  # 区ごとの残差行

    def test_warns_when_the_model_degenerates_to_translation(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """アンカーが 1 行（広がり不足）なら並進へ縮退した旨を警告 log する.

        縮退したこと自体は正しい挙動だが、region_size_px を下げて区の配置を
        広げれば改善するので、ユーザーに気づける形で出す。
        """
        regions = [
            _region(index, anchor) for index, anchor in enumerate(COLLINEAR_ANCHORS)
        ]
        session = _StubSession(
            [
                _alignment(index, Point2d(0.10 * (index + 1), -0.20), anchor=anchor)
                for index, anchor in enumerate(COLLINEAR_ANCHORS)
            ]
        )

        record, outcome = self._run(
            manager, catalog, wait_until, session, regions, min_regions=1
        )

        board = outcome["board"]
        assert isinstance(board, BoardAlignment)
        assert board.model == "translation"
        text = "\n".join(record.log_lines)
        assert "警告" in text
        assert "広がり" in text
        assert "translation" in text

    def test_warns_when_a_region_did_not_converge(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """収束しなかった区は採用しつつ、区ごとの行と全体の警告に出す（裁定 3）."""
        regions = [
            _region(index, anchor) for index, anchor in enumerate(SPREAD_ANCHORS)
        ]
        session = _StubSession(
            [
                _alignment(0, Point2d(0.10, -0.20), anchor=SPREAD_ANCHORS[0]),
                _alignment(1, Point2d(0.20, -0.40), anchor=SPREAD_ANCHORS[1]),
                _alignment(2, Point2d(0.30, -0.30), anchor=SPREAD_ANCHORS[2]),
                _alignment(
                    3,
                    Point2d(0.40, -0.10),
                    anchor=SPREAD_ANCHORS[3],
                    converged=False,
                ),
            ]
        )

        record, outcome = self._run(
            manager, catalog, wait_until, session, regions, min_regions=1
        )

        board = outcome["board"]
        assert isinstance(board, BoardAlignment)
        assert len(board.results) == 4  # 収束しなくても採用する
        text = "\n".join(record.log_lines)
        assert "収束" in text
        assert "警告" in text

    def test_failed_region_invokes_on_failure_and_continues(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """照合失敗（None）の領域では on_failure を呼び、残りの領域を続行する."""
        regions = [_region(0), _region(1), _region(2)]
        session = _StubSession(
            [
                _alignment(0, Point2d(0.10, -0.20)),
                None,
                _alignment(2, Point2d(0.30, -0.40)),
            ]
        )
        failed: list[AlignmentRegion] = []

        record, outcome = self._run(
            manager,
            catalog,
            wait_until,
            session,
            regions,
            min_regions=2,
            on_failure=failed.append,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        board = outcome["board"]
        assert isinstance(board, BoardAlignment)
        assert len(board.results) == 2
        assert [r.index for r in failed] == [1]
        assert [r.index for r in session.measured] == [0, 1, 2]
        assert "警告" in "\n".join(record.log_lines)

    def test_too_few_successes_aborts(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """成功領域数が min_regions を下回ったら ValueError で中止する."""
        regions = [_region(0), _region(1)]
        session = _StubSession([_alignment(0, Point2d(0.1, -0.2)), None])

        _, outcome = self._run(
            manager, catalog, wait_until, session, regions, min_regions=2
        )

        error = outcome.get("error")
        assert isinstance(error, str)
        assert "成功 1" in error
        assert "必要 2" in error
        assert "計画 2" in error

    def test_too_few_planned_regions_aborts_before_measuring(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        """計画領域数が min_regions 未満なら 1 領域も計測せずに中止する.

        ステージを動かす前に落とすことで、無駄な巡回と誤補正を避ける。
        """
        session = _StubSession([])

        _, outcome = self._run(
            manager, catalog, wait_until, session, [_region(0)], min_regions=3
        )

        error = outcome.get("error")
        assert isinstance(error, str)
        assert "1 個" in error
        assert "必要 3" in error
        # 撤去した設定キーを案内しない（タイル張りでは区数の上限が無い）
        assert "region_count" not in error
        assert session.measured == []

    @staticmethod
    def _run(
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        session: _StubSession,
        regions: list[AlignmentRegion],
        *,
        min_regions: int,
        on_failure=None,
    ) -> tuple[JobRecord, dict[str, object]]:
        return _run_measure_job(
            manager,
            catalog,
            wait_until,
            session,
            regions,
            min_regions=min_regions,
            on_failure=on_failure,
        )


class TestConfirmNextPoint:
    """confirm_next_point: 巡回先ごとの確認プロンプトと戻り値の契約."""

    def test_next_returns_true_and_quit_returns_false(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """「次へ」応答は True、「終了」応答は False（周回の継続/終了判定の根拠）."""
        record, results, _ = _run_confirm_job(
            manager,
            catalog,
            wait_until,
            ["Top-Left", "Grid 1/4"],
            [True, False],
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert results == [True, False]

    def test_prompt_spec_pins_confirm_kind_labels_and_default(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """Prompt spec は confirm / default True / 「次へ」「終了」ラベル固定."""
        _, _, specs = _run_confirm_job(
            manager, catalog, wait_until, ["Top-Left"], [True]
        )

        assert len(specs) == 1
        spec = specs[0]
        assert spec.kind == "confirm"
        assert spec.default is True
        assert spec.true_label == "次へ"
        assert spec.false_label == "終了"

    def test_prompt_message_contains_the_point_label(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """どの巡回先での確認かをユーザーが判別できるよう label を message に含む."""
        _, _, specs = _run_confirm_job(
            manager, catalog, wait_until, ["Bottom-Right"], [True]
        )

        assert "Bottom-Right" in specs[0].message

    def test_while_waiting_callback_runs_during_the_wait(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """`while_waiting` は ctx.prompt へ委譲され、応答待ちの間繰り返し呼ばれる."""
        polls: list[int] = []
        results: list[bool] = []

        def run(ctx: JobContext) -> None:
            results.append(
                confirm_next_point(
                    ctx, "Top-Left", while_waiting=lambda: polls.append(1)
                )
            )

        register_synthetic(
            catalog, run, name="confirm_polling", label="while_waiting 検証ジョブ"
        )
        record = manager.start("confirm_polling", {})
        wait_until(lambda: len(polls) >= 2, timeout=60.0)

        pending = record.pending_prompt
        assert pending is not None
        manager.respond_prompt(pending[0], True)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert results == [True]
