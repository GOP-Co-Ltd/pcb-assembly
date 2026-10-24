"""`pcbasm.pasting.overrides` の仕様テスト（保存 JSON の変換とストア）.

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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

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
    board_settings_export_filename,
    decode_board_settings,
    encode_board_settings,
)
from pcbasm.pasting.params import DispenseMotion, PasteParams, PasteParamsPatch
from pcbasm.pasting.settings import LevelSetting, PasteSettingsModel

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

        with pytest.raises(ValueError):
            store.load_or_init("boards/a.kicad_pcb", _base_config())

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

    @pytest.mark.parametrize(
        "value",
        [
            [1.0],
            [1.0, 2.0, 3.0],
            ["1.0", "2.0"],
            {"x": 1.0, "y": 2.0},
            [float("nan"), 0.0],
        ],
    )
    def test_malformed_point_decodes_to_none(self, value: object):
        # 壊れた保存内容でページを開けなくしない（pad id と同じく黙って捨てる）
        restored = _decode({"version": 1, "settings": {"initial_purge_point": value}})

        assert restored.initial_purge_point is None


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

    @pytest.mark.parametrize(
        "value",
        [
            [1.0],
            [1.0, 2.0, 3.0],
            ["1.0", "2.0"],
            {"x": 1.0, "y": 2.0},
            [float("nan"), 0.0],
        ],
    )
    def test_malformed_points_are_dropped(self, value: object):
        # 壊れた保存内容でページを開けなくしない（pad id と同じく黙って捨てる）
        restored = _decode(
            {"version": 1, "settings": {"flow_calibration_points": [value]}}
        )

        assert restored.flow_calibration_points == ()

    def test_a_malformed_entry_does_not_drop_the_sound_ones(self):
        restored = _decode(
            {
                "version": 1,
                "settings": {"flow_calibration_points": [[1.0], [9.5, 2.25]]},
            }
        )

        assert restored.flow_calibration_points == (Point2d(9.5, 2.25),)

    def test_a_non_sequence_decodes_to_an_empty_set(self):
        restored = _decode(
            {"version": 1, "settings": {"flow_calibration_points": "nope"}}
        )

        assert restored.flow_calibration_points == ()


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


class TestDecodeErrors:
    def test_unknown_version_is_an_error(self):
        decoded, error = decode_board_settings(
            {"version": 99, "settings": {"levels": []}}, base=_base()
        )

        assert decoded is None
        assert error is not None
        assert "schema version" in error

    def test_missing_settings_is_an_error(self):
        decoded, error = decode_board_settings({"version": 1}, base=_base())

        assert decoded is None
        assert error is not None

    def test_missing_levels_decodes_to_empty_model(self):
        restored = _decode({"version": 1, "settings": {}})

        assert restored.levels == ()
        assert restored.initial_purge_point is None
        assert restored.flow_calibration_points == ()


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
