"""吐出量キャリブレーションのメニューコマンド解釈。"""

from __future__ import annotations

import pytest

from web.api.jobs.pasting import parse_run_calib_command


class TestParseRunCalibCommand:
    """parse_run_calib_command（純粋関数）の契約.

    CALIBRATION_MENU_STAGE 文字列のテンプレ/JS 整合は routers/test_pages.py の data-
    loading-stage アサートと e2e の DOM アサートが担保する。
    """

    @pytest.mark.parametrize(
        "which",
        ["rotations_per_ul", "max_dispense_rate", "max_fill_speed", "all", "finish"],
    )
    def test_known_which_returns_which(self, which: str):
        assert parse_run_calib_command({"type": "run_calib", "which": which}) == which

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "run_calib"},  # which 欠落
            {"type": "run_calib", "which": "bogus"},  # 未知 which
            {"type": "run_calib", "which": 3},  # 非文字列
            {"type": "extrude", "amount": 1.0},  # 別 type
            {"type": "finish"},  # loading finish（run_calib ではない）
            {},  # キー無し
        ],
    )
    def test_invalid_command_yields_none(self, command: dict[str, object]):
        assert parse_run_calib_command(command) is None
