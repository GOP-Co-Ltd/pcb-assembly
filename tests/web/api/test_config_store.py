"""`web.api.config_store.ConfigStore` の仕様テスト.

計画書「`src/webui/config_store.py`」節が契約:

- machine.toml のホワイトリスト読み書き
- tomlkit によるコメント・構造保持（変更対象外の行は不変）
- 未知キー / 型不一致 → UnknownFieldError

計画書 memory/agents/implementation-planner/webui-camera-calib.md
「公開インターフェース案 4」+ 要確認事項 2（orchestrator 採用）が追記契約:

- camera.crop.width / camera.crop.height は 1 以上の int（0 / 負値は
  UnknownFieldError）。change 即自動保存 UI での事故防止

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- machine_name（ドットの無いトップレベル bare key）を書き込める。tomlkit が
  トップレベルへ挿入する挙動に依存するので、書込後に tomllib で再パースして
  トップレベルに残ることをピンする（テーブルへ吸い込まれたら気づけるように）
"""

import re
import tomllib
from pathlib import Path
from typing import cast

import pytest

from web.api.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    MachineSettingValue,
    UnknownFieldError,
)


class TestMachineSettings:
    """machine.toml のホワイトリスト読み書き."""

    def test_read_returns_values_with_declared_types(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.dispense_mode"] == "auto"
        assert values["paste_dispenser.line_direction"] is None
        assert values["paste_dispenser.auto_line_aspect_ratio"] == 1.618
        assert values["paste_dispenser.max_fill_speed"] == 0.8
        assert values["paste_dispenser.solder_paste_density"] == 3.78
        assert values["paste_dispenser.paste_height"] == "auto"
        assert values["paste_dispenser.lift_height"] == 2.0
        assert values["paste_dispenser.toolhead.x"] == -1.772
        assert values["probe.min_radius"] == 0.7
        assert values["probe.board_edge_margin"] == 2.5
        assert values["camera.device_id"] == 0
        assert values["camera.format"] == "YUYV"
        assert values["camera.crop.width"] == 600
        assert values["reference_point.target_diameter"] == 3.0

    def test_read_covers_every_whitelisted_key(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert set(values) == {spec.key for spec in MACHINE_FIELDS}

    def test_missing_keys_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.initial_purge_ul"] is None
        assert values["paste_dispenser.bead_width_factor"] is None
        assert values["paste_dispenser.boundary_margin"] is None
        assert values["paste_dispenser.auto_area_short_side_factor"] is None
        assert values["paste_dispenser.pad_align.refine_max_short_side"] is None
        assert values["probe.lift_height"] is None

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.max_fill_speed", 0.9),
            ("paste_dispenser.dispense_mode", "line"),
            ("paste_dispenser.line_direction", "outward"),
            ("paste_dispenser.solder_paste_density", 4.1),
            ("paste_dispenser.initial_purge_ul", 0.2),
            # 境界: 0 は「初回パージをしない」設定として受理する
            ("paste_dispenser.initial_purge_ul", 0.0),
            ("paste_dispenser.paste_height", 0.25),
            ("paste_dispenser.paste_height", "auto"),
            ("paste_dispenser.lift_height", 3.25),
            # machine.toml に無いキーは追記される
            ("paste_dispenser.bead_width_factor", 1.5),
            ("paste_dispenser.auto_area_short_side_factor", 4.0),
            ("probe.board_edge_margin", 3.0),
            ("reference_point.offsets.bottom_right", [-4.0, 4.0]),
            ("paste_dispenser.nozzle_cap.x", 10.123),
            ("paste_dispenser.nozzle_clean.x", 10.123),
            ("paste_dispenser.nozzle_clean.wipe_speed", 8.0),
            # 境界: 0 は「その工程を行わない」設定として受理する
            ("paste_dispenser.nozzle_clean.press_depth", 0.0),
            ("paste_dispenser.nozzle_clean.purge_ul", 0.0),
            ("paste_dispenser.nozzle_clean.stroke", 0.0),
            ("paste_dispenser.nozzle_clean.passes", 0),
            ("paste_dispenser.pad_align.region_size_px", 160),
            ("paste_dispenser.pad_align.region_overlap", 0.25),
        ],
    )
    def test_write_then_reread_reflects_value(
        self, store: ConfigStore, key: str, value: MachineSettingValue
    ):
        store.write_machine_settings({key: value})

        assert store.read_machine_settings()[key] == value

    def test_write_keeps_untouched_lines_byte_identical(
        self, store: ConfigStore, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "max_fill_speed" in changed[0][0]

    def test_write_keeps_inline_comment_on_changed_line(
        self, store: ConfigStore, config_dir: Path
    ):
        store.write_machine_settings({"probe.min_radius": 2.5})

        path = config_dir / "machine.toml"
        line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("min_radius")
        )
        assert "2.5" in line
        assert "銅箔境界" in line

    def test_write_adds_probe_lift_height_missing_from_toml(
        self, store: ConfigStore, config_dir: Path
    ):
        store.write_machine_settings({"probe.lift_height": 1.25})

        values = store.read_machine_settings()
        assert values["probe.lift_height"] == 1.25
        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "lift_height = 1.25" in text

    def test_read_includes_camera_calibration_file(self, store: ConfigStore):
        """Phase 4: camera.calibration_file がホワイトリストに含まれ既存値が読める."""
        values = store.read_machine_settings()

        assert values["camera.calibration_file"] == "ov9281_test_fixture.json"

    def test_write_calibration_file_changes_only_target_line(
        self, store: ConfigStore, config_dir: Path
    ):
        """camera.calibration_file の書込は対象行のみ変更しコメント・構造を保つ."""
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings(
            {"camera.calibration_file": "ov9281_20260612.json"}
        )

        values = store.read_machine_settings()
        assert values["camera.calibration_file"] == "ov9281_20260612.json"
        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "calibration_file" in changed[0][0]

    def test_write_machine_name_stays_a_top_level_bare_key(
        self, store: ConfigStore, config_dir: Path
    ):
        """machine_name はテーブルに吸い込まれずトップレベルに残る（tomllib で再パース）.

        tomlkit がドットの無いキーを `[klipper]` などのテーブル内へ挿入すると
        `Machine.machine_name` から読めなくなるため、挙動を明示的にピンする。
        """
        path = config_dir / "machine.toml"

        store.write_machine_settings({"machine_name": "黒兎 2 号機"})

        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["machine_name"] == "黒兎 2 号機"
        assert store.read_machine_settings()["machine_name"] == "黒兎 2 号機"

    def test_write_machine_name_preserves_comments_and_other_lines(
        self, store: ConfigStore, config_dir: Path
    ):
        """行の追加は machine_name の 1 行だけ。既存のコメント・構造は不変."""
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings({"machine_name": "黒兎 2 号機"})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before) + 1
        added = [line for line in after if line not in before]
        assert added == ['machine_name = "黒兎 2 号機"']

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.no_such_key", 1.0),
            ("paste_dispenser.max_fill_speed", "fast"),
            # bool は int のサブクラスなので数値フィールドへの投入は拒否する
            ("paste_dispenser.max_fill_speed", True),
            ("machine_name", 2.0),
            ("camera.calibration_file", 1.0),
            ("paste_dispenser.dispense_mode", "spray"),
            ("paste_dispenser.line_direction", "sideways"),
            ("paste_dispenser.auto_line_aspect_ratio", 1.0),
            ("paste_dispenser.auto_area_short_side_factor", 0.0),
            ("paste_dispenser.auto_area_short_side_factor", -1.0),
            ("paste_dispenser.lift_height", 0.0),
            ("paste_dispenser.lift_height", -1.0),
            ("paste_dispenser.paste_height", 0.0),
            ("paste_dispenser.solder_paste_density", 0.0),
            ("probe.board_edge_margin", 0.0),
            ("probe.board_edge_margin", -1.0),
            ("paste_dispenser.initial_purge_ul", -0.01),
            ("paste_dispenser.nozzle_clean.press_depth", -0.01),
            ("paste_dispenser.nozzle_clean.purge_ul", -0.01),
            ("paste_dispenser.nozzle_clean.stroke", -0.01),
            ("paste_dispenser.nozzle_clean.wipe_speed", 0.0),
            ("paste_dispenser.nozzle_clean.passes", -1),
            # int フィールドへ整数でない float
            ("paste_dispenser.pad_align.blur_ksize", 5.5),
            ("audio.volume", float("nan")),
            ("audio.volume", -0.01),
            ("audio.volume", 1.01),
            ("audio.device", "  "),
        ],
    )
    def test_write_rejects_invalid_value(
        self, store: ConfigStore, key: str, value: object
    ):
        # 型不一致の拒否を検証するため、意図的に契約外の値を渡す
        ill_typed = cast("MachineSettingValue", value)
        with pytest.raises(UnknownFieldError, match=re.escape(key.rsplit(".", 1)[-1])):
            store.write_machine_settings({key: ill_typed})

    def test_missing_machine_toml_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ConfigStore(tmp_path / "no-such-config").read_machine_settings()


class TestReferencePointOffsets:
    """float_pair 型フィールド reference_point.offsets.* の読み書き."""

    def test_read_returns_all_four_corner_pairs(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["reference_point.offsets.top_left"] == [5.0, -5.0]
        assert values["reference_point.offsets.top_right"] == [-5.0, -5.0]
        assert values["reference_point.offsets.bottom_left"] == [5.0, 5.0]
        assert values["reference_point.offsets.bottom_right"] == [-5.0, 5.0]

    def test_write_pair_keeps_table_comment(self, store: ConfigStore, config_dir: Path):
        store.write_machine_settings({"reference_point.offsets.top_left": [6.0, -6.0]})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "top_left = [6.0, -6.0]" in text

    @pytest.mark.parametrize(
        "value",
        [[1.0], [1.0, 2.0, 3.0], ["a", 1.0], [True, 1.0], 1.0, "1,2"],
    )
    def test_invalid_pair_raises(self, store: ConfigStore, value: object):
        # 型不一致の拒否を検証するため、意図的に契約外の値を渡す
        ill_typed = cast("MachineSettingValue", value)
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                {"reference_point.offsets.top_left": ill_typed}
            )


class TestLegacyNozzleSectionMigration:
    """旧トップレベル [nozzle_cap] / [nozzle_clean] を書き込みのついでに移す.

    設定を書き換えずに読める（Machine が旧パスも読む）が、放っておくと新旧が二重に
    残る。書き込み時にファイルごと新パスへ移し、旧セクションを消す。
    """

    def _write_legacy(self, config_dir: Path, body: str) -> Path:
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(body)
        return path

    def test_write_moves_the_legacy_section_and_drops_it(
        self, store: ConfigStore, config_dir: Path
    ):
        self._write_legacy(config_dir, "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n")

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        values = store.read_machine_settings()
        assert values["paste_dispenser.nozzle_cap.x"] == 10.0
        assert values["paste_dispenser.nozzle_cap.z"] == 3.5
        assert "[nozzle_cap]" not in (config_dir / "machine.toml").read_text()

    def test_write_migrates_every_legacy_section(
        self, store: ConfigStore, config_dir: Path
    ):
        self._write_legacy(
            config_dir,
            "\n[nozzle_cap]\nx = 1.0\ny = 2.0\nz = 3.0\n"
            "\n[nozzle_clean]\nx = 4.0\ny = 5.0\nz = 6.0\npress_depth = 0.4\n",
        )

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        values = store.read_machine_settings()
        assert values["paste_dispenser.nozzle_cap.x"] == 1.0
        assert values["paste_dispenser.nozzle_clean.x"] == 4.0
        assert values["paste_dispenser.nozzle_clean.press_depth"] == 0.4
        text = (config_dir / "machine.toml").read_text()
        assert "[nozzle_cap]" not in text
        assert "[nozzle_clean]" not in text

    def test_already_migrated_value_is_kept(self, store: ConfigStore, config_dir: Path):
        """移行済みの値を旧セクションの残骸で上書きしない."""
        store.write_machine_settings(
            {
                "paste_dispenser.nozzle_cap.x": 10.0,
                "paste_dispenser.nozzle_cap.y": 20.0,
                "paste_dispenser.nozzle_cap.z": 3.5,
            }
        )
        self._write_legacy(config_dir, "\n[nozzle_cap]\nx = 1.0\ny = 2.0\nz = 3.0\n")

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        assert store.read_machine_settings()["paste_dispenser.nozzle_cap.x"] == 10.0
        assert "[nozzle_cap]" not in (config_dir / "machine.toml").read_text()

    def test_migration_is_idempotent(self, store: ConfigStore, config_dir: Path):
        """2 回目以降の書き込みで移行済みの値が動かない."""
        self._write_legacy(config_dir, "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n")

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})
        store.write_machine_settings({"paste_dispenser.max_fill_speed": 1.1})

        values = store.read_machine_settings()
        assert values["paste_dispenser.nozzle_cap.x"] == 10.0
        assert values["paste_dispenser.max_fill_speed"] == 1.1

    def test_out_of_order_paste_dispenser_keeps_the_legacy_section(
        self, store: ConfigStore, config_dir: Path
    ):
        """移行先へ入れられないときは旧セクションを消さない.

        [paste_dispenser] 群が他のテーブルで分断されていると tomlkit は Table ではなく proxy
        を返し、そこへは入れられない。消してから弾くと座標が無音で失われ、 キャップ駐機が効かなくなる。
        """
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.pad_align]\nmax_passes = 3\n"
                "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n"
            )

        store.write_machine_settings({"machine_name": "移行テスト"})

        text = path.read_text()
        assert "[nozzle_cap]" in text
        assert "x = 10.0" in text

    def test_migration_keeps_inline_comments_but_orphans_the_heading(
        self, store: ConfigStore, config_dir: Path
    ):
        """行内コメントはテーブルごと移り、見出しコメントは元の位置に残る.

        tomlkit ではセクション直上の独立コメントがテーブルとは別の要素なので、一緒には
        移せない。動作には影響しないが、移行後に手書きの注釈が孤立することを明示する。
        """
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n# 手書きの見出しコメント\n[nozzle_cap]\nx = 10.0 # 実測\ny = 20.0\nz = 3.5\n"
            )

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        text = path.read_text()
        assert "x = 10.0 # 実測" in text
        assert "# 手書きの見出しコメント" in text

    def test_migration_creates_paste_dispenser_when_absent(
        self, store: ConfigStore, tmp_path: Path
    ):
        """[paste_dispenser] が無いファイルでも座標を失わない."""
        config_dir = tmp_path / "minimal-config"
        config_dir.mkdir()
        path = config_dir / "machine.toml"
        path.write_text(
            'machine_type = "paste"\n\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n'
        )

        ConfigStore(config_dir).write_machine_settings({"machine_name": "新規機体"})

        values = ConfigStore(config_dir).read_machine_settings()
        assert values["paste_dispenser.nozzle_cap.x"] == 10.0
        assert "[nozzle_cap]" not in path.read_text()

    def test_write_keeps_other_lines_when_there_is_nothing_to_migrate(
        self, store: ConfigStore, config_dir: Path
    ):
        """移行対象が無いときは余計な書き換えをしない."""
        path = config_dir / "machine.toml"
        before = path.read_text().splitlines()

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        after = path.read_text().splitlines()
        assert len(after) == len(before)


class TestNozzleCapFields:
    """Nozzle_cap.x/y/z フィールドの読み書き（nozzle-cap-parking 計画書「WebUI」節）.

    Repo fixture には [paste_dispenser.nozzle_cap] を入れない（未記録が既定状態）ため、欠落時は
    None。
    """

    def test_missing_nozzle_cap_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.nozzle_cap.x"] is None
        assert values["paste_dispenser.nozzle_cap.y"] is None
        assert values["paste_dispenser.nozzle_cap.z"] is None

    def test_missing_nozzle_clean_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.nozzle_clean.x"] is None
        assert values["paste_dispenser.nozzle_clean.press_depth"] is None


class TestPadAlignRegionSettings:
    """重複領域の寸法・overlap 設定を読み書きする."""

    def test_missing_region_settings_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.pad_align.region_size_px"] is None
        assert values["paste_dispenser.pad_align.region_overlap"] is None
        assert values["paste_dispenser.pad_align.refine_max_short_side"] is None

    @pytest.mark.parametrize("threshold", [0.0, 0.25])
    def test_write_refinement_threshold_then_reread_reflects_value(
        self, store: ConfigStore, threshold: float
    ):
        key = "paste_dispenser.pad_align.refine_max_short_side"

        store.write_machine_settings({key: threshold})

        assert store.read_machine_settings()[key] == pytest.approx(threshold)

    @pytest.mark.parametrize(
        "threshold",
        [True, -0.01, float("nan"), float("inf"), float("-inf")],
        ids=["bool", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_invalid_refinement_threshold_raises(
        self, store: ConfigStore, threshold: MachineSettingValue
    ):
        with pytest.raises(UnknownFieldError, match="refine_max_short_side"):
            store.write_machine_settings(
                {"paste_dispenser.pad_align.refine_max_short_side": threshold}
            )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.pad_align.region_size_px", 0),
            ("paste_dispenser.pad_align.region_overlap", -0.01),
            ("paste_dispenser.pad_align.region_overlap", 1.0),
        ],
    )
    def test_invalid_values_raise(self, store: ConfigStore, key: str, value):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({key: value})


class TestCopperPreprocessingSettings:
    """銅箔前処理の blur 設定を公開 ConfigStore 経由で検証する."""

    @pytest.mark.parametrize("blur_ksize", [0, -1, 2, 4])
    def test_rejects_blur_kernel_that_is_not_positive_and_odd(
        self, store: ConfigStore, blur_ksize: int
    ):
        with pytest.raises(UnknownFieldError, match="blur_ksize"):
            store.write_machine_settings(
                {"paste_dispenser.pad_align.blur_ksize": blur_ksize}
            )


class TestCameraCropFields:
    """Int 型フィールド camera.crop.width / camera.crop.height の 1 以上検証 （webui-
    camera-calib 計画書・要確認事項 2）.

    Change 即自動保存 UI では 0 や負値が machine.toml に書かれる事故が 起きやすいため、per-key
    検証を追加する。
    """

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_value_raises(self, store: ConfigStore, key: str, value: int):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({key: value})

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    def test_minimum_valid_value_is_accepted(self, store: ConfigStore, key: str):
        # 境界: 1 は有効な最小値
        store.write_machine_settings({key: 1})

        values = store.read_machine_settings()
        assert values[key] == 1


class TestAudioFields:
    """`[audio]` の読み書き（webui-audio-output 計画書「WebUI 配線」節）.

    Repo fixture には `[audio]` を入れない（未設定でも既定値で鳴るのが要件）ため、 欠落時は None、write
    後は round-trip する。
    """

    def test_missing_audio_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["audio.device"] is None
        assert values["audio.volume"] is None

    def test_write_then_reread_reflects_values(self, store: ConfigStore):
        store.write_machine_settings(
            {"audio.device": "  plughw:CARD=Audio,DEV=0  ", "audio.volume": 0.25}
        )

        values = store.read_machine_settings()
        assert values["audio.device"] == "plughw:CARD=Audio,DEV=0"
        assert values["audio.volume"] == 0.25

    @pytest.mark.parametrize("volume", [0.0, 1.0])
    def test_volume_boundaries_are_accepted(self, store: ConfigStore, volume: float):
        store.write_machine_settings({"audio.volume": volume})

        values = store.read_machine_settings()
        assert values["audio.volume"] == volume

    def test_creating_audio_table_keeps_existing_comments(
        self, store: ConfigStore, config_dir: Path
    ):
        """`[audio]` テーブルの新規作成でも既存コメントは失われない."""
        store.write_machine_settings({"audio.volume": 0.5})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "キャリブレーション値 2026/06/08" in text
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "volume = 0.5" in text


class TestFlowCalibrationFields:
    """運転時流量キャリブレーション設定の読み書き."""

    _PREFIX = "paste_dispenser.flow_calibration"

    def test_missing_settings_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values[f"{self._PREFIX}.calibration_file"] is None
        assert values[f"{self._PREFIX}.crop_size_mm"] is None

    def test_write_creates_the_nested_section(self, store: ConfigStore):
        store.write_machine_settings(
            {
                f"{self._PREFIX}.calibration_file": "cal.paste-volume.json",
                f"{self._PREFIX}.amount_ul": 0.25,
                f"{self._PREFIX}.crop_size_mm": 2.4,
            }
        )

        values = store.read_machine_settings()

        assert values[f"{self._PREFIX}.calibration_file"] == "cal.paste-volume.json"
        assert values[f"{self._PREFIX}.amount_ul"] == 0.25
        assert values[f"{self._PREFIX}.crop_size_mm"] == 2.4

    def test_an_empty_file_name_is_accepted_as_the_way_to_disable(
        self, store: ConfigStore
    ):
        store.write_machine_settings({f"{self._PREFIX}.calibration_file": ""})

        assert store.read_machine_settings()[f"{self._PREFIX}.calibration_file"] == ""

    @pytest.mark.parametrize("key", ["amount_ul", "crop_size_mm"])
    def test_rejects_non_positive_dimensions(self, store: ConfigStore, key: str):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({f"{self._PREFIX}.{key}": 0.0})

    def test_zero_settle_seconds_is_accepted_as_the_way_to_skip_the_wait(
        self, store: ConfigStore
    ):
        store.write_machine_settings({f"{self._PREFIX}.settle_seconds": 0.0})

        assert store.read_machine_settings()[f"{self._PREFIX}.settle_seconds"] == 0.0

    def test_rejects_a_negative_settle_time(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({f"{self._PREFIX}.settle_seconds": -1.0})
