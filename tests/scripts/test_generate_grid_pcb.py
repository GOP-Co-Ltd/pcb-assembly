"""generate_grid_pcb スクリプト（CLI ラッパ）のテスト.

Phase 3 で生成ロジックは `pcbasm.pcb.generate` へ昇格した（計画書
webui-phase3.md「pcbasm 昇格」節）。ロジック本体のテストは
tests/pcbasm/pcb/test_generate.py に移設済み。ここでは argparse →
pcbasm への委譲のみを検証する。
"""

import sys

import pytest

import pcbasm.pcb.generate as generate_module


@pytest.fixture
def mock_pcbnew(monkeypatch, mocker):
    """pcbasm.pcb.generate が参照する pcbnew をモジュールごとモックする."""
    mock_module = mocker.MagicMock()
    mock_module.FromMM.side_effect = lambda x: int(x * 1_000_000)
    mock_module.VECTOR2I.side_effect = lambda x, y: (x, y)
    mock_module.SHAPE_T_SEGMENT = 0
    mock_module.Edge_Cuts = 44
    mock_module.F_Cu = 0
    mock_module.PAD_ATTRIB_SMD = 1
    mock_module.PAD_SHAPE_RECT = 1

    mock_board = mocker.MagicMock()
    mock_module.BOARD.return_value = mock_board
    mock_module.PCB_SHAPE.side_effect = lambda board: mocker.MagicMock()

    def make_footprint(board):
        fp = mocker.MagicMock()
        fp_pad = mocker.MagicMock()
        mock_module.PAD.side_effect = lambda f: fp_pad
        return fp

    mock_module.FOOTPRINT.side_effect = make_footprint

    monkeypatch.setattr(generate_module, "pcbnew", mock_module)
    return mock_module


class TestMainArgparse:
    """main関数のCLI引数パースのテスト."""

    def test_required_output_argument(self, mock_pcbnew, monkeypatch, tmp_path):
        from scripts.dev.generate_grid_pcb import main

        out = str(tmp_path / "out.kicad_pcb")
        monkeypatch.setattr(sys, "argv", ["generate_grid_pcb", "-o", out])
        main()
        mock_pcbnew.SaveBoard.assert_called_once()

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
