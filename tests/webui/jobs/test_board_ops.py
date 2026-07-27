"""`webui.jobs.board_ops` の公開ヘルパの仕様テスト.

paste-align-max-failures 計画書「公開 IF」節が `pad_align_abort_message` の契約:

- max_failures is None → None（無制限。board_tour が使用）
- 失敗数 <= 許容数 → None（境界: 失敗数 == 許容数は許容）
- 超過 → 失敗数・許容数・全 designator を含む日本語メッセージ文字列
  （メッセージは部分一致で検証する。完全一致は禁止）

`confirm_next_point` は「直行性テストを巡回先ごとのユーザー確認へ戻す」変更の
公開 IF が契約:

- 巡回先ラベル付きの confirm prompt を出し、「次へ」で True・「終了」で False
- prompt spec は kind="confirm" / default=True / true_label="次へ" /
  false_label="終了"、message に巡回先ラベルを含む（UI のボタン文言が
  「終了で正常終了」という意味を担うため契約として固定する）
- prompt 待機中の abort が JobAborted として伝播することは JobContext.prompt
  一般の契約であり `test_manager.py::TestPrompt` が既に固定している（重複回避）

prompt 往復は実 JobManager + 実 JobContext を通す（合成ジョブ経由。モックなし）。
"""

import pytest

from webui.jobs.board_ops import confirm_next_point, pad_align_abort_message
from webui.jobs.catalog import JobCatalog
from webui.jobs.context import JobContext, PromptSpec
from webui.jobs.manager import JobManager, JobRecord, JobStatus

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


class TestPadAlignAbortMessage:
    """pad_align_abort_message: 失敗数が許容数を超えたときだけ中止メッセージを返す."""

    @pytest.mark.parametrize(
        ("failed", "max_failures"),
        [
            ([], 0),
            (["R1"], 1),
            (["R1", "R2"], 2),  # 境界: 失敗数 == 許容数は許容
        ],
    )
    def test_within_limit_returns_none(self, failed: list[str], max_failures: int):
        assert pad_align_abort_message(failed, max_failures) is None

    def test_none_max_failures_means_unlimited(self):
        assert pad_align_abort_message(["R1", "R2", "R3"], None) is None

    def test_exceeding_limit_returns_message_with_counts_and_designator(self):
        message = pad_align_abort_message(["R1"], 0)

        assert message is not None
        assert "失敗 1" in message
        assert "許容 0" in message
        assert "R1" in message

    def test_message_lists_every_failed_designator(self):
        message = pad_align_abort_message(["R1", "C3", "U2"], 2)

        assert message is not None
        assert "失敗 3" in message
        assert "許容 2" in message
        assert "R1" in message
        assert "C3" in message
        assert "U2" in message


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
