"""`web.api.machine_config.MachineConfigFile` の仕様テスト.

- machine() は `config/` の machine.toml を呼ぶたびにディスクから読む（起動時の移行と degraded は test_app.py）
- 設定項目は `setting_fields()` の全項目で、machine.toml に書かれている値を読む
- 書き込みは tomlkit でコメント・構造を保ち、変えた行だけが変わる
- 未知キー / 型の合わない値 / 全体の検証に通らない値 → InvalidSettingError（何も書かない）
- 書き込んだら `on_change` で知らせる
"""

import re
import tomllib
from pathlib import Path
from typing import cast

import pytest

from pcbasm.config.fields import setting_fields
from web.api.machine_config import InvalidSettingError, MachineConfigFile
from web.contract.models import MachineSettingValue


class TestMachine:
    def test_loads_the_machine_toml_of_the_config_dir(
        self, machine_config: MachineConfigFile
    ):
        assert machine_config.machine().klipper.port == 7126


class TestSettingsWithFiles:
    def test_invalid_values_do_not_write_additional_files(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        original = machine_config.path.read_bytes()
        with pytest.raises(InvalidSettingError):
            machine_config.write(
                {"paste_dispenser.nozzle_diameter": 0},
                files={"unused-calibration.json": b"{}"},
            )
        assert machine_config.path.read_bytes() == original
        assert not (config_dir / "unused-calibration.json").exists()

    @pytest.mark.parametrize(
        "filename", ["../outside.json", "absolute", "machine.toml"]
    )
    def test_invalid_filename_rejects_the_entire_batch(
        self, machine_config: MachineConfigFile, config_dir: Path, filename: str
    ):
        if filename == "absolute":
            filename = str(config_dir.parent / "outside.json")
        original = machine_config.path.read_bytes()
        with pytest.raises(InvalidSettingError, match="ファイル名"):
            machine_config.write(
                {"camera.calibration_file": "must-not-be-saved.json"},
                files={"unused.json": b"{}", filename: b"{}"},
            )
        assert machine_config.path.read_bytes() == original
        assert not (config_dir / "unused.json").exists()

    def test_an_additional_file_replaces_a_symlink_without_writing_through_it(
        self, machine_config: MachineConfigFile, config_dir: Path, tmp_path: Path
    ):
        outside = tmp_path / "outside.json"
        outside.write_bytes(b"original")
        destination = config_dir / "calibration.json"
        destination.symlink_to(outside)

        machine_config.write(
            {"camera.calibration_file": destination.name},
            files={destination.name: b"new calibration"},
        )

        assert outside.read_bytes() == b"original"
        assert not destination.is_symlink()
        assert destination.read_bytes() == b"new calibration"


class TestMachineSettings:
    """machine.toml の設定項目の読み書き."""

    def test_read_returns_values_with_declared_types(
        self, machine_config: MachineConfigFile
    ):
        values = machine_config.values()

        assert values["paste_dispenser.defaults.dispense_mode"] == "auto"
        assert values["paste_dispenser.defaults.line_direction"] is None
        assert values["paste_dispenser.auto_line_aspect_ratio"] == 1.618
        assert values["paste_dispenser.motion.max_fill_speed"] == 0.8
        assert values["paste_dispenser.solder_paste_density"] == 3.78
        assert values["paste_dispenser.defaults.paste_height"] == "auto"
        assert values["paste_dispenser.motion.lift_height"] == 2.0
        assert values["paste_dispenser.toolhead.x"] == -1.772
        assert values["probe.min_radius"] == 0.7
        assert values["probe.board_edge_margin"] == 2.5
        assert values["camera.device_id"] == 0
        assert values["camera.format"] == "YUYV"
        assert values["camera.crop.width"] == 600
        assert values["reference_point.target_diameter"] == 3.0

    def test_read_covers_every_setting_field(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert set(values) == {field.key for field in setting_fields()}

    def test_missing_keys_read_as_none(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert values["paste_dispenser.initial_purge_ul"] is None
        assert values["paste_dispenser.defaults.bead_width_factor"] is None
        assert values["paste_dispenser.defaults.boundary_margin"] is None
        assert values["paste_dispenser.auto_area_short_side_factor"] is None
        assert values["alignment.refine_max_short_side"] is None
        assert values["probe.lift_height"] is None

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.motion.max_fill_speed", 0.9),
            ("paste_dispenser.defaults.dispense_mode", "line"),
            ("paste_dispenser.defaults.line_direction", "outward"),
            ("paste_dispenser.solder_paste_density", 4.1),
            ("paste_dispenser.initial_purge_ul", 0.2),
            # 境界: 0 は「初回パージをしない」設定として受理する
            ("paste_dispenser.initial_purge_ul", 0.0),
            ("paste_dispenser.defaults.paste_height", 0.25),
            ("paste_dispenser.defaults.paste_height", "auto"),
            ("paste_dispenser.motion.lift_height", 3.25),
            # machine.toml に無いキーは追記される
            ("paste_dispenser.defaults.bead_width_factor", 1.5),
            ("paste_dispenser.auto_area_short_side_factor", 4.0),
            ("probe.board_edge_margin", 3.0),
            ("reference_point.offsets.bottom_right", [-4.0, 4.0]),
            ("paste_dispenser.nozzle_cap.position", [10.123, 20.0, -3.5]),
            ("paste_dispenser.nozzle_clean.position", [10.123, 20.0, -3.5]),
            ("paste_dispenser.nozzle_clean.wipe_speed", 8.0),
            # 境界: 0 は「その工程を行わない」設定として受理する
            ("paste_dispenser.nozzle_clean.press_depth", 0.0),
            ("paste_dispenser.nozzle_clean.purge_ul", 0.0),
            ("paste_dispenser.nozzle_clean.stroke", 0.0),
            ("paste_dispenser.nozzle_clean.passes", 0),
            ("alignment.region_size_px", 160),
            ("alignment.region_overlap", 0.25),
        ],
    )
    def test_write_then_reread_reflects_value(
        self, machine_config: MachineConfigFile, key: str, value: MachineSettingValue
    ):
        machine_config.write({key: value})

        assert machine_config.values()[key] == value

    def test_write_keeps_untouched_lines_byte_identical(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        machine_config.write({"paste_dispenser.motion.max_fill_speed": 0.9})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "max_fill_speed" in changed[0][0]

    def test_write_keeps_inline_comment_on_changed_line(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        machine_config.write({"probe.min_radius": 2.5})

        path = config_dir / "machine.toml"
        line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("min_radius")
        )
        assert "2.5" in line
        assert "銅箔境界" in line

    def test_write_adds_probe_lift_height_missing_from_toml(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        machine_config.write({"probe.lift_height": 1.25})

        values = machine_config.values()
        assert values["probe.lift_height"] == 1.25
        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "lift_height = 1.25" in text

    def test_read_includes_camera_calibration_file(
        self, machine_config: MachineConfigFile
    ):
        """camera.calibration_file は設定項目で、既存値が読める."""
        values = machine_config.values()

        assert values["camera.calibration_file"] == "ov9281_test_fixture.json"

    def test_write_calibration_file_changes_only_target_line(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """camera.calibration_file の書込は対象行のみ変更しコメント・構造を保つ."""
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        machine_config.write({"camera.calibration_file": "ov9281_20260612.json"})

        values = machine_config.values()
        assert values["camera.calibration_file"] == "ov9281_20260612.json"
        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "calibration_file" in changed[0][0]

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.no_such_key", 1.0),
            ("paste_dispenser.motion.max_fill_speed", "fast"),
            # bool は int のサブクラスなので数値フィールドへの投入は拒否する
            ("paste_dispenser.motion.max_fill_speed", True),
            ("camera.calibration_file", 1.0),
            ("paste_dispenser.defaults.dispense_mode", "spray"),
            ("paste_dispenser.defaults.line_direction", "sideways"),
            ("paste_dispenser.auto_line_aspect_ratio", 1.0),
            ("paste_dispenser.auto_area_short_side_factor", 0.0),
            ("paste_dispenser.auto_area_short_side_factor", -1.0),
            ("paste_dispenser.motion.lift_height", 0.0),
            ("paste_dispenser.motion.lift_height", -1.0),
            ("paste_dispenser.defaults.paste_height", 0.0),
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
            ("alignment.blur_ksize", 5.5),
            ("audio.volume", float("nan")),
            ("audio.volume", -0.01),
            ("audio.volume", 1.01),
            ("audio.device", "  "),
        ],
    )
    def test_write_rejects_invalid_value(
        self, machine_config: MachineConfigFile, key: str, value: object
    ):
        # 型不一致の拒否を検証するため、意図的に契約外の値を渡す
        ill_typed = cast("MachineSettingValue", value)
        with pytest.raises(
            InvalidSettingError, match=re.escape(key.rsplit(".", 1)[-1])
        ):
            machine_config.write({key: ill_typed})

    def test_missing_machine_toml_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            MachineConfigFile(tmp_path / "no-such-config").values()


class TestFiniteMachineSettings:
    @pytest.mark.parametrize(
        "value",
        [float("inf"), float("-inf"), float("nan"), 10**400],
        ids=["inf", "-inf", "nan", "overflow-int"],
    )
    @pytest.mark.parametrize(
        ("key", "pair"),
        [
            ("reference_point.x", False),
            ("paste_dispenser.defaults.paste_height", False),
            ("reference_point.offsets.top_left", True),
        ],
    )
    def test_non_finite_values_leave_the_entire_file_untouched(
        self,
        machine_config: MachineConfigFile,
        config_dir: Path,
        key: str,
        pair: bool,
        value: float | int,
    ):
        path = config_dir / "machine.toml"
        before = path.read_bytes()
        assert key in machine_config.values()
        values: dict[str, MachineSettingValue] = {
            "camera.calibration_file": "保存しない.json",
            key: [1.0, value] if pair else value,
        }

        with pytest.raises(InvalidSettingError):
            machine_config.write(values)

        assert path.read_bytes() == before


class TestReferencePointOffsets:
    """float_pair 型フィールド reference_point.offsets.* の読み書き."""

    def test_read_returns_all_four_corner_pairs(
        self, machine_config: MachineConfigFile
    ):
        values = machine_config.values()

        assert values["reference_point.offsets.top_left"] == [5.0, -5.0]
        assert values["reference_point.offsets.top_right"] == [-5.0, -5.0]
        assert values["reference_point.offsets.bottom_left"] == [5.0, 5.0]
        assert values["reference_point.offsets.bottom_right"] == [-5.0, 5.0]

    def test_write_pair_keeps_table_comment(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        machine_config.write({"reference_point.offsets.top_left": [6.0, -6.0]})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "top_left = [6.0, -6.0]" in text

    @pytest.mark.parametrize(
        "value",
        [[1.0], [1.0, 2.0, 3.0], ["a", 1.0], [True, 1.0], 1.0, "1,2"],
    )
    def test_invalid_pair_raises(
        self, machine_config: MachineConfigFile, value: object
    ):
        # 型不一致の拒否を検証するため、意図的に契約外の値を渡す
        ill_typed = cast("MachineSettingValue", value)
        with pytest.raises(InvalidSettingError):
            machine_config.write({"reference_point.offsets.top_left": ill_typed})


class TestNozzlePositionFields:
    """ノズル位置の座標は x / y / z の 3 つを 1 項目（position）として読み書きする.

    Repo fixture には座標を入れない（未教示が既定状態）ため、欠落時は None。
    """

    def test_missing_positions_read_as_none(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert values["paste_dispenser.nozzle_cap.position"] is None
        assert values["paste_dispenser.nozzle_clean.position"] is None
        assert values["paste_dispenser.nozzle_clean.press_depth"] is None

    def test_position_is_written_as_three_toml_keys(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """TOML の形（x / y / z）は変えない."""
        machine_config.write({"paste_dispenser.nozzle_cap.position": [10.0, 20.0, 3.5]})

        document = tomllib.loads((config_dir / "machine.toml").read_text())
        cap = document["paste_dispenser"]["nozzle_cap"]
        assert cap == {"x": 10.0, "y": 20.0, "z": 3.5}

    @pytest.mark.parametrize(
        "value", [[10.0, 20.0], [10.0, 20.0, 3.5, 1.0]], ids=["two", "four"]
    )
    def test_rejects_anything_but_three_coordinates(
        self, machine_config: MachineConfigFile, config_dir: Path, value: list[float]
    ):
        path = config_dir / "machine.toml"
        original = path.read_bytes()

        with pytest.raises(InvalidSettingError):
            machine_config.write({"paste_dispenser.nozzle_cap.position": value})

        assert path.read_bytes() == original

    def test_rejects_a_single_coordinate(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """座標の一部だけを書く要求は拒否され、ファイルは変わらない（1 軸ずつ書くキーは無い）."""
        path = config_dir / "machine.toml"
        original = path.read_bytes()

        with pytest.raises(InvalidSettingError, match="未知のマシン設定キー"):
            machine_config.write({"paste_dispenser.nozzle_cap.x": 12.5})

        assert path.read_bytes() == original


class TestWholeDocumentValidation:
    """書き込みはディスクの最新に差分を当て、全体を load_machine と同じ検証にかける."""

    def test_rejects_a_write_that_makes_a_valid_config_unloadable(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """項目単体では通っても、全体で読めなくなる書き込みは拒否する（min_samples ≤ max_samples）."""
        path = config_dir / "machine.toml"
        original = path.read_bytes()

        with pytest.raises(InvalidSettingError, match="max_samples"):
            machine_config.write({"probe.min_samples": 16})

        assert path.read_bytes() == original

    def test_applies_the_change_onto_hand_edits_made_after_start(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """手編集（ssh）した値を、後から WebUI で別の項目を書いても上書きしない."""
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text().replace("max_fill_speed = 0.8", "max_fill_speed = 0.6")
        )

        machine_config.write({"paste_dispenser.motion.lift_height": 2.5})

        values = machine_config.values()
        assert values["paste_dispenser.motion.max_fill_speed"] == 0.6
        assert values["paste_dispenser.motion.lift_height"] == 2.5

    def test_fixes_one_error_at_a_time_while_degraded(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """設定エラーの間も、もとから誤りのある節にだけ誤りが残る書き込みは受け付ける.

        同じ節に誤りが 2 つあると、1 つ直すと理由の文言は変わる。節で比べるので直せる。
        """
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text()
            .replace("max_fill_speed = 0.8", "max_fill_speed = -1.0")
            .replace("lift_height = 2.0", "lift_height = 0.0")
        )

        machine_config.write({"paste_dispenser.motion.max_fill_speed": 0.8})

        assert machine_config.values()["paste_dispenser.motion.max_fill_speed"] == 0.8

    def test_rejects_breaking_another_section_while_degraded(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """設定エラーの間も、誤りの無かった節を新たに壊す書き込みは拒否する."""
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text().replace("lift_height = 2.0", "lift_height = 0.0")
        )
        broken = path.read_bytes()

        with pytest.raises(InvalidSettingError, match="max_samples"):
            machine_config.write({"probe.min_samples": 16})

        assert path.read_bytes() == broken

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.initial_purge_ul", -5.0),
            ("paste_dispenser.auto_line_aspect_ratio", 0.5),
            ("paste_dispenser.defaults.dispense_mode", "spray"),
            ("paste_dispenser.motion.lift_height", 0.0),
        ],
    )
    def test_rejects_an_invalid_value_in_the_broken_section_while_degraded(
        self,
        machine_config: MachineConfigFile,
        config_dir: Path,
        key: str,
        value: MachineSettingValue,
    ):
        """設定エラーの間も、範囲の外の値はキーを名指しして拒否する.

        壊れた節の中の誤りは全体の検証では隠れる（motion が読めないと親の節の検証まで進まない）ので、項目ごとに検証する。
        """
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text().replace("max_fill_speed = 0.8", "max_fill_speed = -1.0")
        )
        broken = path.read_bytes()

        with pytest.raises(InvalidSettingError, match=re.escape(key)):
            machine_config.write({key: value})

        assert path.read_bytes() == broken


class TestPadAlignRegionSettings:
    """重複領域の寸法・overlap 設定を読み書きする."""

    def test_missing_region_settings_read_as_none(
        self, machine_config: MachineConfigFile
    ):
        values = machine_config.values()

        assert values["alignment.region_size_px"] is None
        assert values["alignment.region_overlap"] is None
        assert values["alignment.refine_max_short_side"] is None

    @pytest.mark.parametrize("threshold", [0.0, 0.25])
    def test_write_refinement_threshold_then_reread_reflects_value(
        self, machine_config: MachineConfigFile, threshold: float
    ):
        key = "alignment.refine_max_short_side"

        machine_config.write({key: threshold})

        assert machine_config.values()[key] == pytest.approx(threshold)

    @pytest.mark.parametrize(
        "threshold",
        [True, -0.01, float("nan"), float("inf"), float("-inf")],
        ids=["bool", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_invalid_refinement_threshold_raises(
        self, machine_config: MachineConfigFile, threshold: MachineSettingValue
    ):
        with pytest.raises(InvalidSettingError, match="refine_max_short_side"):
            machine_config.write({"alignment.refine_max_short_side": threshold})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("alignment.region_size_px", 0),
            ("alignment.region_overlap", -0.01),
            ("alignment.region_overlap", 1.0),
        ],
    )
    def test_invalid_values_raise(
        self, machine_config: MachineConfigFile, key: str, value
    ):
        with pytest.raises(InvalidSettingError):
            machine_config.write({key: value})


class TestCopperPreprocessingSettings:
    """銅箔前処理の blur 設定を書き込みの検証で確かめる."""

    @pytest.mark.parametrize("blur_ksize", [0, -1, 2, 4])
    def test_rejects_blur_kernel_that_is_not_positive_and_odd(
        self, machine_config: MachineConfigFile, blur_ksize: int
    ):
        with pytest.raises(InvalidSettingError, match="blur_ksize"):
            machine_config.write({"alignment.blur_ksize": blur_ksize})


class TestCameraCropFields:
    """camera.crop.width / camera.crop.height は 1 以上の整数.

    入力したそばから保存する UI では 0 や負値が書かれやすいので、スキーマの検証で拒否する。
    """

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_value_raises(
        self, machine_config: MachineConfigFile, key: str, value: int
    ):
        with pytest.raises(InvalidSettingError):
            machine_config.write({key: value})

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    def test_minimum_valid_value_is_accepted(
        self, machine_config: MachineConfigFile, key: str
    ):
        # 境界: 1 は有効な最小値
        machine_config.write({key: 1})

        values = machine_config.values()
        assert values[key] == 1


class TestAudioFields:
    """`[audio]` の読み書き（webui-audio-output 計画書「WebUI 配線」節）.

    Repo fixture には `[audio]` を入れない（未設定でも既定値で鳴るのが要件）ため、 欠落時は None、write
    後は round-trip する。
    """

    def test_missing_audio_reads_as_none(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert values["audio.device"] is None
        assert values["audio.volume"] is None

    def test_write_then_reread_reflects_values(self, machine_config: MachineConfigFile):
        machine_config.write(
            {"audio.device": "  plughw:CARD=Audio,DEV=0  ", "audio.volume": 0.25}
        )

        values = machine_config.values()
        assert values["audio.device"] == "plughw:CARD=Audio,DEV=0"
        assert values["audio.volume"] == 0.25

    @pytest.mark.parametrize("volume", [0.0, 1.0])
    def test_volume_boundaries_are_accepted(
        self, machine_config: MachineConfigFile, volume: float
    ):
        machine_config.write({"audio.volume": volume})

        values = machine_config.values()
        assert values["audio.volume"] == volume

    def test_creating_audio_table_keeps_existing_comments(
        self, machine_config: MachineConfigFile, config_dir: Path
    ):
        """`[audio]` テーブルの新規作成でも既存コメントは失われない."""
        machine_config.write({"audio.volume": 0.5})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "キャリブレーション値 2026/06/08" in text
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "volume = 0.5" in text


class TestFlowCalibrationFields:
    """運転時流量キャリブレーション設定の読み書き."""

    _PREFIX = "paste_dispenser.volume_correction"

    def test_missing_settings_read_as_none(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert values[f"{self._PREFIX}.calibration_file"] is None
        assert values[f"{self._PREFIX}.crop_size_mm"] is None

    def test_write_creates_the_nested_section(self, machine_config: MachineConfigFile):
        machine_config.write(
            {
                f"{self._PREFIX}.calibration_file": "cal.paste-volume.json",
                f"{self._PREFIX}.amount_ul": 0.25,
                f"{self._PREFIX}.crop_size_mm": 2.4,
            }
        )

        values = machine_config.values()

        assert values[f"{self._PREFIX}.calibration_file"] == "cal.paste-volume.json"
        assert values[f"{self._PREFIX}.amount_ul"] == 0.25
        assert values[f"{self._PREFIX}.crop_size_mm"] == 2.4

    def test_an_empty_file_name_is_accepted_as_the_way_to_disable(
        self, machine_config: MachineConfigFile
    ):
        machine_config.write({f"{self._PREFIX}.calibration_file": ""})

        assert machine_config.values()[f"{self._PREFIX}.calibration_file"] == ""

    @pytest.mark.parametrize("key", ["amount_ul", "crop_size_mm"])
    def test_rejects_non_positive_dimensions(
        self, machine_config: MachineConfigFile, key: str
    ):
        with pytest.raises(InvalidSettingError):
            machine_config.write({f"{self._PREFIX}.{key}": 0.0})

    def test_zero_settle_seconds_is_accepted_as_the_way_to_skip_the_wait(
        self, machine_config: MachineConfigFile
    ):
        machine_config.write({f"{self._PREFIX}.settle_seconds": 0.0})

        assert machine_config.values()[f"{self._PREFIX}.settle_seconds"] == 0.0

    def test_rejects_a_negative_settle_time(self, machine_config: MachineConfigFile):
        with pytest.raises(InvalidSettingError):
            machine_config.write({f"{self._PREFIX}.settle_seconds": -1.0})


class TestSettleAndDetectionFields:
    """`[settle]` / `[detection]` の読み書き（どちらも省略可のセクション）."""

    def test_missing_sections_read_as_none(self, machine_config: MachineConfigFile):
        values = machine_config.values()

        assert values["settle.move_sec"] is None
        assert values["settle.probe_sec"] is None
        assert values["detection.sample_count"] is None
        assert values["detection.minimum_sample_count"] is None

    def test_write_then_reread_reflects_values(self, machine_config: MachineConfigFile):
        machine_config.write(
            {
                "settle.move_sec": 0.8,
                "settle.probe_sec": 0.2,
                "detection.sample_count": 20,
                "detection.minimum_sample_count": 8,
            }
        )

        values = machine_config.values()
        assert values["settle.move_sec"] == 0.8
        assert values["settle.probe_sec"] == 0.2
        assert values["detection.sample_count"] == 20
        assert values["detection.minimum_sample_count"] == 8

    def test_zero_settle_is_accepted(self, machine_config: MachineConfigFile):
        machine_config.write({"settle.move_sec": 0.0})

        assert machine_config.values()["settle.move_sec"] == 0.0

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("settle.move_sec", -0.1),
            ("detection.sample_count", 0),
            ("detection.minimum_sample_count", 0),
        ],
    )
    def test_invalid_values_are_rejected(
        self, machine_config: MachineConfigFile, key: str, value: float
    ):
        with pytest.raises(InvalidSettingError):
            machine_config.write({key: value})

    def test_minimum_above_sample_count_is_rejected(
        self, machine_config: MachineConfigFile
    ):
        """片方だけ編集しても、書き込み後の値で相互制約を見る."""
        machine_config.write({"detection.sample_count": 6})

        with pytest.raises(InvalidSettingError, match="minimum_sample_count"):
            machine_config.write({"detection.minimum_sample_count": 7})

    def test_raising_both_at_once_is_accepted(self, machine_config: MachineConfigFile):
        machine_config.write(
            {"detection.sample_count": 30, "detection.minimum_sample_count": 20}
        )

        values = machine_config.values()
        assert values["detection.minimum_sample_count"] == 20


class TestChangeNotification:
    """書き込みと設定履歴の復元は、画面へ知らせるために ``on_change`` を呼ぶ."""

    def test_write_and_restore_call_on_change(self, config_dir: Path):
        changes: list[None] = []
        machine_config = MachineConfigFile(
            config_dir, on_change=lambda: changes.append(None)
        )

        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        [entry] = machine_config.history_entries()
        machine_config.restore(entry.name)

        assert len(changes) == 2

    def test_rejected_write_does_not_call_on_change(self, config_dir: Path):
        changes: list[None] = []
        machine_config = MachineConfigFile(
            config_dir, on_change=lambda: changes.append(None)
        )

        with pytest.raises(InvalidSettingError):
            machine_config.write({"paste_dispenser.motion.lift_height": 0.0})

        assert changes == []
