"""`web.api.jobs.board_ops` の公開ヘルパの仕様テスト.

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

prompt 往復は実 JobManager + 実 JobContext を通す（合成ジョブ経由。モックなし）。
"""

from web.api.jobs.board_ops import confirm_next_point
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.context import JobContext, PromptSpec
from web.api.jobs.manager import JobManager, JobRecord, JobStatus

from .conftest import WaitUntil, register_synthetic


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
        """Prompt spec は confirm / default True / 「次へ」「終了」ラベル固定.

        どの巡回先での確認かをユーザーが判別できるよう label も message に含む。
        """
        _, _, specs = _run_confirm_job(
            manager, catalog, wait_until, ["Bottom-Right"], [True]
        )

        assert len(specs) == 1
        spec = specs[0]
        assert spec.kind == "confirm"
        assert spec.default is True
        assert spec.true_label == "次へ"
        assert spec.false_label == "終了"
        assert "Bottom-Right" in spec.message

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
