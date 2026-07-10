"""`webui.jobs.board_ops.pad_align_abort_message` の仕様テスト.

paste-align-max-failures 計画書「公開 IF」節が契約:

- max_failures is None → None（無制限。board_tour が使用）
- 失敗数 <= 許容数 → None（境界: 失敗数 == 許容数は許容）
- 超過 → 失敗数・許容数・全 designator を含む日本語メッセージ文字列
  （メッセージは部分一致で検証する。完全一致は禁止）
"""

import pytest

from webui.jobs.board_ops import pad_align_abort_message


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
