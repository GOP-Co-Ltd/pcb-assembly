"""`webui.board_settings.BoardSettingsStore` の仕様テスト（unit）.

計画書 Phase 3「src/webui/board_settings.py」節が契約:

- board_id は source_pcb（相対 posix パス）から安定して導出され、別パスでは衝突しない
- load_or_init: 未存在 → machine.toml 由来の L0、存在 → 復元
- save → load の round-trip
- JSON に version / source_pcb / machine / settings が入る（ネスト方式の契約ピン）
- prune は orphan キーを除去して保存する

PcbFile / pcbnew には依存しない。``PasteSettingsModel`` / ``PadHierarchy`` は
直接構築する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely import Polygon

from pcbasm.config import PasteDispenser, Toolhead
from pcbasm.geometry import Point2d
from pcbasm.pasting import (
    LevelSetting,
    PasteOverride,
    PasteSettingsModel,
    settings_to_dict,
)
from pcbasm.pcb import Component, Layer, Pad, build_pad_hierarchy
from webui.board_settings import BoardSettingsStore


def _base_config() -> PasteDispenser:
    """テスト用の最小 PasteDispenser（7 項目 + 装置値）."""
    return PasteDispenser(
        rotations_per_ul=10.0,
        nozzle_diameter=0.4,
        fill_speed=0.8,
        max_dispense_rate=1.0,
        dispense_accel=1.0,
        retract_amount=0.1,
        retract_rate=1.0,
        retract_accel_factor=1.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=1.5,
        ul_per_mm2=0.05,
        prime_extra_delay=0.2,
        bead_width_factor=1.1,
        overlap=0.3,
        boundary_margin=0.05,
    )


def _square(cx: float, cy: float, size: float = 1.0) -> Polygon:
    half = size / 2
    return Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def _hierarchy():
    components = [Component("U1", "x", "0402", Point2d(0.0, 0.0), 0.0, Layer.TOP)]
    pads = [
        Pad("U1", "1", "n1", Layer.TOP, _square(0, 0)),
        Pad("U1", "2", "n2", Layer.TOP, _square(2, 0)),
    ]
    return build_pad_hierarchy(components, pads)


class TestBoardId:
    """board_id の安定性と衝突回避."""

    def test_stable_for_same_path(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        assert store.board_id("boards/a.kicad_pcb") == store.board_id(
            "boards/a.kicad_pcb"
        )

    def test_distinct_paths_differ(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        assert store.board_id("boards/a.kicad_pcb") != store.board_id(
            "boards/b.kicad_pcb"
        )

    def test_is_sixteen_hex_chars(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        board_id = store.board_id("boards/a.kicad_pcb")
        assert len(board_id) == 16
        int(board_id, 16)  # 16 進として解釈できる


class TestLoadOrInit:
    """load_or_init（未存在 → 初期化 / 存在 → 復元）."""

    def test_init_uses_machine_config_for_l0(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()

        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert model.base_enabled is True
        assert model.levels == {}
        assert model.base.fill_speed == config.fill_speed
        assert model.base.paste_height == config.paste_height
        assert model.base.boundary_margin == config.boundary_margin

    def test_init_does_not_write_file(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        store.load_or_init("kurousagi", "boards/a.kicad_pcb", _base_config())
        assert list(tmp_path.rglob("*.json")) == []

    def test_load_restores_saved_model(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=False,
            levels={("L2", "U1"): LevelSetting(enabled=True)},
        )
        store.save("kurousagi", "boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert loaded.base_enabled is False
        assert loaded.levels[("L2", "U1")].enabled is True


class TestRoundTrip:
    """Save → load の round-trip."""

    def test_override_values_survive(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=True,
            levels={
                ("L2", "U1"): LevelSetting(
                    enabled=False, override=PasteOverride(fill_speed=0.5, overlap=0.1)
                )
            },
        )
        store.save("kurousagi", "boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        setting = loaded.levels[("L2", "U1")]
        assert setting.enabled is False
        assert setting.override.fill_speed == 0.5
        assert setting.override.overlap == 0.1
        # 未設定項目は継承（None）のまま
        assert setting.override.paste_height is None


class TestJsonShape:
    """保存 JSON のネスト方式の契約ピン."""

    def test_doc_has_version_source_machine_settings(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        store.save("kurousagi", "boards/a.kicad_pcb", model)

        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))

        assert doc["version"] == 1
        assert doc["source_pcb"] == "boards/a.kicad_pcb"
        assert doc["machine"] == "kurousagi"
        assert doc["settings"] == settings_to_dict(model)

    def test_unknown_version_raises(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "settings": {}}), encoding="utf-8")

        with pytest.raises(ValueError):
            store.load_or_init("kurousagi", "boards/a.kicad_pcb", _base_config())


class TestPrune:
    """Prune（orphan キーの除去 + 保存）."""

    def test_orphan_keys_removed(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("kurousagi", "boards/a.kicad_pcb", config).base,
            base_enabled=True,
            levels={
                ("L2", "U1"): LevelSetting(enabled=False),  # 現階層に存在
                ("L2", "U99"): LevelSetting(enabled=False),  # orphan
            },
        )

        pruned = store.prune("kurousagi", "boards/a.kicad_pcb", model, hierarchy)

        assert ("L2", "U1") in pruned.levels
        assert ("L2", "U99") not in pruned.levels

    def test_prune_persists_result(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("kurousagi", "boards/a.kicad_pcb", config).base,
            base_enabled=True,
            levels={("L2", "U99"): LevelSetting(enabled=False)},
        )

        store.prune("kurousagi", "boards/a.kicad_pcb", model, hierarchy)
        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert ("L2", "U99") not in loaded.levels
