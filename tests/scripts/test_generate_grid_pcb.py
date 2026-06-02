"""generate_grid_pcb スクリプトのテスト."""

import sys

import pytest


@pytest.fixture
def mock_pcbnew(monkeypatch, mocker):
    """pcbnewモジュール全体をモックする."""
    mock_module = mocker.MagicMock()

    # FromMM: 実際と同様にnm変換(整数)を返す
    mock_module.FromMM.side_effect = lambda x: int(x * 1_000_000)

    # VECTOR2I: タプル的に扱えるモック
    mock_module.VECTOR2I.side_effect = lambda x, y: (x, y)

    # 定数
    mock_module.SHAPE_T_SEGMENT = 0
    mock_module.Edge_Cuts = 44
    mock_module.F_Cu = 0
    mock_module.PAD_ATTRIB_SMD = 1
    mock_module.PAD_SHAPE_RECT = 1

    # BOARD
    mock_board = mocker.MagicMock()
    mock_module.BOARD.return_value = mock_board

    # PCB_SHAPE
    mock_module.PCB_SHAPE.side_effect = lambda board: mocker.MagicMock()

    # FOOTPRINT / PAD
    def make_footprint(board):
        fp = mocker.MagicMock()
        fp_pad = mocker.MagicMock()
        mock_module.PAD.side_effect = lambda f: fp_pad
        return fp

    mock_module.FOOTPRINT.side_effect = make_footprint

    monkeypatch.setitem(sys.modules, "pcbnew", mock_module)
    return mock_module


@pytest.fixture
def _import_generate(mock_pcbnew):
    """mock_pcbnew適用後にモジュールをインポートする."""
    # キャッシュされている場合はリロード
    if "scripts.dev.generate_grid_pcb" in sys.modules:
        del sys.modules["scripts.dev.generate_grid_pcb"]


class TestGenerateGridPcb:
    """generate_grid_pcb関数のテスト."""

    @pytest.mark.usefixtures("_import_generate")
    def test_default_2x2_grid(self, mock_pcbnew, tmp_path):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        board = mock_pcbnew.BOARD.return_value

        # 4 edge segments + 4 footprints = 8 Add calls
        assert board.Add.call_count == 8

        # SaveBoard called with correct path
        mock_pcbnew.SaveBoard.assert_called_once_with(str(output), board)

    @pytest.mark.usefixtures("_import_generate")
    def test_3x3_grid_creates_9_pads(self, mock_pcbnew, tmp_path):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=40, divisions=3, pad_size=0.5, output=output)

        board = mock_pcbnew.BOARD.return_value
        # 4 edges + 9 footprints = 13
        assert board.Add.call_count == 13

    @pytest.mark.usefixtures("_import_generate")
    def test_1x1_grid_creates_1_pad(self, mock_pcbnew, tmp_path):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=10, divisions=1, pad_size=0.5, output=output)

        board = mock_pcbnew.BOARD.return_value
        # 4 edges + 1 footprint = 5
        assert board.Add.call_count == 5

    @pytest.mark.usefixtures("_import_generate")
    def test_output_directory_created(self, mock_pcbnew, tmp_path):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        output = tmp_path / "sub" / "dir" / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        assert output.parent.exists()

    @pytest.mark.usefixtures("_import_generate")
    def test_footprint_references_are_sequential(self, mock_pcbnew, tmp_path, mocker):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        # FOOTPRINT呼び出しごとにSetReferenceの引数を記録
        references = []

        def track_footprint(board):
            fp = mocker.MagicMock()
            fp.SetReference.side_effect = lambda ref: references.append(ref)
            fp_pad = mocker.MagicMock()
            mock_pcbnew.PAD.side_effect = lambda f: fp_pad
            return fp

        mock_pcbnew.FOOTPRINT.side_effect = track_footprint

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        assert references == ["P1", "P2", "P3", "P4"]

    @pytest.mark.usefixtures("_import_generate")
    def test_print_summary(self, mock_pcbnew, tmp_path, capsys):
        from scripts.dev.generate_grid_pcb import generate_grid_pcb

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        captured = capsys.readouterr().out
        assert "30x30 mm" in captured
        assert "2x2 = 4 pads" in captured
        assert "step=10.0 mm" in captured
        assert "1.0x1.0 mm" in captured


class TestMainArgparse:
    """main関数のCLI引数パースのテスト."""

    @pytest.mark.usefixtures("_import_generate")
    def test_required_output_argument(self, mock_pcbnew, monkeypatch):
        from scripts.dev.generate_grid_pcb import main

        monkeypatch.setattr(
            sys, "argv", ["generate_grid_pcb", "-o", "/tmp/out.kicad_pcb"]
        )
        main()
        mock_pcbnew.SaveBoard.assert_called_once()

    @pytest.mark.usefixtures("_import_generate")
    def test_custom_arguments(self, mock_pcbnew, monkeypatch, tmp_path):
        from scripts.dev.generate_grid_pcb import main

        out = str(tmp_path / "custom.kicad_pcb")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "generate_grid_pcb",
                "-s",
                "50",
                "-n",
                "3",
                "--pad-size",
                "2.0",
                "-o",
                out,
            ],
        )
        main()

        board = mock_pcbnew.BOARD.return_value
        # 4 edges + 9 footprints = 13
        assert board.Add.call_count == 13
