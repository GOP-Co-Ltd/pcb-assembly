"""`pcbasm.pasting.overrides` の仕様テスト（階層 override の解決、保存 JSON の変換とストア）.

ストアの契約:

- board_id は source_pcb（相対 posix パス）から安定して導出され、別パスでは衝突しない
- load_or_init: 未存在 → machine.toml 由来の defaults、存在 → 差分復元
- save → load の round-trip
- JSON に version / source_pcb / settings が入る（ネスト方式の契約ピン）
- prune は orphan キーを除去して保存する
- update はロック内で「再 load → mutate → atomic write」を行い、同時編集で
  先行の変更が失われない
- ロックは排他（先行 update の mutate が返るまで後続は mutate に入れない）
- prune / import は ``save`` による全量上書きで、この排他の対象外

pcbnew には依存しない。``PasteSettingsModel`` / ``PadHierarchy`` は直接構築する。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

import attrs
import pytest
from shapely import Polygon

from pcbasm.board import Board, Component, Layer, Outline, Pad
from pcbasm.config.dispenser import PasteDispenserConfig, ToolheadConfig
from pcbasm.geometry import Point2d
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.overrides import (
    BOARD_SETTINGS_SCHEMA_VERSION,
    BoardOverrides,
    BoardSession,
    LevelSetting,
    PasteSettingsModel,
    StoredBoardSettingsError,
    board_settings_export_filename,
    decode_board_settings,
    descendant_override_summary,
    encode_board_settings,
    own_override_summary,
    resolve_node_settings,
    resolve_pad_settings,
)
from pcbasm.pasting.params import DispenseMotion, PasteParams, PasteParamsPatch

type Mutate = Callable[[PasteSettingsModel], PasteSettingsModel]


def _base_config() -> PasteDispenserConfig:
    """テスト用の最小 PasteDispenserConfig（override 項目 + 装置値）."""
    return PasteDispenserConfig(
        rotations_per_ul=10.0,
        nozzle_diameter=0.4,
        toolhead=ToolheadConfig(x=0.0, y=0.0),
        auto_line_aspect_ratio=1.618,
        defaults=PasteParams(
            dispense_mode="auto",
            ul_per_mm2=0.05,
            paste_height="auto",
            prime_extra_delay=0.2,
            bead_width_factor=1.1,
            overlap=0.3,
            boundary_margin=0.05,
        ),
        motion=DispenseMotion(
            max_fill_speed=0.8,
            max_dispense_rate=1.0,
            dispense_accel=1.0,
            retract_amount=0.1,
            retract_rate=1.0,
            retract_accel_factor=2.0,
        ),
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


def _outline(size: float = 40.0) -> Polygon:
    """基板外形（原点始まりの正方形）。prune の点検証に使う."""
    return Polygon([(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)])


def _hierarchy():
    components = [Component("U1", "x", "0402", Point2d(0.0, 0.0), 0.0, Layer.TOP)]
    pads = [
        Pad("U1", "1", "n1", Layer.TOP, _square(0, 0)),
        Pad("U1", "2", "n2", Layer.TOP, _square(2, 0)),
    ]
    return PadHierarchy.build(components, pads)


_INSIDE = Point2d(10.0, 12.0)
_OUTSIDE = Point2d(100.0, 12.0)


def _with_points(
    model: PasteSettingsModel, kind: str, points: list[Point2d]
) -> PasteSettingsModel:
    """initial_purge_point / flow_calibration_points を同じ形で書き込む."""
    if kind == "initial_purge_point":
        return model.with_initial_purge_point(points[0])
    return model.with_flow_calibration_points(points)


def _points(model: PasteSettingsModel, kind: str) -> tuple[Point2d, ...]:
    """書き込んだ点を kind によらず tuple で読み出す."""
    if kind == "initial_purge_point":
        point = model.initial_purge_point
        return () if point is None else (point,)
    return model.flow_calibration_points


def _level(model: PasteSettingsModel, key: tuple[str, ...]) -> LevelSetting:
    setting = model.level(key)
    assert setting is not None, key
    return setting


def _saved_doc(
    root: Path,
    store: BoardOverrides,
    source_pcb: str = "boards/a.kicad_pcb",
) -> dict:
    board_id = store.board_id(source_pcb)
    path = root / "board_settings" / f"{board_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _base() -> PasteParams:
    return PasteParams(
        dispense_mode="auto",
        line_direction="unconstrained",
        ul_per_mm2=0.1,
        paste_height=0.05,
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
        micro_line_z_start=0.07,
        micro_line_z_end=0.07,
    )


def _decode(doc: dict, base: PasteParams | None = None) -> PasteSettingsModel:
    decoded, error = decode_board_settings(doc, base=base or _base())
    assert error is None, error
    assert decoded is not None
    return decoded.model


class TestBoardId:
    """board_id の安定性と衝突回避."""

    def test_is_stable_for_a_path_and_differs_between_paths(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)

        assert store.board_id("boards/a.kicad_pcb") == store.board_id(
            "boards/a.kicad_pcb"
        )
        assert store.board_id("boards/a.kicad_pcb") != store.board_id(
            "boards/b.kicad_pcb"
        )


class TestLoadOrInit:
    """load_or_init（未存在 → 初期化 / 存在 → 復元）."""

    def test_init_uses_machine_config_for_l0(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        config = _base_config()

        model = store.load_or_init("boards/a.kicad_pcb", config)

        assert model.levels == ()
        assert model.base.dispense_mode == config.defaults.dispense_mode
        assert model.base.line_direction == config.defaults.line_direction
        assert model.base.prime_extra_delay == config.defaults.prime_extra_delay
        assert model.base.paste_height == config.defaults.paste_height
        assert model.base.boundary_margin == config.defaults.boundary_margin

    def test_init_does_not_write_file(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        store.load_or_init("boards/a.kicad_pcb", _base_config())
        assert list(tmp_path.rglob("*.json")) == []

    def test_load_restores_saved_l0_and_level_overrides(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
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
        assert loaded.base.prime_extra_delay == config.defaults.prime_extra_delay


class TestRoundTrip:
    """Save → load の round-trip."""

    def test_override_values_survive(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
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
        # 未設定項目は継承のまま
        assert setting.patch.to_dict() == {"prime_extra_delay": 0.5, "overlap": 0.1}


class TestInitialPurgePoint:
    """initial_purge_point の基板単位保存."""

    def test_default_is_none_and_not_saved_when_unset(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)

        assert model.initial_purge_point is None

        store.save("boards/a.kicad_pcb", model)
        doc = _saved_doc(tmp_path, store)

        assert "initial_purge_point" not in doc["settings"]

    def test_save_load_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = model.with_initial_purge_point(Point2d(10.0, 12.0))

        store.save("boards/a.kicad_pcb", edited)
        loaded = store.load_or_init("boards/a.kicad_pcb", config)
        doc = _saved_doc(tmp_path, store)

        assert loaded.initial_purge_point == Point2d(10.0, 12.0)
        assert doc["settings"]["initial_purge_point"] == [10.0, 12.0]

    def test_export_import_round_trip_when_explicit(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = model.with_initial_purge_point(Point2d(3.0, 4.0))

        doc = store.export_doc("boards/a.kicad_pcb", edited)
        restored = store.model_from_doc(
            doc,
            config,
            expected_source_pcb="boards/a.kicad_pcb",
        )

        assert doc["settings"]["initial_purge_point"] == [3.0, 4.0]
        assert restored.initial_purge_point == Point2d(3.0, 4.0)

    @pytest.mark.parametrize("kind", ["initial_purge_point", "flow_calibration_points"])
    def test_prune_keeps_points_inside_the_outline(self, tmp_path: Path, kind: str):
        store = BoardOverrides(tmp_path)
        model = store.load_or_init("boards/a.kicad_pcb", _base_config())
        edited = _with_points(model, kind, [_INSIDE])

        pruned = store.prune(
            "boards/a.kicad_pcb", edited, _hierarchy(), outline=_outline()
        )

        assert _points(pruned, kind) == (_INSIDE,)

    @pytest.mark.parametrize(
        ("kind", "written", "expected"),
        [
            # 基板が差し替わって点が外形外へ出ても、解決エラーでページを塞がない
            ("initial_purge_point", [_OUTSIDE], ()),
            ("flow_calibration_points", [_INSIDE, _OUTSIDE], (_INSIDE,)),
        ],
    )
    def test_prune_drops_points_outside_the_outline(
        self,
        tmp_path: Path,
        kind: str,
        written: list[Point2d],
        expected: tuple[Point2d, ...],
    ):
        store = BoardOverrides(tmp_path)
        config = _base_config()
        model = store.load_or_init("boards/a.kicad_pcb", config)
        edited = _with_points(model, kind, written)

        pruned = store.prune(
            "boards/a.kicad_pcb", edited, _hierarchy(), outline=_outline()
        )

        assert _points(pruned, kind) == expected
        loaded = store.load_or_init("boards/a.kicad_pcb", config)
        assert _points(loaded, kind) == expected


class TestJsonShape:
    """保存 JSON のネスト方式の契約ピン."""

    def test_doc_has_version_source_pcb_settings(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
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
        store = BoardOverrides(tmp_path)
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
        store = BoardOverrides(tmp_path)
        board_id = store.board_id("boards/a.kicad_pcb")
        path = tmp_path / "board_settings" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "settings": {}}), encoding="utf-8")

        with pytest.raises(StoredBoardSettingsError) as raised:
            store.load_or_init("boards/a.kicad_pcb", _base_config())
        assert str(raised.value) == (
            f"保存済みの基板の塗布設定を読めません: "
            f"未対応の基板の塗布設定です: version=99 ({path})"
        )

    def test_signature_mismatch_initializes_fresh_model(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
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
        store = BoardOverrides(tmp_path)
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
        store = BoardOverrides(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("boards/a.kicad_pcb", config).base,
            levels=(
                LevelSetting(("L2", "U1"), enabled=False),  # 現階層に存在
                LevelSetting(("L2", "U99"), enabled=False),  # orphan
            ),
        )

        pruned = store.prune("boards/a.kicad_pcb", model, hierarchy, outline=_outline())

        assert pruned.level(("L2", "U1")) is not None
        assert pruned.level(("L2", "U99")) is None

    def test_prune_persists_result(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
        config = _base_config()
        hierarchy = _hierarchy()
        model = PasteSettingsModel(
            base=store.load_or_init("boards/a.kicad_pcb", config).base,
            levels=(LevelSetting(("L2", "U99"), enabled=False),),
        )

        store.prune("boards/a.kicad_pcb", model, hierarchy, outline=_outline())
        loaded = store.load_or_init("boards/a.kicad_pcb", config)

        assert loaded.level(("L2", "U99")) is None


class TestUpdate:
    """Update（ロック内で 再 load → mutate → atomic write）."""

    def test_returns_and_persists_mutated_model(self, tmp_path: Path):
        store = BoardOverrides(tmp_path)
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
        store = BoardOverrides(tmp_path)
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
        doc["settings"]["levels"].append(
            {"key": ["L2", "U2"], "enabled": False, "override": {}}
        )
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
        store = BoardOverrides(tmp_path)
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

    def test_signature_mismatch_discards_stale_file_content(self, tmp_path: Path):
        """再 load は load_or_init と同じ signature 判定に従う."""
        store = BoardOverrides(tmp_path)
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
            mutate=lambda model: model.with_initial_purge_point(Point2d(6.0, 7.0)),
        )

        assert result.levels == ()
        assert result.initial_purge_point == Point2d(6.0, 7.0)


class TestBoardSession:
    """BoardSession.load は基板から階層と署名を組み、その署名で保存済みの設定を読む."""

    def _board(self, pad_x: float = 2.0) -> Board:
        return Board(
            outline=Outline(_outline()),
            components=(
                Component("U1", "x", "0402", Point2d(0.0, 0.0), 0.0, Layer.TOP),
            ),
            pads=(
                Pad("U1", "1", "n1", Layer.TOP, _square(0, 0)),
                Pad("U1", "2", "n2", Layer.TOP, _square(pad_x, 0)),
            ),
            copper=(),
        )

    def test_reads_settings_saved_for_the_same_board(self, tmp_path: Path):
        overrides = BoardOverrides(tmp_path)
        board = self._board()
        first = BoardSession.load(overrides, "a.kicad_pcb", board, _base_config())
        overrides.save(
            "a.kicad_pcb",
            first.model.with_pads_enabled([("L4", "U1", "2")], enabled=False),
            board_signature=first.board_signature,
        )

        loaded = BoardSession.load(overrides, "a.kicad_pcb", board, _base_config())

        assert loaded.board_signature == loaded.hierarchy.signature()
        assert loaded.model.level(("L4", "U1", "2")) == LevelSetting(
            ("L4", "U1", "2"), enabled=False
        )

    def test_changed_pads_start_from_defaults(self, tmp_path: Path):
        overrides = BoardOverrides(tmp_path)
        first = BoardSession.load(
            overrides, "a.kicad_pcb", self._board(), _base_config()
        )
        overrides.save(
            "a.kicad_pcb",
            first.model.with_pads_enabled([("L4", "U1", "2")], enabled=False),
            board_signature=first.board_signature,
        )

        moved = BoardSession.load(
            overrides, "a.kicad_pcb", self._board(pad_x=3.0), _base_config()
        )

        assert moved.model.levels == ()

    def test_initial_ignores_unreadable_saved_settings(self, tmp_path: Path):
        """読めない保存済みの設定があっても、初期値で組める（上書きして復旧するため）."""
        overrides = BoardOverrides(tmp_path)
        path = tmp_path / "board_settings" / f"{overrides.board_id('a.kicad_pcb')}.json"
        path.parent.mkdir()
        path.write_text('{"version": 999}', encoding="utf-8")
        board = self._board()
        with pytest.raises(StoredBoardSettingsError):
            BoardSession.load(overrides, "a.kicad_pcb", board, _base_config())

        session = BoardSession.initial("a.kicad_pcb", board, _base_config())

        assert session.model == PasteSettingsModel.from_config(_base_config())
        assert session.board_signature == session.hierarchy.signature()


class TestEncode:
    def test_doc_shape_omits_base_and_unset_purge_point(self):
        model = PasteSettingsModel(
            base=_base(),
            levels=(
                LevelSetting(("L1", "0402"), patch=PasteParamsPatch(ul_per_mm2=0.08)),
            ),
        )

        doc = encode_board_settings(model, source_pcb="boards/a.kicad_pcb")

        assert doc == {
            "version": BOARD_SETTINGS_SCHEMA_VERSION,
            "source_pcb": "boards/a.kicad_pcb",
            "settings": {
                "levels": [
                    {
                        "key": ["L1", "0402"],
                        "enabled": None,
                        "override": {"ul_per_mm2": 0.08},
                    }
                ]
            },
        }

    def test_doc_includes_signature_and_purge_point_when_set(self):
        model = PasteSettingsModel(base=_base(), initial_purge_point=Point2d(4.0, 5.0))

        doc = encode_board_settings(
            model, source_pcb="boards/a.kicad_pcb", board_signature="sig"
        )

        assert doc["board_signature"] == "sig"
        assert doc["settings"]["initial_purge_point"] == [4.0, 5.0]


class TestMicroLineOverride:
    def test_round_trip_restores_micro_line_override(self):
        patch = PasteParamsPatch(
            dispense_mode="micro_line",
            micro_line_z_start=0.09,
            micro_line_z_end="auto",
        )
        model = PasteSettingsModel(
            base=_base(), levels=(LevelSetting(("L1", "0402"), patch=patch),)
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.levels[0].patch == patch


class TestInitialPurgePointCodec:
    """パージ位置は ``settings.initial_purge_point`` に [x, y] で残す."""

    def test_round_trip_restores_the_point(self):
        model = PasteSettingsModel(
            base=_base(), initial_purge_point=Point2d(3.5, -1.25)
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.initial_purge_point == Point2d(3.5, -1.25)


class TestFlowCalibrationPoints:
    """流量キャリブレーションの測定位置は ``settings.flow_calibration_points`` に残す."""

    def test_doc_carries_the_points_in_order_when_set(self):
        model = PasteSettingsModel(
            base=_base(),
            flow_calibration_points=(Point2d(9.5, 2.25), Point2d(13.0, 2.25)),
        )

        doc = encode_board_settings(model, source_pcb="a")

        assert doc["settings"]["flow_calibration_points"] == [
            [9.5, 2.25],
            [13.0, 2.25],
        ]

    def test_round_trip_restores_the_points(self):
        model = PasteSettingsModel(
            base=_base(),
            flow_calibration_points=(Point2d(9.5, 2.25), Point2d(13.0, 2.25)),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.flow_calibration_points == (
            Point2d(9.5, 2.25),
            Point2d(13.0, 2.25),
        )

    def test_is_independent_of_the_initial_purge_point(self):
        model = PasteSettingsModel(
            base=_base(),
            initial_purge_point=Point2d(1.0, 2.0),
            flow_calibration_points=(Point2d(9.5, 2.25),),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.initial_purge_point == Point2d(1.0, 2.0)
        assert restored.flow_calibration_points == (Point2d(9.5, 2.25),)


class TestCodecRoundTrip:
    def test_levels_enum_and_auto_height_survive(self):
        model = PasteSettingsModel(
            base=_base(),
            initial_purge_point=Point2d(1.0, 2.0),
            levels=(
                LevelSetting(
                    ("L2", "U1"),
                    patch=PasteParamsPatch(
                        dispense_mode="area",
                        line_direction="inward",
                        paste_height="auto",
                    ),
                ),
                LevelSetting(("L4", "U1", "9"), enabled=True),
                LevelSetting(("L2", "R1"), enabled=False),
            ),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored == model

    def test_unset_patch_fields_stay_inherited(self):
        model = PasteSettingsModel(
            base=_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))
        patch = restored.levels[0].patch

        assert patch.to_dict() == {"ul_per_mm2": pytest.approx(0.5)}

    def test_decode_uses_current_base_not_saved_one(self):
        doc = encode_board_settings(PasteSettingsModel(base=_base()), source_pcb="a")
        new_base = PasteParams(**{**_base().to_dict(), "ul_per_mm2": 0.3})

        restored = _decode(doc, base=new_base)

        assert restored.base == new_base


def _encoded_sample() -> dict:
    model = PasteSettingsModel(
        base=_base(),
        initial_purge_point=Point2d(1.0, 2.0),
        flow_calibration_points=(Point2d(9.5, 2.25),),
        levels=(LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),),
    )
    return encode_board_settings(model, source_pcb="a")


class TestDecodeErrors:
    """保存 JSON は strict に読む（未知 key・欠落 key・型・値の不正を黙って捨てず、理由を返す）."""

    def test_unknown_version_is_an_error(self):
        decoded, error = decode_board_settings(
            {"version": 99, "settings": {"levels": []}}, base=_base()
        )

        assert decoded is None
        assert error == "未対応の基板の塗布設定です: version=99"

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            pytest.param(
                lambda doc: doc.pop("settings"),
                "必須の項目がありません: settings",
                id="missing-settings",
            ),
            pytest.param(
                lambda doc: doc.pop("source_pcb"),
                "必須の項目がありません: source_pcb",
                id="missing-source-pcb",
            ),
            pytest.param(
                lambda doc: doc["settings"].pop("levels"),
                "必須の項目がありません: levels",
                id="missing-levels",
            ),
            pytest.param(
                lambda doc: doc.update(extra=1),
                "未知のキーです: extra",
                id="unknown-top-key",
            ),
            pytest.param(
                lambda doc: doc["settings"]["levels"][0].update(extra=1),
                "未知のキーです: extra",
                id="unknown-level-key",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(initial_purge_point=[1.0]),
                "配列または未設定（null）が必要です: [1.0]",
                id="short-purge-point",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(
                    flow_calibration_points=[[1.0, 2.0, 3.0]]
                ),
                "2要素の配列が必要です: [1.0, 2.0, 3.0]",
                id="long-calibration-point",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(
                    flow_calibration_points=[[float("nan"), 0.0]]
                ),
                "有限な数値が必要です: nan",
                id="nan-calibration-point",
            ),
            pytest.param(
                lambda doc: doc["settings"]["levels"][0].update(enabled="yes"),
                "真偽値または未設定（null）が必要です: 'yes'",
                id="enabled-not-bool",
            ),
            pytest.param(
                lambda doc: doc["settings"]["levels"][0]["override"].update(bogus=1.0),
                "未知の設定項目です: bogus (key=['L2', 'U1'])",
                id="unknown-override-field",
            ),
            pytest.param(
                lambda doc: doc["settings"]["levels"][0]["override"].update(
                    ul_per_mm2=-1.0
                ),
                "ul_per_mm2は正の有限値である必要があります: -1.0 (key=['L2', 'U1'])",
                id="invalid-override-value",
            ),
        ],
    )
    def test_malformed_document_is_an_error(
        self, mutate: Callable[[dict], object], reason: str
    ):
        doc = _encoded_sample()
        mutate(doc)

        decoded, error = decode_board_settings(doc, base=_base())

        assert decoded is None
        assert error == f"基板の塗布設定 schema v1 が不正です: {reason}"

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            pytest.param(
                lambda doc: doc.update(machine="kurousagi"),
                "未知のキーです: machine",
                id="machine-until-2026-07-27",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(base={}, base_enabled=True),
                "未知のキーです: base, base_enabled",
                id="base-until-2026-09-03",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(initial_purge_pad_id="U1.1"),
                "未知のキーです: initial_purge_pad_id",
                id="purge-pad-id-until-2026-09-08",
            ),
            pytest.param(
                lambda doc: doc["settings"].update(flow_calibration_point=[1.0, 2.0]),
                "未知のキーです: flow_calibration_point",
                id="single-calibration-point-2026-09-11",
            ),
            pytest.param(
                lambda doc: doc["settings"]["levels"][0]["override"].update(
                    fill_speed=1.0
                ),
                "未知の設定項目です: fill_speed (key=['L2', 'U1'])",
                id="fill-speed-until-2026-06-24",
            ),
        ],
    )
    def test_keys_written_by_older_versions_are_rejected_by_name(
        self, mutate: Callable[[dict], object], reason: str
    ):
        """旧版が書き、旧版の decode が黙って捨てていた key も、名前を挙げて拒否する."""
        doc = _encoded_sample()
        mutate(doc)

        _, error = decode_board_settings(doc, base=_base())

        assert error == f"基板の塗布設定 schema v1 が不正です: {reason}"

    def test_integral_numbers_from_a_browser_roundtrip_are_read_as_floats(self):
        """書き出した JSON をブラウザで読み込むと ``1.0`` が ``1`` になる."""
        doc = {
            "version": 1,
            "source_pcb": "a",
            "settings": {
                "levels": [
                    {"key": ["L0"], "enabled": None, "override": {"overlap": 0}}
                ],
                "initial_purge_point": [3, 4],
            },
        }

        restored = _decode(doc)

        assert restored.initial_purge_point == Point2d(3.0, 4.0)
        assert restored.levels[0].patch.to_dict() == {"overlap": 0.0}
        assert type(restored.levels[0].patch.to_dict()["overlap"]) is float


class TestExportFilename:
    """書き出しファイル名 ``<基板名>-paste-overrides-<YYYYMMDDTHHMMSS>.json``."""

    def test_uses_board_stem_and_timestamp(self):
        name = board_settings_export_filename(
            "real/led_blinker.kicad_pcb", datetime(2026, 9, 11, 14, 30, 5)
        )

        assert name == "led_blinker-paste-overrides-20260911T143005.json"

    def test_omitted_timestamp_uses_now(self):
        before = datetime.now().strftime("%Y%m%d")

        name = board_settings_export_filename("board.kicad_pcb")

        assert name.startswith(f"board-paste-overrides-{before}")
        assert name.endswith(".json")

    @pytest.mark.parametrize(
        ("source_pcb", "expected_stem"),
        [
            ("my board.kicad_pcb", "my-board"),
            ("rev/2.0/panel:A.kicad_pcb", "panel-A"),
            ("日本語基板.kicad_pcb", "日本語基板"),
            ("", "board"),
        ],
    )
    def test_stem_is_folded_to_a_safe_name(self, source_pcb: str, expected_stem: str):
        name = board_settings_export_filename(
            source_pcb, datetime(2026, 9, 11, 14, 30, 5)
        )

        assert name == f"{expected_stem}-paste-overrides-20260911T143005.json"


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    hw, hh = w / 2.0, h / 2.0
    return Polygon(
        [
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        ]
    )


def _component(designator: str, package: str) -> Component:
    return Component(
        designator=designator,
        value="",
        package=package,
        position=Point2d(x=0.0, y=0.0),
        rotation=0.0,
        layer=Layer.TOP,
    )


def _pad(designator: str, pad_number: str, polygon: Polygon) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=Layer.TOP,
        polygon=polygon,
    )


def _full_base() -> PasteParams:
    """machine.toml 相当の L0 確定値."""
    return PasteParams(
        dispense_mode="auto",
        line_direction="unconstrained",
        paste_height=0.05,
        ul_per_mm2=0.1,
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
        micro_line_z_start=0.07,
        micro_line_z_end=0.07,
    )


def _two_component_hierarchy() -> (
    tuple[Sequence[Component], Sequence[Pad], PadHierarchy]
):
    components = [_component("R1", "0402"), _component("U1", "QFN-8")]
    pads = [
        _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
        _pad("R1", "2", _rect(1.0, 0.0, 0.5, 0.9)),
        _pad("U1", "1", _rect(10.0, 0.0, 0.3, 0.5)),
        _pad("U1", "9", _rect(11.0, 0.0, 4.2, 4.2)),
    ]
    return components, pads, PadHierarchy.build(components, pads)


def _duplicate_pad_number_hierarchy() -> (
    tuple[Sequence[Component], Sequence[Pad], PadHierarchy]
):
    components = [_component("U1", "LFCSP-24")]
    # 読込（load_board）が分割片に付ける ref
    pads = [
        attrs.evolve(_pad("U1", "", _rect(x, y, 0.93, 0.93)), ref=f"U1.#{index}")
        for index, (x, y) in enumerate(
            [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)], start=1
        )
    ]
    return components, pads, PadHierarchy.build(components, pads)


class TestResolvePadSettingsKeys:
    """resolve_pad_settings の戻りキーと確定値。"""

    def test_keys_are_pad_refs(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_pad_settings(hierarchy, model)

        assert set(resolved.keys()) == {"R1.1", "R1.2", "U1.1", "U1.9"}

    def test_duplicate_pad_numbers_are_distinct_resolved_keys(self):
        _, pads, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_pad_settings(hierarchy, model)

        assert list(resolved) == [pad.ref for pad in pads]
        assert list(resolved) == ["U1.#1", "U1.#2", "U1.#3", "U1.#4"]

    def test_all_values_resolve_to_base_when_no_levels(self):
        _, _, hierarchy = _two_component_hierarchy()
        base = _full_base()
        model = PasteSettingsModel(base=base)

        resolved = resolve_pad_settings(hierarchy, model)

        # ルートノードも同じ規則で base に解決される
        root = resolve_node_settings(hierarchy, model)[("L0",)]
        for paste in (root, *resolved.values()):
            assert paste.enabled is True
            assert paste.params.dispense_mode == base.dispense_mode
            assert paste.params.line_direction == base.line_direction
            assert paste.params.paste_height == pytest.approx(base.paste_height)
            assert paste.params.ul_per_mm2 == pytest.approx(base.ul_per_mm2)
            assert paste.params.prime_extra_delay == pytest.approx(
                base.prime_extra_delay
            )
            assert paste.params.bead_width_factor == pytest.approx(
                base.bead_width_factor
            )
            assert paste.params.overlap == pytest.approx(base.overlap)
            assert paste.params.boundary_margin == pytest.approx(base.boundary_margin)


class TestOverrideMerge:
    """Override の階層マージ（非 None 項目のみ上書き、未指定は継承）。"""

    def test_level_overrides_single_field_keeps_others_inherited(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        u1 = resolved["U1.1"]
        assert u1.params.ul_per_mm2 == pytest.approx(0.5)  # 上書き
        assert u1.params.prime_extra_delay == pytest.approx(0.8)  # 継承
        # 別部品 R1 は影響を受けない
        assert resolved["R1.1"].params.ul_per_mm2 == pytest.approx(0.1)

    def test_enum_and_auto_height_override_inherit_like_numeric_fields(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L2", "U1"),
                    patch=PasteParamsPatch(
                        dispense_mode="line",
                        line_direction="outward",
                        paste_height="auto",
                    ),
                ),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        u1 = resolved["U1.1"]
        assert u1.params.dispense_mode == "line"
        assert u1.params.line_direction == "outward"
        assert u1.params.paste_height == "auto"
        assert u1.params.ul_per_mm2 == pytest.approx(0.1)
        r1 = resolved["R1.1"]
        assert r1.params.dispense_mode == "auto"
        assert r1.params.line_direction == "unconstrained"
        assert r1.params.paste_height == pytest.approx(0.05)

    def test_l0_override_applies_to_all_pads_over_machine_default(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L0",), patch=PasteParamsPatch(prime_extra_delay=0.45)),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert {paste.params.prime_extra_delay for paste in resolved.values()} == {0.45}
        assert {paste.params.ul_per_mm2 for paste in resolved.values()} == {0.1}

    def test_l0_override_can_be_overridden_by_more_specific_level(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L0",), patch=PasteParamsPatch(prime_extra_delay=0.45)),
                LevelSetting(
                    ("L2", "U1"), patch=PasteParamsPatch(prime_extra_delay=0.9)
                ),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["R1.1"].params.prime_extra_delay == pytest.approx(0.45)
        assert resolved["U1.1"].params.prime_extra_delay == pytest.approx(0.9)

    def test_more_specific_level_wins_over_less_specific(self):
        # L4 が L2 を上書き、未指定 field は L2 から継承
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L2", "U1"),
                    patch=PasteParamsPatch(ul_per_mm2=0.5, prime_extra_delay=1.5),
                ),
                LevelSetting(("L4", "U1", "9"), patch=PasteParamsPatch(ul_per_mm2=0.9)),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        thermal = resolved["U1.9"]
        assert thermal.params.ul_per_mm2 == pytest.approx(0.9)  # L4 が勝つ
        assert thermal.params.prime_extra_delay == pytest.approx(1.5)  # L2 から継承
        assert thermal.params.paste_height == pytest.approx(0.05)  # base から継承
        # 同部品の別 pad は L4 の影響を受けず L2 が効く
        other = resolved["U1.1"]
        assert other.params.ul_per_mm2 == pytest.approx(0.5)
        assert other.params.prime_extra_delay == pytest.approx(1.5)

    def test_l1_package_override_applies_to_all_same_package(self):
        components = [_component("R1", "0402"), _component("R2", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R2", "1", _rect(5.0, 0.0, 0.5, 0.9)),
        ]
        hierarchy = PadHierarchy.build(components, pads)
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L1", "0402"), patch=PasteParamsPatch(bead_width_factor=0.7)
                ),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["R1.1"].params.bead_width_factor == pytest.approx(0.7)
        assert resolved["R2.1"].params.bead_width_factor == pytest.approx(0.7)
        # 同じ override は node 側でも L1 ノードとそのサブツリー全体へ伝搬する
        nodes = resolve_node_settings(hierarchy, model)
        assert nodes[("L1", "0402")].params.bead_width_factor == pytest.approx(0.7)
        assert nodes[("L4", "R1", "1")].params.bead_width_factor == pytest.approx(0.7)
        assert nodes[("L4", "R2", "1")].params.bead_width_factor == pytest.approx(0.7)


class TestEnabledResolution:
    """Enabled は最具体レベルの明示値が勝つ。"""

    def test_l2_disable_disables_all_pads_of_component(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(LevelSetting(("L2", "U1"), enabled=False),),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["U1.1"].enabled is False
        assert resolved["U1.9"].enabled is False
        assert resolved["R1.1"].enabled is True  # 他部品は無関係

    def test_l4_enable_revives_pad_disabled_at_l2(self):
        # L2=False で配下無効でも、同 pad の L4=True で有効に復活
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), enabled=False),
                LevelSetting(("L4", "U1", "9"), enabled=True),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["U1.9"].enabled is True  # L4 が勝つ
        assert resolved["U1.1"].enabled is False  # L4 指定なしは L2 のまま
        # node 側では無効化された L2 ノード自身も見える
        nodes = resolve_node_settings(hierarchy, model)
        assert nodes[("L2", "U1")].enabled is False
        assert nodes[("L4", "U1", "9")].enabled is True
        assert nodes[("L4", "U1", "1")].enabled is False

    def test_l0_disable_defaults_all_pads_disabled(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(), levels=(LevelSetting(("L0",), enabled=False),)
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert all(paste.enabled is False for paste in resolved.values())

    def test_l0_disable_individual_enable_revives(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L0",), enabled=False),
                LevelSetting(("L4", "U1", "9"), enabled=True),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["U1.9"].enabled is True
        assert resolved["U1.1"].enabled is False
        assert resolved["R1.1"].enabled is False

    def test_enabled_none_at_level_does_not_override(self):
        # LevelSetting.enabled=None（override のみ）は enabled を上書きしない
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L2", "U1"), enabled=None, patch=PasteParamsPatch(ul_per_mm2=0.5)
                ),
            ),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["U1.1"].enabled is True  # 既定の有効を維持

    def test_l4_disable_affects_only_one_duplicate_pad_fragment(self):
        _, _, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(LevelSetting(("L4", "U1", "#2"), enabled=False),),
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved["U1.#1"].enabled is True
        assert resolved["U1.#2"].enabled is False
        assert resolved["U1.#3"].enabled is True
        assert resolved["U1.#4"].enabled is True


class TestResolveNodeSettings:
    """resolve_node_settings は階層の全ノード（L0–L4）を pad と同一規則で解決する。

    UI の階層表が各ノード行に解決済み値を表示するための算出。``resolve_pad_settings``
    と同じ継承規則（最具体が勝つ・enabled は明示値が勝つ）を、ツリーのルートから
    自ノードまでのパスで適用する。
    """

    def test_returns_all_hierarchy_node_keys(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_node_settings(hierarchy, model)

        assert set(resolved.keys()) == hierarchy.all_keys()

    def test_root_enabled_follows_l0_disable(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(), levels=(LevelSetting(("L0",), enabled=False),)
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L0",)].enabled is False

    def test_node_without_override_inherits_ancestor_values(self):
        # L2:U1 に override を置くと、その配下の L3/L4 ノードは override 無しでも
        # L2 の値を継承する。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L2", "U1")].params.ul_per_mm2 == pytest.approx(0.5)
        assert resolved[("L4", "U1", "1")].params.ul_per_mm2 == pytest.approx(0.5)
        assert resolved[("L4", "U1", "9")].params.ul_per_mm2 == pytest.approx(0.5)
        # 継承していない field は base のまま
        assert resolved[("L4", "U1", "1")].params.prime_extra_delay == pytest.approx(
            0.8
        )

    def test_override_propagates_to_node_and_descendants_only(self):
        # L2:U1 の override は U1 サブツリーにのみ伝播し、兄弟 L2:R1 と
        # 祖先（L0/L1）には及ばない。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L2", "U1"), patch=PasteParamsPatch(prime_extra_delay=0.3)
                ),
            ),
        )

        resolved = resolve_node_settings(hierarchy, model)

        # 自ノードと子孫は上書き
        assert resolved[("L2", "U1")].params.prime_extra_delay == pytest.approx(0.3)
        assert resolved[("L4", "U1", "1")].params.prime_extra_delay == pytest.approx(
            0.3
        )
        assert resolved[("L4", "U1", "9")].params.prime_extra_delay == pytest.approx(
            0.3
        )
        # 兄弟部品 R1 は影響なし
        assert resolved[("L2", "R1")].params.prime_extra_delay == pytest.approx(0.8)
        assert resolved[("L4", "R1", "1")].params.prime_extra_delay == pytest.approx(
            0.8
        )
        # 祖先 L0 は影響なし
        assert resolved[("L0",)].params.prime_extra_delay == pytest.approx(0.8)

    def test_l1_package_override_reaches_same_package_nodes(self):
        # L1 ノードに置いた override は同 package のサブツリー全体へ伝播する。
        components = [_component("R1", "0402"), _component("R2", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R2", "1", _rect(5.0, 0.0, 0.5, 0.9)),
        ]
        hierarchy = PadHierarchy.build(components, pads)
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(
                    ("L1", "0402"), patch=PasteParamsPatch(bead_width_factor=0.7)
                ),
            ),
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L1", "0402")].params.bead_width_factor == pytest.approx(0.7)
        assert resolved[("L4", "R1", "1")].params.bead_width_factor == pytest.approx(
            0.7
        )
        assert resolved[("L4", "R2", "1")].params.bead_width_factor == pytest.approx(
            0.7
        )

    def test_l4_node_values_match_resolve_pad_settings(self):
        # parity: 各 L4 ノードキーの解決値が、その L4 配下 pad に対する
        # resolve_pad_settings の解決値と一致する（同一規則の担保）。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L1", "0402"), patch=PasteParamsPatch(overlap=0.3)),
                LevelSetting(
                    ("L2", "U1"),
                    enabled=False,
                    patch=PasteParamsPatch(ul_per_mm2=0.5, prime_extra_delay=1.5),
                ),
                LevelSetting(
                    ("L4", "U1", "9"),
                    enabled=True,
                    patch=PasteParamsPatch(ul_per_mm2=0.9),
                ),
            ),
        )

        node_resolved = resolve_node_settings(hierarchy, model)
        pad_resolved = resolve_pad_settings(hierarchy, model)

        for pad in hierarchy.iter_pads():
            l4_key = hierarchy.node_keys_for_pad(pad)[-1]
            assert node_resolved[l4_key] == pad_resolved[pad.ref]

    def test_l4_node_values_match_resolve_pad_settings_for_duplicate_pads(self):
        # parity（分割 pad）: 同一 pad_number の分割片も L4 key が個別に分かれ、
        # pad-level 解決と一致する。
        _, _, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(LevelSetting(("L4", "U1", "#2"), enabled=False),),
        )

        node_resolved = resolve_node_settings(hierarchy, model)
        pad_resolved = resolve_pad_settings(hierarchy, model)

        for pad in hierarchy.iter_pads():
            l4_key = hierarchy.node_keys_for_pad(pad)[-1]
            assert node_resolved[l4_key] == pad_resolved[pad.ref]


class TestFindOrphans:
    """PasteSettingsModel.find_orphans は現存階層に無い levels キーを返す。"""

    def test_returns_keys_absent_from_hierarchy(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), enabled=False),  # 現存
                LevelSetting(("L2", "Q9"), enabled=False),  # 階層に無い
                LevelSetting(("L4", "U1", "404"), enabled=True),  # pad 番号が無い
            ),
        )

        orphans = model.find_orphans(hierarchy)

        assert set(orphans) == {("L2", "Q9"), ("L4", "U1", "404")}

    def test_returns_empty_when_all_keys_exist(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L1", "0402"), enabled=True),
                LevelSetting(("L2", "U1"), enabled=False),
            ),
        )

        assert model.find_orphans(hierarchy) == []


class TestWithLevelPatch:
    """PasteSettingsModel.with_level_patch の upsert / clear / enabled 保持。"""

    def test_upsert_values_keeps_other_fields_inherited(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        patched = model.with_level_patch(("L2", "U1"), values={"ul_per_mm2": 0.5})

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved["U1.1"].params.ul_per_mm2 == pytest.approx(0.5)
        assert resolved["U1.1"].params.prime_extra_delay == pytest.approx(0.8)  # 継承

    def test_clear_restores_inheritance(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        patched = model.with_level_patch(("L2", "U1"), clear=["ul_per_mm2"])

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved["U1.1"].params.ul_per_mm2 == pytest.approx(0.1)  # base へ復帰

    def test_enabled_not_sent_keeps_existing(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(LevelSetting(("L2", "U1"), enabled=False),),
        )

        patched = model.with_level_patch(("L2", "U1"), values={"ul_per_mm2": 0.5})

        setting = patched.level(("L2", "U1"))
        assert setting is not None
        assert setting.enabled is False  # enabled は保持

    def test_enabled_sent_updates_enabled(self):
        model = PasteSettingsModel(base=_full_base())

        patched = model.with_level_patch(("L2", "U1"), enabled=False, enabled_sent=True)

        setting = patched.level(("L2", "U1"))
        assert setting is not None
        assert setting.enabled is False

    def test_empty_result_removes_level(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        patched = model.with_level_patch(("L2", "U1"), clear=["ul_per_mm2"])

        assert patched.level(("L2", "U1")) is None


class TestWithPadsEnabled:
    """with_pads_enabled は指定 L4 群の enabled を一括設定する。"""

    def test_disables_listed_pads_only(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())
        l4_keys, _ = hierarchy.l4_keys_for_pad_ids(["U1.9"])

        patched = model.with_pads_enabled(l4_keys, enabled=False)

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved["U1.9"].enabled is False
        assert resolved["U1.1"].enabled is True
        assert resolved["R1.1"].enabled is True


class TestOverrideSummaries:
    """own_override_summary / descendant_override_summary の集計."""

    def test_own_summary_counts_enabled_and_fields_in_ui_order(self):
        setting = LevelSetting(
            ("L2", "U1"),
            enabled=False,
            patch=PasteParamsPatch(paste_height=0.1, ul_per_mm2=0.5),
        )

        summary = own_override_summary(setting)

        assert summary.enabled is True
        assert summary.fields == ("ul_per_mm2", "paste_height")  # UI 列順
        assert summary.count == 3

    def test_own_summary_of_missing_setting_is_empty(self):
        summary = own_override_summary(None)

        assert (summary.enabled, summary.fields, summary.count) == (False, (), 0)

    def test_descendant_summary_excludes_self_and_counts_descendants(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
                LevelSetting(("L4", "U1", "9"), enabled=True),
                LevelSetting(
                    ("L4", "U1", "1"),
                    enabled=False,
                    patch=PasteParamsPatch(ul_per_mm2=0.9, overlap=0.1),
                ),
            ),
        )

        def find(node, key):
            if node.key == key:
                return node
            for child in node.children:
                found = find(child, key)
                if found is not None:
                    return found
            return None

        u1 = find(hierarchy.root, ("L2", "U1"))
        assert u1 is not None
        summary = descendant_override_summary(u1, model)

        assert summary.enabled_count == 2
        assert summary.field_counts["ul_per_mm2"] == 1
        assert summary.field_counts["overlap"] == 1
        assert summary.fields == ("ul_per_mm2", "overlap")
        assert summary.node_count == 2
        assert summary.count == 4

    def test_descendant_summary_is_empty_without_descendant_overrides(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels=(LevelSetting(("L0",), patch=PasteParamsPatch(overlap=0.2)),),
        )

        summary = descendant_override_summary(hierarchy.root, model)

        assert summary.count == 0
        assert summary.fields == ()
        assert all(count == 0 for count in summary.field_counts.values())
