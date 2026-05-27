"""interactive_loading のテスト."""

import logging

import pytest
from pytest_mock import MockerFixture

from pcbasm.pasting.loading import interactive_loading


@pytest.fixture
def mock_applicator(mocker: MockerFixture):
    """PasteApplicator のモック."""
    applicator = mocker.Mock()
    applicator.load = mocker.Mock()
    return applicator


class TestInteractiveLoading:
    def test_default_amount_on_empty_input(
        self, mock_applicator, mocker: MockerFixture, caplog
    ):
        """Enter入力でデフォルト量をロードする."""
        mocker.patch("builtins.input", side_effect=["", "q"])
        with caplog.at_level(logging.INFO):
            interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_called_once_with(2.0)
        assert "2.0 μL" in caplog.text
        assert "完了" in caplog.text

    def test_custom_amount(self, mock_applicator, mocker: MockerFixture, caplog):
        """数値入力で指定量をロードする."""
        mocker.patch("builtins.input", side_effect=["3.5", "q"])
        with caplog.at_level(logging.INFO):
            interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_called_once_with(3.5)
        assert "3.5 μL" in caplog.text

    @pytest.mark.parametrize("quit_cmd", ["q", "quit"])
    def test_quit_without_loading(
        self, mock_applicator, mocker: MockerFixture, quit_cmd: str
    ):
        """q/quitで即座に終了し、ロードしない."""
        mocker.patch("builtins.input", side_effect=[quit_cmd])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_not_called()

    def test_invalid_input_shows_warning(
        self, mock_applicator, mocker: MockerFixture, caplog
    ):
        """不正入力で警告メッセージを出し、ロードしない."""
        mocker.patch("builtins.input", side_effect=["abc", "q"])
        with caplog.at_level(logging.WARNING):
            interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_not_called()
        assert "不正な入力です" in caplog.text

    def test_multiple_operations(self, mock_applicator, mocker: MockerFixture):
        """複数回のロード操作を順に実行する."""
        mocker.patch("builtins.input", side_effect=["", "5.0", "", "q"])
        interactive_loading(mock_applicator, 2.0)

        assert mock_applicator.load.call_count == 3
        calls = mock_applicator.load.call_args_list
        assert calls[0].args == (2.0,)
        assert calls[1].args == (5.0,)
        assert calls[2].args == (2.0,)
