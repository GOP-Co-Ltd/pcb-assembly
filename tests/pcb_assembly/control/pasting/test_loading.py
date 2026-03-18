"""interactive_loading のテスト."""

import pytest
from pytest_mock import MockerFixture

from pcb_assembly.control.pasting.loading import interactive_loading


@pytest.fixture
def mock_applicator(mocker: MockerFixture):
    """PasteApplicator のモック."""
    applicator = mocker.Mock()
    applicator.load = mocker.Mock()
    return applicator


class TestInteractiveLoading:
    def test_default_amount_on_empty_input(
        self, mock_applicator, mocker: MockerFixture, capsys
    ):
        """Enter入力でデフォルト量をロードする."""
        mocker.patch("builtins.input", side_effect=["", "q"])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_called_once_with(2.0)
        captured = capsys.readouterr()
        assert "2.0 μL" in captured.out
        assert "完了" in captured.out

    def test_custom_amount(self, mock_applicator, mocker: MockerFixture, capsys):
        """数値入力で指定量をロードする."""
        mocker.patch("builtins.input", side_effect=["3.5", "q"])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_called_once_with(3.5)
        captured = capsys.readouterr()
        assert "3.5 μL" in captured.out

    def test_quit_immediately(self, mock_applicator, mocker: MockerFixture):
        """qで即座に終了する."""
        mocker.patch("builtins.input", side_effect=["q"])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_not_called()

    def test_quit_with_quit_command(self, mock_applicator, mocker: MockerFixture):
        """quitでも終了する."""
        mocker.patch("builtins.input", side_effect=["quit"])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_not_called()

    def test_invalid_input_shows_error(
        self, mock_applicator, mocker: MockerFixture, capsys
    ):
        """不正入力でエラーメッセージを表示し、ロードしない."""
        mocker.patch("builtins.input", side_effect=["abc", "q"])
        interactive_loading(mock_applicator, 2.0)

        mock_applicator.load.assert_not_called()
        captured = capsys.readouterr()
        assert "不正な入力です" in captured.out

    def test_multiple_operations(self, mock_applicator, mocker: MockerFixture):
        """複数回のロード操作を順に実行する."""
        mocker.patch("builtins.input", side_effect=["", "5.0", "", "q"])
        interactive_loading(mock_applicator, 2.0)

        assert mock_applicator.load.call_count == 3
        calls = mock_applicator.load.call_args_list
        assert calls[0].args == (2.0,)
        assert calls[1].args == (5.0,)
        assert calls[2].args == (2.0,)

    def test_header_shows_default_amount(
        self, mock_applicator, mocker: MockerFixture, capsys
    ):
        """ヘッダーにデフォルト量が表示される."""
        mocker.patch("builtins.input", side_effect=["q"])
        interactive_loading(mock_applicator, 1.5)

        captured = capsys.readouterr()
        assert "デフォルト量: 1.5 μL" in captured.out
