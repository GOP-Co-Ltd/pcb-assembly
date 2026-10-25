"""基板の同一性（pad 構成の署名と board_id）と基板設定 JSON v1 を固定する.

基板ごとの override は ``board_settings/<board_id>.json`` に残り、読むときに署名を照合する。
どちらかの算出が変わると、運用中の基板設定が黙って初期値に戻るので、ここで値そのものを固定する。
"""

import json
from pathlib import Path

import pytest

from pcbasm.config import load_machine
from pcbasm.kicad import load_board
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.overrides import BoardOverrides
from tests.support.paths import TEST_DATA_DIR

GOLDEN_DIR = Path(__file__).parent
BOARD_SETTINGS = GOLDEN_DIR / "board_settings_v1.json"


def _hierarchy(name: str) -> PadHierarchy:
    pcb = load_board(TEST_DATA_DIR / name / f"{name}.kicad_pcb")
    return PadHierarchy.build(pcb.components, pcb.pads)


class TestBoardIdentity:
    @pytest.mark.parametrize(
        ("name", "signature"),
        [
            (
                "led_blinker",
                "9c26117008626ef7e3f4de14dc20b4c07a359d269813bb6da077c3ace413e039",
            ),
            (
                "fill_coverage",
                "10a758809cdd923c6500087f1447811a83e561271f201aaed85e863a0b1e1b92",
            ),
        ],
    )
    def test_pad_hierarchy_signature(self, name: str, signature: str):
        assert _hierarchy(name).signature() == signature

    def test_board_id(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)

        assert store.board_id("real/led_blinker.kicad_pcb") == "d5b32394f1e1ba21"


class TestBoardSettingsV1:
    """Fixture 基板の階層キーで書いた v1 JSON が、読んで保存して読み直しても変わらない."""

    def test_round_trip(self, tmp_path: Path):
        doc = json.loads(BOARD_SETTINGS.read_text(encoding="utf-8"))
        hierarchy = _hierarchy("led_blinker")
        signature = hierarchy.signature()
        source_pcb = doc["source_pcb"]
        base = load_machine(GOLDEN_DIR / "machine.toml").paste_dispenser
        store = BoardOverrides(tmp_path)

        model = store.model_from_doc(
            doc, base, board_signature=signature, expected_source_pcb=source_pcb
        )
        store.save(source_pcb, model, board_signature=signature)

        assert model.find_orphans(hierarchy) == []
        assert store.export_doc(source_pcb, model, board_signature=signature) == doc
        assert store.load_or_init(source_pcb, base, board_signature=signature) == model
