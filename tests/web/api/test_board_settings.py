"""`web.api.board_settings.BoardSettingsStore` の仕様テスト（unit）.

計画書 Phase 3「src/webui/board_settings.py」節が契約:

- board_id は source_pcb（相対 posix パス）から安定して導出され、別パスでは衝突しない
- load_or_init: 未存在 → machine.toml 由来の defaults、存在 → 差分復元
- save → load の round-trip
- JSON に version / source_pcb / settings が入る（ネスト方式の契約ピン）
- prune は orphan キーを除去して保存する

MR1 追記（計画書 web-api-ui-split.md「MR1」節）:

- update はロック内で「再 load → mutate → atomic write」を行い、同時編集で
  先行の変更が失われない
- ロックは排他（先行 update の mutate が返るまで後続は mutate に入れない）
- prune / import は ``save`` による全量上書きで、この排他の対象外

PcbFile / pcbnew には依存しない。``PasteSettingsModel`` / ``PadHierarchy`` は
直接構築する。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from shapely import Polygon

from pcbasm.config import PasteDispenser, Toolhead
from pcbasm.geometry import Point2d
from pcbasm.pasting.params import PasteParamsPatch
from pcbasm.pasting.settings import LevelSetting, PasteSettingsModel
from pcbasm.pcb import Component, Layer, Pad, PadHierarchy
from web.api.board_settings import BoardSettingsStore

type Mutate = Callable[[PasteSettingsModel], PasteSettingsModel]


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
        retract_accel_factor=2.0,
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
    return PadHierarchy.build(components, pads)


def _level(model: PasteSettingsModel, key: tuple[str, ...]) -> LevelSetting:
    setting = model.level(key)
    assert setting is not None, key
    return setting


def _saved_doc(
    root: Path,
    store: BoardSettingsStore,
    source_pcb: str = "boards/a.kicad_pcb",
) -> dict:
    board_id = store.board_id(source_pcb)
    path = root / "board_settings" / f"{board_id}.json"
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

        model = store.load_or_init("boards/a.kicad_pcb", config)

        assert model.levels == ()
        assert model.base.dispense_mode == config.dispense_mode
        assert model.base.line_direction == config.line_direction
        assert model.base.prime_extra_delay == config.prime_extra_delay
        assert model.base.paste_height == config.paste_height
        assert model.base.boundary_margin == config.boundary_margin

    def test_init_does_not_write_file(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        store.load_or_init("boards/a.kicad_pcb", _base_config())
        assert list(tmp_path.rglob("*.json")) == []

    def test_load_restores_saved_l0_and_level_overrides(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=(
                LevelSetting(("L0",), enabled=False),
                LevelSetting(("L2", "U1"), enabled=True),
            ),
        )
        store.save("boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert _level(loaded, ("L0",)).enabled is False
        assert _level(loaded, ("L2", "U1")).enabled is True
        assert loaded.base.prime_extra_delay == config.prime_extra_delay


class TestRoundTrip:
    """Save → load の round-trip."""

    def test_override_values_survive(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=(
                LevelSetting(
                    ("L2", "U1"),
                    enabled=False,
                    patch=PasteParamsPatch(prime_extra_delay=0.5, overlap=0.1),
                ),
            ),
        )
        store.save("boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        setting = _level(loaded, ("L2", "U1"))
        assert setting.enabled is False
        assert setting.patch.prime_extra_delay == 0.5
        assert setting.patch.overlap == 0.1
        # 未設定項目は継承（None）のまま
        assert setting.patch.paste_height is None


class TestInitialPurgePadId:
    """initial_purge_pad_id の基板単位保存."""

    def test_default_is_none_and_not_saved_when_unset(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)

        assert model.initial_purge_pad_id is None

        store.save("boards/a.kicad_pcb", model)
        doc = _saved_doc(tmp_path, store)

        assert "initial_purge_pad_id" not in doc["settings"]

    def test_save_load_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=model.levels,
            initial_purge_pad_id="U1.2",
        )

        store.save("boards/a.kicad_pcb", edited)
        loaded = store.load_or_init("boards/a.kicad_pcb", config)
        doc = _saved_doc(tmp_path, store)

        assert loaded.initial_purge_pad_id == "U1.2"
        assert doc["settings"]["initial_purge_pad_id"] == "U1.2"

    def test_export_import_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=model.levels,
            initial_purge_pad_id="U1.1",
        )

        doc = store.export_doc("boards/a.kicad_pcb", edited)
        restored = store.model_from_doc(
            doc,
            config,
            expected_source_pcb="boards/a.kicad_pcb",
        )

        assert doc["settings"]["initial_purge_pad_id"] == "U1.1"
        assert restored.initial_purge_pad_id == "U1.1"

    def test_prune_keeps_existing_initial_purge_pad_id(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=model.levels,
            initial_purge_pad_id="U1.2",
        )

        pruned = store.prune("boards/a.kicad_pcb", edited, hierarchy)
        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert pruned.initial_purge_pad_id == "U1.2"
        assert loaded.initial_purge_pad_id == "U1.2"

    def test_prune_clears_orphan_initial_purge_pad_id(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=model.levels,
            initial_purge_pad_id="U99.1",
        )

        pruned = store.prune("boards/a.kicad_pcb", edited, hierarchy)
        loaded = store.load_or_init("boards/a.kicad_pcb", config)
        doc = _saved_doc(tmp_path, store)

        assert pruned.initial_purge_pad_id is None
        assert loaded.initial_purge_pad_id is None
        assert "initial_purge_pad_id" not in doc["settings"]


class TestJsonShape:
    """保存 JSON のネスト方式の契約ピン."""

    def test_doc_has_version_source_pcb_settings(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        store.save("boards/a.kicad_pcb", model)

        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))

        assert doc["version"] == 1
        assert doc["source_pcb"] == "boards/a.kicad_pcb"
        assert doc["settings"] == {"levels": []}
        assert "base" not in doc["settings"]
        assert "base_enabled" not in doc["settings"]

    def test_doc_can_include_board_signature(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        signature = hierarchy.signature()
        model = store.load_or_init("boards/a.kicad_pcb", config)

        store.save(
            "boards/a.kicad_pcb",
            model,
            board_signature=signature,
        )

        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["board_signature"] == signature

    def test_unknown_version_raises(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "settings": {}}), encoding="utf-8")

        with pytest.raises(ValueError):
            store.load_or_init("boards/a.kicad_pcb", _base_config())

    def test_legacy_root_is_read_only_fallback(self, tmp_path: Path):
        """legacy_root は読込専用の fallback（書き戻さない）.

        machine セグメント除去により、この fallback は実在しうる旧レイアウト
        ``data/board_settings/<machine>/<board_id>.json`` とは一致しない。
        ここでピンしているのは経路の read-only 性だけ。
        """
        current = tmp_path / "current"
        legacy = tmp_path / "legacy" / "board_settings"
        store = BoardSettingsStore(current, legacy_root=legacy)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=(
                LevelSetting(("L0",), enabled=False),
                LevelSetting(("L2", "U1"), enabled=True),
            ),
        )
        legacy_store = BoardSettingsStore(tmp_path / "legacy")
        legacy_store.save("boards/a.kicad_pcb", edited)

        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert _level(loaded, ("L0",)).enabled is False
        assert list(current.rglob("*.json")) == []

    def test_legacy_base_equal_to_machine_config_is_not_l0_override(
        self, tmp_path: Path
    ):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_pcb": "boards/a.kicad_pcb",
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

        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert loaded.level(("L0",)) is None

    def test_legacy_base_difference_becomes_l0_override(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        old_base = {
            "prime_extra_delay": 0.4,
            "paste_height": config.paste_height,
            "ul_per_mm2": config.ul_per_mm2,
            "bead_width_factor": config.bead_width_factor,
            "overlap": config.overlap,
            "boundary_margin": config.boundary_margin,
        }
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_pcb": "boards/a.kicad_pcb",
                    "settings": {
                        "base": old_base,
                        "base_enabled": True,
                        "levels": [],
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert loaded.base.prime_extra_delay == config.prime_extra_delay
        assert _level(loaded, ("L0",)).patch.prime_extra_delay == 0.4

    def test_signature_mismatch_initializes_fresh_model(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = PasteSettingsModel(
            base=model.base,
            levels=(LevelSetting(("L2", "U1"), enabled=True),),
        )
        store.save(
            "boards/a.kicad_pcb",
            edited,
            board_signature="old-signature",
        )

        loaded = store.load_or_init(
            "boards/a.kicad_pcb",
            config,
            board_signature="new-signature",
        )

        assert loaded.levels == ()

    def test_model_from_doc_rejects_mismatched_signature(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        doc = store.export_doc(
            "boards/a.kicad_pcb",
            model,
            board_signature="old-signature",
        )

        with pytest.raises(ValueError):
            store.model_from_doc(
                doc,
                config,
                board_signature="new-signature",
                expected_source_pcb="boards/a.kicad_pcb",
            )


class TestPrune:
    """Prune（orphan キーの除去 + 保存）."""

    def test_orphan_keys_removed(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("boards/a.kicad_pcb", config).base,
            levels=(
                LevelSetting(("L2", "U1"), enabled=False),  # 現階層に存在
                LevelSetting(("L2", "U99"), enabled=False),  # orphan
            ),
        )

        pruned = store.prune("boards/a.kicad_pcb", model, hierarchy)

        assert pruned.level(("L2", "U1")) is not None
        assert pruned.level(("L2", "U99")) is None

    def test_prune_persists_result(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("boards/a.kicad_pcb", config).base,
            levels=(LevelSetting(("L2", "U99"), enabled=False),),
        )

        store.prune("boards/a.kicad_pcb", model, hierarchy)
        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert loaded.level(("L2", "U99")) is None


class TestUpdate:
    """Update（ロック内で 再 load → mutate → atomic write）."""

    def test_returns_and_persists_mutated_model(self, tmp_path: Path):
        store = BoardSettingsStore(tmp_path)
        config = _base_config()

        result = store.update(
            "boards/a.kicad_pcb",
            config,
            mutate=lambda model: model.with_level_patch(
                ("L2", "U1"), enabled=False, enabled_sent=True
            ),
        )
        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert _level(result, ("L2", "U1")).enabled is False
        assert _level(loaded, ("L2", "U1")).enabled is False

    def test_reloads_before_mutate_so_external_write_is_not_lost(self, tmp_path: Path):
        """Mutate 前に再 load する証明。A の後にファイルを直接書き換えても B が拾う.

        ロック内かどうかまでは見ない（それは
        ``test_second_update_blocks_until_first_mutate_returns`` が担う）。
        """
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        store.update(
            "boards/a.kicad_pcb",
            config,
            mutate=lambda model: model.with_level_patch(
                ("L2", "U1"), enabled=False, enabled_sent=True
            ),
        )

        # store の外（別プロセス相当）で U2 の設定を足す
        path = (
            tmp_path / "board_settings" / f"{store.board_id('boards/a.kicad_pcb')}.json"
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["settings"]["levels"].append({"key": ["L2", "U2"], "enabled": False})
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        result = store.update(
            "boards/a.kicad_pcb",
            config,
            mutate=lambda model: model.with_level_patch(
                ("L2", "U3"), enabled=False, enabled_sent=True
            ),
        )

        assert result.level(("L2", "U2")) is not None
        assert result.level(("L2", "U3")) is not None

    def test_second_update_blocks_until_first_mutate_returns(self, tmp_path: Path):
        """``update`` が排他であることの決定的な証明.

        A の ``mutate`` を Event で止めたまま B を起動し、B が ``mutate`` に
        到達できないことを確認する。A を解放したら B が完走し、B の結果には
        A の変更が含まれる（ロック内で再 load している）。
        """
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        a_entered = threading.Event()
        a_may_finish = threading.Event()
        b_entered = threading.Event()
        errors: list[BaseException] = []
        results: dict[str, PasteSettingsModel] = {}

        def mutate_a(model: PasteSettingsModel) -> PasteSettingsModel:
            a_entered.set()
            assert a_may_finish.wait(timeout=10.0)
            return model.with_level_patch(
                ("L2", "U1"), enabled=False, enabled_sent=True
            )

        def mutate_b(model: PasteSettingsModel) -> PasteSettingsModel:
            b_entered.set()
            return model.with_level_patch(
                ("L2", "U2"), enabled=False, enabled_sent=True
            )

        def run(name: str, mutate: Mutate) -> None:
            try:
                results[name] = store.update(
                    "boards/a.kicad_pcb", config, mutate=mutate
                )
            except BaseException as exc:
                errors.append(exc)

        thread_a = threading.Thread(target=run, args=("a", mutate_a))
        thread_a.start()
        assert a_entered.wait(timeout=10.0)
        thread_b = threading.Thread(target=run, args=("b", mutate_b))
        thread_b.start()

        # A がロックを保持している間、B は mutate に入れない
        assert not b_entered.wait(timeout=0.5)

        a_may_finish.set()
        thread_a.join(timeout=10.0)
        thread_b.join(timeout=10.0)

        assert errors == []
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert b_entered.is_set()
        assert results["b"].level(("L2", "U1")) is not None
        assert results["b"].level(("L2", "U2")) is not None

    def test_concurrent_updates_of_distinct_nodes_both_survive(self, tmp_path: Path):
        """2 スレッドが別ノードを同時編集しても、片方の変更が消えない.

        U1 側は 1 回だけ編集し、U2 側は編集を反復する。直列化されていないと U2 の反復書き込みが U1
        の変更を含まないモデルで上書きしてしまう。

        これは実スレッドでの通し確認（smoke）であって、検出は**確率的**（実測: ロックを
        no-op にすると 13/20 で失敗）。``_update_lock`` の排他そのものを決定的に守るのは
        :meth:`test_second_update_blocks_until_first_mutate_returns` の方なので、
        本テストが緑であることを lost update が無い根拠にはしない。
        """
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def edit(node: str, times: int) -> None:
            try:
                barrier.wait(timeout=10.0)
                for _ in range(times):
                    store.update(
                        "boards/a.kicad_pcb",
                        config,
                        mutate=lambda model: model.with_level_patch(
                            ("L2", node), enabled=False, enabled_sent=True
                        ),
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=edit, args=("U1", 1)),
            threading.Thread(target=edit, args=("U2", 200)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)

        assert errors == []
        for thread in threads:
            assert not thread.is_alive()
        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert loaded.level(("L2", "U1")) is not None
        assert loaded.level(("L2", "U2")) is not None

    def test_signature_mismatch_discards_stale_file_content(self, tmp_path: Path):
        """再 load は load_or_init と同じ signature 判定に従う."""
        store = BoardSettingsStore(tmp_path)
        config = _base_config()
        store.update(
            "boards/a.kicad_pcb",
            config,
            board_signature="sig-old",
            mutate=lambda model: model.with_level_patch(
                ("L2", "U1"), enabled=False, enabled_sent=True
            ),
        )

        result = store.update(
            "boards/a.kicad_pcb",
            config,
            board_signature="sig-new",
            mutate=lambda model: model.with_initial_purge_pad_id("U1.1"),
        )

        assert result.levels == ()
        assert result.initial_purge_pad_id == "U1.1"
