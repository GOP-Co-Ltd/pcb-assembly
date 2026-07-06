"""`webui.board_settings.BoardSettingsStore` の仕様テスト（unit）.

計画書 Phase 3「src/webui/board_settings.py」節が契約:

- board_id は source_pcb（相対 posix パス）から安定して導出され、別パスでは衝突しない
- load_or_init: 未存在 → machine.toml 由来の defaults、存在 → 差分復元
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
)
from pcbasm.pcb import Component, Layer, Pad, build_pad_hierarchy
from webui.board_settings import BoardSettingsStore


def _base_config() -> PasteDispenser:
    """テスト用の最小 PasteDispenser（override 項目 + 装置値）."""
    return PasteDispenser(
        rotations_per_ul=10.0,
        nozzle_diameter=0.4,
        max_fill_speed=0.8,
        max_dispense_rate=1.0,
        dispense_accel=1.0,
        retract_amount=0.1,
        retract_rate=1.0,
        retract_accel_factor=1.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height="auto",
        ul_per_mm2=0.05,
        dispense_mode="auto",
        auto_line_aspect_ratio=1.618,
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


def _saved_doc(
    root: Path,
    store: BoardSettingsStore,
    source_pcb: str = "boards/a.kicad_pcb",
    machine: str = "kurousagi",
) -> dict:
    board_id = store.board_id(source_pcb)
    path = root / "board_settings" / machine / f"{board_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


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
        assert model.base.dispense_mode == config.dispense_mode
        assert model.base.prime_extra_delay == config.prime_extra_delay
        assert model.base.paste_height == config.paste_height
        assert model.base.boundary_margin == config.boundary_margin

    def test_init_does_not_write_file(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        store.load_or_init("kurousagi", "boards/a.kicad_pcb", _base_config())
        assert list(tmp_path.rglob("*.json")) == []

    def test_load_restores_saved_l0_and_level_overrides(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels={
                ("L0",): LevelSetting(enabled=False),
                ("L2", "U1"): LevelSetting(enabled=True),
            },
        )
        store.save("kurousagi", "boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert loaded.base_enabled is True
        assert loaded.levels[("L0",)].enabled is False
        assert loaded.levels[("L2", "U1")].enabled is True
        assert loaded.base.prime_extra_delay == config.prime_extra_delay


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
                    enabled=False,
                    override=PasteOverride(prime_extra_delay=0.5, overlap=0.1),
                )
            },
        )
        store.save("kurousagi", "boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        setting = loaded.levels[("L2", "U1")]
        assert setting.enabled is False
        assert setting.override.prime_extra_delay == 0.5
        assert setting.override.overlap == 0.1
        # 未設定項目は継承（None）のまま
        assert setting.override.paste_height is None


class TestInitialPurgePadId:
    """initial_purge_pad_id の基板単位保存."""

    def test_default_is_none_and_not_saved_when_unset(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert model.initial_purge_pad_id is None

        store.save("kurousagi", "boards/a.kicad_pcb", model)
        doc = _saved_doc(tmp_path, store)

        assert "initial_purge_pad_id" not in doc["settings"]

    def test_save_load_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=model.base_enabled,
            levels=model.levels,
            initial_purge_pad_id="U1.2",
        )

        store.save("kurousagi", "boards/a.kicad_pcb", edited)
        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        doc = _saved_doc(tmp_path, store)

        assert loaded.initial_purge_pad_id == "U1.2"
        assert doc["settings"]["initial_purge_pad_id"] == "U1.2"

    def test_export_import_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=model.base_enabled,
            levels=model.levels,
            initial_purge_pad_id="U1.1",
        )

        doc = store.export_doc("kurousagi", "boards/a.kicad_pcb", edited)
        restored = store.model_from_doc(
            doc,
            config,
            expected_machine="kurousagi",
            expected_source_pcb="boards/a.kicad_pcb",
        )

        assert doc["settings"]["initial_purge_pad_id"] == "U1.1"
        assert restored.initial_purge_pad_id == "U1.1"

    def test_prune_keeps_existing_initial_purge_pad_id(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=model.base_enabled,
            levels=model.levels,
            initial_purge_pad_id="U1.2",
        )

        pruned = store.prune("kurousagi", "boards/a.kicad_pcb", edited, hierarchy)
        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert pruned.initial_purge_pad_id == "U1.2"
        assert loaded.initial_purge_pad_id == "U1.2"

    def test_prune_clears_orphan_initial_purge_pad_id(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            base_enabled=model.base_enabled,
            levels=model.levels,
            initial_purge_pad_id="U99.1",
        )

        pruned = store.prune("kurousagi", "boards/a.kicad_pcb", edited, hierarchy)
        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        doc = _saved_doc(tmp_path, store)

        assert pruned.initial_purge_pad_id is None
        assert loaded.initial_purge_pad_id is None
        assert "initial_purge_pad_id" not in doc["settings"]


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
        assert doc["settings"] == {"levels": []}
        assert "base" not in doc["settings"]
        assert "base_enabled" not in doc["settings"]

    def test_doc_can_include_board_signature(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        signature = hierarchy.signature()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        store.save(
            "kurousagi",
            "boards/a.kicad_pcb",
            model,
            board_signature=signature,
        )

        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["board_signature"] == signature

    def test_unknown_version_raises(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "settings": {}}), encoding="utf-8")

        with pytest.raises(ValueError):
            store.load_or_init("kurousagi", "boards/a.kicad_pcb", _base_config())

    def test_legacy_root_is_read_only_fallback(self, tmp_path: Path):
        current = tmp_path / "current"
        legacy = tmp_path / "legacy" / "board_settings"
        store = BoardSettingsStore(current, legacy_root=legacy)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels={
                ("L0",): LevelSetting(enabled=False),
                ("L2", "U1"): LevelSetting(enabled=True),
            },
        )
        legacy_store = BoardSettingsStore(tmp_path / "legacy")
        legacy_store.save("kurousagi", "boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert loaded.levels[("L0",)].enabled is False
        assert list(current.rglob("*.json")) == []

    def test_legacy_base_equal_to_machine_config_is_not_l0_override(
        self, tmp_path: Path
    ):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_pcb": "boards/a.kicad_pcb",
                    "machine": "kurousagi",
                    "settings": {
                        "base": {
                            "paste_height": config.paste_height,
                            "ul_per_mm2": config.ul_per_mm2,
                            "prime_extra_delay": config.prime_extra_delay,
                            "bead_width_factor": config.bead_width_factor,
                            "overlap": config.overlap,
                            "boundary_margin": config.boundary_margin,
                        },
                        "base_enabled": True,
                        "levels": [],
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert ("L0",) not in loaded.levels

    def test_legacy_base_difference_becomes_l0_override(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        old_model = PasteSettingsModel(
            base=PasteOverride(
                prime_extra_delay=0.4,
                paste_height=config.paste_height,
                ul_per_mm2=config.ul_per_mm2,
                bead_width_factor=config.bead_width_factor,
                overlap=config.overlap,
                boundary_margin=config.boundary_margin,
            ),
            base_enabled=True,
        )
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / "kurousagi" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_pcb": "boards/a.kicad_pcb",
                    "machine": "kurousagi",
                    "settings": {
                        "base": {
                            "paste_height": old_model.base.paste_height,
                            "ul_per_mm2": old_model.base.ul_per_mm2,
                            "prime_extra_delay": old_model.base.prime_extra_delay,
                            "bead_width_factor": old_model.base.bead_width_factor,
                            "overlap": old_model.base.overlap,
                            "boundary_margin": old_model.base.boundary_margin,
                        },
                        "base_enabled": True,
                        "levels": [],
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)

        assert loaded.base.prime_extra_delay == config.prime_extra_delay
        assert loaded.levels[("L0",)].override.prime_extra_delay == 0.4

    def test_signature_mismatch_initializes_fresh_model(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels={("L2", "U1"): LevelSetting(enabled=True)},
        )
        store.save(
            "kurousagi",
            "boards/a.kicad_pcb",
            edited,
            board_signature="old-signature",
        )

        loaded = store.load_or_init(
            "kurousagi",
            "boards/a.kicad_pcb",
            config,
            board_signature="new-signature",
        )

        assert loaded.levels == {}

    def test_model_from_doc_rejects_mismatched_signature(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("kurousagi", "boards/a.kicad_pcb", config)
        doc = store.export_doc(
            "kurousagi",
            "boards/a.kicad_pcb",
            model,
            board_signature="old-signature",
        )

        with pytest.raises(ValueError):
            store.model_from_doc(
                doc,
                config,
                board_signature="new-signature",
                expected_machine="kurousagi",
                expected_source_pcb="boards/a.kicad_pcb",
            )


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
