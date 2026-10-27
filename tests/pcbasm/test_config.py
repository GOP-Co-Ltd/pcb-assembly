from operator import attrgetter
from pathlib import Path

import attrs
import pytest

from pcbasm.config import (
    ConfigError,
    MachineConfig,
    load_capabilities,
    load_klipper,
    load_machine,
)
from pcbasm.config.alignment import AlignmentConfig
from pcbasm.config.audio import AudioConfig
from pcbasm.config.camera import CameraConfig, CameraCropConfig
from pcbasm.config.detection import DetectionConfig
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.pnp import PnpConfig
from pcbasm.config.probe import ProbeConfig
from pcbasm.config.reference_point import CornerOffsetsConfig, ReferencePointConfig
from pcbasm.config.settle import SettleConfig
from pcbasm.config.solder import (
    AutoModeConfig,
    DispenserConfig,
    NozzleCleanConfig,
    NozzleConfig,
    NozzleOffsetConfig,
    SolderConfig,
    SolderMotionConfig,
    TactConfig,
    VolumeCorrectionConfig,
)
from pcbasm.geometry import Point3d
from pcbasm.pasting.params import DispenseMotion, PasteParams
from pcbasm.paths import PROJECT_ROOT
from tests.support.machine_toml import pnp_only
from tests.support.paths import MINIMAL_MACHINE_TOML, TEST_DATA_DIR


def _write_with(tmp_path: Path, extra: str = "", **replace: str) -> Path:
    """検証用 machine.toml の文字列を置き換え、末尾に ``extra`` を足して書く."""
    path = tmp_path / "machine.toml"
    source = (MINIMAL_MACHINE_TOML).read_text()
    for old, new in replace.items():
        assert old in source
        source = source.replace(old, new, 1)
    path.write_text(f"{source}\n{extra}", encoding="utf-8")
    return path


def _with_marker_tolerance(tmp_path: Path, tolerance: float) -> Path:
    """検証用 machine.toml の [reference_point] に diameter_tolerance_mm を足して書く."""
    target = "target_diameter = 3.0\n"
    return _write_with(
        tmp_path, **{target: f"{target}diameter_tolerance_mm = {tolerance}\n"}
    )


def _machine_with(tmp_path: Path, extra: str) -> MachineConfig:
    """検証用 machine.toml の末尾に ``extra`` を足した MachineConfig を作る."""
    return load_machine(_write_with(tmp_path, extra))


def _solder(**overrides) -> SolderConfig:
    """検証用 machine.toml の [solder] の項目を ``overrides`` で置き換える.

    項目は下位表の項目名で渡す（どの表の項目かは名前で決まる）。
    """
    solder = load_machine(MINIMAL_MACHINE_TOML).require_solder()
    tables: dict[str, dict] = {
        name: {} for name in ("dispenser", "nozzle", "motion", "defaults", "auto")
    }
    root = {}
    for key, value in overrides.items():
        owner = next(
            (
                name
                for name in tables
                if key in attrs.fields_dict(type(getattr(solder, name)))
            ),
            None,
        )
        (root if owner is None else tables[owner])[key] = value
    evolved = {
        name: attrs.evolve(getattr(solder, name), **values)
        for name, values in tables.items()
    }
    return attrs.evolve(solder, **root, **evolved)


class TestMachine:
    """Machineクラスのテスト."""

    def test_load_config(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.klipper == KlipperConfig(host="127.0.0.1", port=7126)
        assert machine.solder == SolderConfig(
            paste_density=3.78,
            dispenser=DispenserConfig(
                rotations_per_ul=1.0,
                max_dispense_rate=5.0,
                dispense_accel=10.0,
                retract_amount=10.0,
                retract_rate=50.0,
                retract_accel_factor=2.0,
            ),
            nozzle=NozzleConfig(
                diameter=0.21, offset=NozzleOffsetConfig(x=13.2, y=54.7)
            ),
            motion=SolderMotionConfig(max_fill_speed=2.0, lift_height=3.0),
            defaults=PasteParams(
                dispense_mode="auto", ul_per_mm2=0.2, paste_height="auto"
            ),
            auto=AutoModeConfig(line_aspect_ratio=1.618),
        )
        assert machine.require_solder().defaults.line_direction == "unconstrained"
        assert machine.camera == CameraConfig(
            device_id=0,
            width=640,
            height=480,
            fps=30.0,
            format="YUYV",
            crop=CameraCropConfig(width=400, height=400),
            calibration_file=TEST_DATA_DIR / "calibration.json",
        )
        assert machine.reference_point == ReferencePointConfig(
            x=23.1,
            y=8.3,
            target_diameter=3.0,
            offsets=CornerOffsetsConfig(
                top_left=(0.0, -5.0),
                top_right=(0.0, -5.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(5.0, 5.0),
            ),
        )

    def test_default_values(self, tmp_path):
        path = _write_with(tmp_path, **{"device_id = 0\n": "", 'format = "YUYV"\n': ""})

        machine = load_machine(path)

        assert machine.camera.device_id == 0
        assert machine.camera.format == "YUYV"

    def test_rejects_legacy_three_corner_offsets(self, tmp_path):
        path = _write_with(tmp_path, **{"bottom_right = [5.0, 5.0]\n": ""})

        with pytest.raises(ConfigError, match="reference_point.offsets"):
            load_machine(path)

    def test_reference_marker_tolerance_defaults_to_half_a_millimetre(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.reference_point.diameter_tolerance_mm == pytest.approx(0.5)

    def test_reads_reference_marker_tolerance(self, tmp_path):
        path = _with_marker_tolerance(tmp_path, 0.8)

        assert load_machine(path).reference_point.diameter_tolerance_mm == 0.8

    @pytest.mark.parametrize("tolerance", [0.0, -0.5])
    def test_rejects_non_positive_reference_marker_tolerance(self, tmp_path, tolerance):
        path = _with_marker_tolerance(tmp_path, tolerance)

        with pytest.raises(ConfigError, match="diameter_tolerance_mm"):
            load_machine(path)

    def test_alignment_defaults_when_section_absent(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.alignment == AlignmentConfig()
        assert machine.alignment.tolerance == pytest.approx(0.1)
        assert machine.alignment.region_size_px == 100
        assert machine.alignment.refine_max_short_side == pytest.approx(0.4)

    @pytest.mark.parametrize(
        ("removed_line", "attribute", "expected"),
        [
            ("paste_density = 3.78\n", "paste_density", 3.78),
            ("lift_height = 3.0\n", "motion.lift_height", 2.0),
            ("retract_rate = 50.0\n", "dispenser.retract_rate", 5.0),
            ("", "initial_purge_ul", 0.1),
            ("", "auto.area_short_side_factor", 3.0),
            ("", "defaults.bead_width_factor", 1.0),
        ],
    )
    def test_optional_keys_fall_back_to_defaults(
        self, tmp_path, removed_line: str, attribute: str, expected: float
    ):
        source = (MINIMAL_MACHINE_TOML).read_text()
        if removed_line:
            source = source.replace(removed_line, "")
        path = tmp_path / "machine.toml"
        path.write_text(source, encoding="utf-8")

        solder = load_machine(path).require_solder()

        assert attrgetter(attribute)(solder) == pytest.approx(expected)

    def test_alignment_section_overrides_defaults(self, tmp_path):
        source = (MINIMAL_MACHINE_TOML).read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source + "\n[alignment]\ntolerance = 0.05\n"
            "region_size_px = 160\n"
            "region_overlap = 0.25\n"
            "refine_max_short_side = 0.25\n"
            "blur_ksize = 3\n"
        )

        alignment = load_machine(path).alignment

        assert alignment.tolerance == pytest.approx(0.05)
        assert alignment.region_size_px == 160
        assert alignment.region_overlap == pytest.approx(0.25)
        assert alignment.refine_max_short_side == pytest.approx(0.25)
        assert alignment.canny_low == pytest.approx(100.0)  # 未指定はデフォルト
        assert alignment.blur_ksize == 3

    @pytest.mark.parametrize("amount", [0.25, 0.0], ids=["explicit", "zero-disables"])
    def test_initial_purge_ul_reads_explicit_value(self, tmp_path, amount: float):
        source = (MINIMAL_MACHINE_TOML).read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                "[solder]\n",
                f"[solder]\ninitial_purge_ul = {amount}\n",
                1,
            ),
            encoding="utf-8",
        )

        machine = load_machine(path)

        assert machine.require_solder().initial_purge_ul == pytest.approx(amount)

    def test_unknown_line_direction_is_rejected(self):
        with pytest.raises(ValueError) as raised:
            _solder(line_direction="sideways")

        assert "線走行方向" in str(raised.value)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("initial_purge_ul", -0.01),
            ("lift_height", 0.0),
            ("lift_height", -1.0),
            ("area_short_side_factor", 0.0),
            ("area_short_side_factor", -1.0),
            ("diameter", 0.0),
            ("max_fill_speed", 0.0),
            ("max_dispense_rate", -1.0),
            ("dispense_accel", 0.0),
            ("retract_amount", 0.0),
            ("retract_accel_factor", 1.0),
            ("bead_width_factor", 0.0),
            ("overlap", -0.1),
            ("overlap", 1.0),
            ("boundary_margin", float("nan")),
            ("ul_per_mm2", 0.0),
            ("prime_extra_delay", -0.1),
            ("paste_height", float("nan")),
            ("diameter", float("inf")),
            ("max_fill_speed", True),
            ("outer_diameter", 0.0),
            ("micro_line_z_start", 0.0),
            ("micro_line_z_end", -0.01),
            ("micro_line_z_end", "fast"),
            ("micro_line_z_start", "start"),
            ("micro_line_settle_seconds", -0.1),
            ("post_dispense_settle_seconds", -0.1),
        ],
    )
    def test_paste_settings_out_of_range_are_rejected(self, key, value):
        # FillSequence / PasteApplicator は検証済み値を前提にするため入口で弾く。
        with pytest.raises(ValueError, match=key):
            _solder(**{key: value})

    def test_dispense_dynamics_boundary_values_are_accepted(self):
        dispenser = _solder(overlap=0.0, boundary_margin=0.0)

        assert dispenser.defaults.overlap == 0.0
        assert dispenser.defaults.boundary_margin == 0.0

    def test_negative_boundary_margin_is_accepted(self):
        # 負の外周余白は pad 外形をはみ出して塗る指定
        dispenser = _solder(boundary_margin=-0.2)

        assert dispenser.defaults.boundary_margin == -0.2

    def test_nozzle_outer_diameter_defaults_to_inner_diameter(self):
        nozzle = _solder(diameter=0.3).nozzle

        assert nozzle.outer_diameter is None
        assert nozzle.effective_outer_diameter == pytest.approx(0.3)

    def test_nozzle_outer_diameter_smaller_than_inner_is_rejected(self):
        with pytest.raises(ValueError, match="outer_diameter"):
            _solder(diameter=0.3, outer_diameter=0.29)

    def test_post_dispense_settle_defaults_to_a_tenth_of_a_second(self):
        assert _solder().motion.post_dispense_settle_seconds == pytest.approx(0.1)

    def test_dispense_motion_joins_the_dispenser_and_the_motion(self):
        """Fill sequence の吐出の動きは [solder.dispenser] と [solder.motion] の値."""
        solder = load_machine(MINIMAL_MACHINE_TOML).require_solder()

        assert solder.dispense_motion == DispenseMotion(
            max_fill_speed=2.0,
            max_dispense_rate=5.0,
            dispense_accel=10.0,
            retract_amount=10.0,
            retract_rate=50.0,
            retract_accel_factor=2.0,
            lift_height=3.0,
            micro_line_settle_seconds=0.1,
            post_dispense_settle_seconds=0.1,
        )

    def test_micro_line_start_defaults_to_auto_and_end_follows_it(self):
        dispenser = _solder()

        assert dispenser.defaults.micro_line_z_start == "auto"
        assert dispenser.defaults.micro_line_z_end == "start"

    @pytest.mark.parametrize("value", ["start", "auto", 0.05])
    def test_micro_line_end_is_read_as_written(self, value):
        assert _solder(micro_line_z_end=value).defaults.micro_line_z_end == value

    def test_micro_line_mode_is_accepted(self):
        dispenser = _solder(dispense_mode="micro_line")

        assert dispenser.defaults.dispense_mode == "micro_line"

    def test_missing_required_section_is_a_config_error(self, tmp_path):
        source = (MINIMAL_MACHINE_TOML).read_text()
        path = tmp_path / "machine.toml"
        path.write_text(source.split("[probe]")[0], encoding="utf-8")

        with pytest.raises(
            ConfigError, match="必須の項目がありません @ machine.toml.probe"
        ):
            load_machine(path)


class TestCapabilities:
    """機体の機能は machine.toml の節の有無で表す（[solder] ははんだ塗布、[pnp] は部品実装）."""

    def test_solder_section_makes_a_solder_machine(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.solder is not None
        assert machine.pnp is None
        assert load_capabilities(MINIMAL_MACHINE_TOML) == ("solder",)

    def test_pnp_section_alone_makes_a_pnp_machine(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()))

        machine = load_machine(path)

        assert machine.solder is None
        assert machine.pnp == PnpConfig()
        assert load_capabilities(path) == ("pnp",)

    def test_both_sections_make_a_machine_with_both(self, tmp_path: Path):
        machine = _machine_with(tmp_path, "[pnp]\n")

        assert machine.solder is not None
        assert machine.pnp == PnpConfig()
        assert load_capabilities(tmp_path / "machine.toml") == ("solder", "pnp")

    def test_a_machine_without_either_is_a_config_error(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()).replace("[pnp]", ""))

        with pytest.raises(ConfigError, match=r"\[solder\]（はんだ塗布）か \[pnp\]"):
            load_machine(path)

    def test_pnp_has_no_keys_yet(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="未知のキーです: nozzle"):
            load_machine(_write_with(tmp_path, "[pnp]\nnozzle = 1\n"))

    def test_capabilities_are_known_while_the_config_is_invalid(self, tmp_path: Path):
        """設定エラーの間も、機能ごとのジョブと画面を出せる."""
        path = _write_with(tmp_path, **{"paste_density = 3.78": "paste_density = -1.0"})

        with pytest.raises(ConfigError):
            load_machine(path)
        assert load_capabilities(path) == ("solder",)

    def test_solder_is_required_only_by_the_solder_jobs(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()))
        machine = load_machine(path)

        with pytest.raises(ConfigError, match=r"\[solder\] がありません"):
            machine.require_solder()
        assert load_machine(MINIMAL_MACHINE_TOML).require_solder().paste_density == 3.78

    def test_parks_at_the_nozzle_cap_of_a_solder_machine(self, tmp_path: Path):
        cap = "[solder.nozzle.cap]\nx = 1.0\ny = 2.0\nz = 3.0\n"
        pnp = tmp_path / "pnp.toml"
        pnp.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()))

        assert _machine_with(tmp_path, cap).cap_position == Point3d(1.0, 2.0, 3.0)
        assert load_machine(MINIMAL_MACHINE_TOML).cap_position is None
        assert load_machine(pnp).cap_position is None


class TestLoadMachine:
    """machine.toml 全体の一括検証（設定エラーは ConfigError にまとめる）."""

    def test_reports_every_invalid_section_at_once(self, tmp_path):
        """1 つ直すたびに次の誤りが出てくる、を繰り返させない."""
        path = _write_with(
            tmp_path,
            "[settle]\nmove_sec = -1.0\n",
            **{"lift_height = 3.0": "lift_height = 0.0"},
        )

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "move_sec" in str(raised.value)
        assert "lift_height" in str(raised.value)
        assert raised.value.sections == {"settle", "solder"}

    def test_unparsable_toml_is_a_config_error(self, tmp_path):
        path = _write_with(tmp_path, 'broken = "unterminated\n')

        with pytest.raises(ConfigError, match="TOML として読めません"):
            load_machine(path)

    def test_calibration_file_resolves_against_the_config_dir(self, tmp_path):
        machine = load_machine(_write_with(tmp_path))

        assert machine.camera.calibration_file == tmp_path / "calibration.json"


class TestLoadKlipper:
    """設定エラーの間も緊急停止を送れるよう、[klipper] 節だけを読む."""

    def test_reads_klipper_even_when_the_rest_is_invalid(self, tmp_path):
        path = _write_with(tmp_path, **{"lift_height = 3.0": "lift_height = 0.0"})

        assert load_klipper(path) == KlipperConfig(host="127.0.0.1", port=7126)

    def test_missing_section_is_a_config_error(self, tmp_path):
        path = tmp_path / "machine.toml"
        path.write_text("[probe]\n", encoding="utf-8")

        with pytest.raises(ConfigError, match="klipper"):
            load_klipper(path)

    def test_connection_has_no_default(self, tmp_path):
        """書き忘れたまま本番の Moonraker（localhost:7125）へつながないように."""
        path = tmp_path / "machine.toml"
        path.write_text('[klipper]\nhost = "localhost"\n', encoding="utf-8")

        with pytest.raises(ConfigError, match="klipper.port"):
            load_klipper(path)


class TestAudio:
    """Raspberry Pi 本体から再生する通知音の出力設定."""

    def test_defaults_to_system_default_device_and_75_percent(self):
        assert AudioConfig() == AudioConfig("default", 0.75)

    def test_strips_device(self):
        assert AudioConfig(device="  plughw:CARD=Audio,DEV=0  ") == AudioConfig(
            device="plughw:CARD=Audio,DEV=0"
        )

    @pytest.mark.parametrize("volume", [0.0, 0.25, 1.0])
    def test_accepts_volume_in_closed_unit_interval(self, volume: float):
        assert AudioConfig(device="default", volume=volume).volume == pytest.approx(
            volume
        )

    @pytest.mark.parametrize(
        "volume",
        [True, float("nan"), float("inf"), float("-inf"), -0.01, 1.01],
        ids=["bool", "nan", "positive-infinity", "negative-infinity", "below", "above"],
    )
    def test_rejects_invalid_volume(self, volume: object):
        with pytest.raises(ValueError, match="volume"):
            AudioConfig(device="default", volume=volume)  # type: ignore[arg-type]

    @pytest.mark.parametrize("device", ["", " ", "\t\n"])
    def test_rejects_blank_device(self, device: str):
        with pytest.raises(ValueError, match="device"):
            AudioConfig(device=device)


class TestMachineAudio:
    """MachineConfig.audio の任意 [audio] section 読み込み（欠落は既定値）."""

    def test_reads_audio_section(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path, '[audio]\ndevice = "  hw:0,0  "\nvolume = 0.35\n'
        )

        assert machine.audio == AudioConfig(device="hw:0,0", volume=0.35)

    def test_defaults_volume_when_omitted(self, tmp_path: Path):
        machine = _machine_with(tmp_path, '[audio]\ndevice = "plughw:CARD=X,DEV=0"\n')

        assert machine.audio == AudioConfig(device="plughw:CARD=X,DEV=0", volume=0.75)

    def test_defaults_whole_config_when_section_is_absent(self):
        assert load_machine(MINIMAL_MACHINE_TOML).audio == AudioConfig()


class TestAlignmentRegionSettings:
    """重複領域による銅箔位置合わせ設定の公開契約."""

    @pytest.mark.parametrize("threshold", [0.0, 0.25])
    def test_accepts_nonnegative_refinement_threshold(self, threshold: float):
        alignment = AlignmentConfig(refine_max_short_side=threshold)

        assert alignment.refine_max_short_side == pytest.approx(threshold)

    @pytest.mark.parametrize(
        "threshold",
        [True, -0.01, float("nan"), float("inf"), float("-inf")],
        ids=["bool", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_rejects_invalid_refinement_threshold(self, threshold):
        with pytest.raises(ValueError, match="refine_max_short_side"):
            AlignmentConfig(refine_max_short_side=threshold)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("region_size_px", 0),
            ("region_size_px", -1),
            ("region_size_px", True),
            ("region_size_px", 1.5),
            ("max_passes", 0),
            ("max_passes", -1),
            ("max_passes", True),
            ("max_passes", 1.5),
        ],
    )
    def test_rejects_non_positive_dimensions_and_counts(self, key, value):
        with pytest.raises(ValueError, match=key):
            AlignmentConfig(**{key: value})

    @pytest.mark.parametrize("overlap", [-0.01, 1.0, 1.01])
    def test_rejects_overlap_outside_half_open_unit_interval(self, overlap):
        with pytest.raises(ValueError, match="region_overlap"):
            AlignmentConfig(region_overlap=overlap)

    @pytest.mark.parametrize("overlap", [0.0, 0.5, 0.999])
    def test_accepts_overlap_inside_half_open_unit_interval(self, overlap):
        assert AlignmentConfig(region_overlap=overlap).region_overlap == pytest.approx(
            overlap
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("tolerance", 0.0),
            ("tolerance", -0.01),
            ("converge_tolerance", 0.0),
            ("converge_tolerance", -0.01),
            ("board_edge_margin", 0.0),
            ("board_edge_margin", -0.01),
        ],
    )
    def test_rejects_invalid_distance_settings(self, key, value):
        with pytest.raises(ValueError, match=key):
            AlignmentConfig(**{key: value})

    @pytest.mark.parametrize(
        "blur_ksize",
        [0, -1, 2, 4, True, 3.5],
        ids=["zero", "negative", "even-two", "even-four", "bool", "non-integer"],
    )
    def test_rejects_blur_kernel_that_is_not_a_positive_odd_integer(self, blur_ksize):
        with pytest.raises(ValueError, match="blur_ksize"):
            AlignmentConfig(blur_ksize=blur_ksize)

    @pytest.mark.parametrize("blur_ksize", [1, 3, 7])
    def test_accepts_valid_blur_kernel(self, blur_ksize: int):
        alignment = AlignmentConfig(blur_ksize=blur_ksize)

        assert alignment.blur_ksize == blur_ksize


class TestFlowCalibration:
    """運転時流量キャリブレーション設定の公開契約."""

    def test_is_enabled_by_the_calibration_file_alone(self):
        """測定位置は基板ごとの設定なので、machine 側は校正ファイルだけで決まる."""
        assert VolumeCorrectionConfig(calibration_file="c.json").enabled is True
        assert VolumeCorrectionConfig().enabled is False

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("amount_ul", 0.0),
            ("amount_ul", -0.1),
            ("amount_ul", float("nan")),
            ("crop_size_mm", 0.0),
            ("crop_size_mm", -1.0),
            ("settle_seconds", -1.0),
            ("settle_seconds", float("nan")),
            ("max_correction_scale", 1.0),
            ("max_correction_scale", 0.5),
            ("max_correction_scale", float("inf")),
        ],
    )
    def test_rejects_invalid_values(self, key, value):
        with pytest.raises(ValueError, match=key):
            VolumeCorrectionConfig(**{key: value})

    def test_accepts_zero_settle_seconds_to_skip_the_wait(self):
        assert VolumeCorrectionConfig(settle_seconds=0.0).settle_seconds == 0.0

    def test_allows_doubling_or_halving_the_factor_by_default(self):
        assert VolumeCorrectionConfig().max_correction_scale == pytest.approx(2.0)


class TestMachineSolderVolumeCorrection:
    """machine.toml の ``[solder.volume_correction]`` の読み込み."""

    def test_defaults_when_the_section_is_absent(self, tmp_path):
        machine = _machine_with(tmp_path, "")

        assert machine.require_solder().volume_correction == VolumeCorrectionConfig()

    def test_reads_the_section_when_present(self, tmp_path):
        machine = _machine_with(
            tmp_path,
            "[solder.volume_correction]\n"
            'calibration_file = "cal.paste-volume.json"\n'
            "amount_ul = 0.15\n"
            "crop_size_mm = 2.4\n"
            "settle_seconds = 4.0\n"
            "max_correction_scale = 1.5\n",
        )
        flow = machine.require_solder().volume_correction

        assert flow.calibration_file == "cal.paste-volume.json"
        assert flow.amount_ul == pytest.approx(0.15)
        assert flow.crop_size_mm == pytest.approx(2.4)
        assert flow.settle_seconds == pytest.approx(4.0)
        assert flow.max_correction_scale == pytest.approx(1.5)
        assert flow.enabled is True


class TestUnknownKeys:
    """書き間違えたキーが黙って既定値で動かないよう、未知のキーは設定エラーにする."""

    def test_unknown_top_level_key_is_a_config_error(self, tmp_path):
        path = _write_with(
            tmp_path, **{"[klipper]": 'machine_type = "paste"\n\n[klipper]'}
        )

        with pytest.raises(ConfigError, match="未知のキーです: machine_type") as raised:
            load_machine(path)

        assert raised.value.sections == {"machine_type"}

    def test_unknown_key_in_a_section_is_a_config_error(self, tmp_path):
        path = _write_with(
            tmp_path,
            **{"[solder]\n": "[solder]\nair_pump_enabled = true\n"},
        )

        with pytest.raises(ConfigError, match="air_pump_enabled") as raised:
            load_machine(path)

        assert raised.value.sections == {"solder"}


@pytest.mark.parametrize("section", ["cap", "clean"])
class TestNozzlePosition:
    """ノズル位置の座標は「3 つそろうか、すべて無いか」の 1 組.

    座標が無いのは立ち上げ途中の正常な状態（未教示）。一部だけなら設定エラーにする。
    """

    def test_missing_section_is_untaught(self, section: str):
        nozzle = load_machine(MINIMAL_MACHINE_TOML).require_solder().nozzle

        assert getattr(nozzle, section).position is None

    def test_reads_recorded_position(self, tmp_path: Path, section: str):
        machine = _machine_with(
            tmp_path, f"[solder.nozzle.{section}]\nx = 10.0\ny = 20.0\nz = 3.5\n"
        )

        position = getattr(machine.require_solder().nozzle, section).position
        assert position == Point3d(10.0, 20.0, 3.5)

    def test_partial_coordinates_are_a_config_error(self, tmp_path: Path, section: str):
        """原点や未記録で埋めて動かさず、設定エラーとして運転者に見せる."""
        path = _write_with(tmp_path, f"[solder.nozzle.{section}]\nx = 12.5\n")

        with pytest.raises(ConfigError, match="x / y / z の 3 つ"):
            load_machine(path)


class TestNozzleClean:
    """NozzleClean の動作値（座標が未教示でも読み、検証する）."""

    def test_motion_only_section_is_untaught_but_keeps_its_values(self, tmp_path: Path):
        """設定ページから押し込み量だけ保存した状態（座標はまだ教示していない）."""
        machine = _machine_with(tmp_path, "[solder.nozzle.clean]\npress_depth = 0.4\n")

        assert machine.require_solder().nozzle.clean == NozzleCleanConfig(
            press_depth=0.4
        )

    def test_reads_recorded_values(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path,
            "[solder.nozzle.clean]\n"
            "x = 10.0\ny = 20.0\nz = -30.0\n"
            "press_depth = 0.4\npurge_ul = 0.3\n"
            "stroke = 1.5\npasses = 3\nwipe_speed = 8.0\n",
        )

        assert machine.require_solder().nozzle.clean == NozzleCleanConfig(
            position=Point3d(10.0, 20.0, -30.0),
            press_depth=0.4,
            purge_ul=0.3,
            stroke=1.5,
            passes=3,
            wipe_speed=8.0,
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("press_depth", -0.1),
            ("purge_ul", -1.0),
            ("stroke", -1.0),
            ("passes", -1),
            ("passes", 1.5),
            ("passes", True),
            ("wipe_speed", 0.0),
            ("wipe_speed", -1.0),
        ],
    )
    def test_rejects_invalid_values(self, key, value):
        with pytest.raises(ValueError, match=key):
            NozzleCleanConfig(**{key: value})

    @pytest.mark.parametrize(
        ("key", "value"),
        [("x", float("inf")), ("y", float("nan")), ("z", float("-inf"))],
    )
    def test_rejects_non_finite_position(self, key, value):
        coordinates = {"x": 10.0, "y": 20.0, "z": -30.0, key: value}

        with pytest.raises(ValueError, match=key):
            NozzleCleanConfig(position=Point3d(**coordinates))

    @pytest.mark.parametrize("passes", [0, 1])
    def test_accepts_zero_and_one_pass(self, passes):
        """こすり回数 0 はこすり無効として受理する."""
        assert NozzleCleanConfig(passes=passes).passes == passes

    def test_accepts_zero_for_optional_steps(self):
        """0 は「その工程を行わない」を意味するので受理する."""
        assert NozzleCleanConfig(press_depth=0.0).press_depth == 0.0
        assert NozzleCleanConfig(purge_ul=0.0).purge_ul == 0.0
        assert NozzleCleanConfig(stroke=0.0).stroke == 0.0


class TestProbe:
    """Probeクラスのテスト."""

    def test_min_samples_less_than_6_raises(self):
        # 2次曲面フィットには6点以上が必要なため min_samples < 6 は弾かれる。
        with pytest.raises(ValueError, match="min_samplesは6以上"):
            ProbeConfig(min_radius=1.5, min_samples=5)

    def test_min_samples_greater_than_max_samples_raises(self):
        # min/max とも6以上にして順序ガードのみを検証する。
        with pytest.raises(ValueError, match="min_samplesはmax_samples以下"):
            ProbeConfig(min_radius=1.5, min_samples=8, max_samples=6)

    @pytest.mark.parametrize("min_radius", [0.0, -1.0])
    def test_min_radius_not_positive_raises(self, min_radius):
        with pytest.raises(ValueError, match="min_radiusは正の値"):
            ProbeConfig(min_radius=min_radius)

    @pytest.mark.parametrize("board_edge_margin", [0.0, -1.0])
    def test_board_edge_margin_not_positive_raises(self, board_edge_margin):
        with pytest.raises(ValueError, match="board_edge_marginは正の値"):
            ProbeConfig(min_radius=1.5, board_edge_margin=board_edge_margin)


class TestMachineSettle:
    """`[settle]`: 装置の静定待ち（セクションごと省略可）."""

    def test_defaults_whole_section_when_absent(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.settle == SettleConfig()

    def test_reads_section(self, tmp_path):
        machine = _machine_with(tmp_path, "[settle]\nmove_sec = 0.8\nprobe_sec = 0.2\n")

        assert machine.settle == SettleConfig(move_sec=0.8, probe_sec=0.2)

    def test_accepts_zero(self):
        assert SettleConfig(move_sec=0.0, probe_sec=0.0).move_sec == 0.0

    @pytest.mark.parametrize(
        ("key", "value"), [("move_sec", -0.1), ("probe_sec", -1.0)]
    )
    def test_rejects_negative(self, key, value):
        with pytest.raises(ValueError, match=key):
            SettleConfig(**{key: value})


class TestMachineTact:
    """`[solder.tact]`: 塗布の所要時間の予測（セクションごと省略可。速度は printer.cfg から読む）."""

    def test_defaults_whole_section_when_absent(self):
        solder = load_machine(MINIMAL_MACHINE_TOML).require_solder()

        assert solder.tact == TactConfig()

    def test_reads_the_values(self, tmp_path):
        machine = _machine_with(
            tmp_path,
            "[solder.tact]\nfixed_sec = 60.0\nprobe_point_sec = 3.0\n"
            "capture_sec = 0.4\nalign_passes = 2.0\n",
        )

        assert machine.require_solder().tact == TactConfig(
            fixed_sec=60.0, probe_point_sec=3.0, capture_sec=0.4, align_passes=2.0
        )

    @pytest.mark.parametrize("key", ["travel_speed", "setup_sec"])
    def test_retired_keys_are_unknown(self, tmp_path, key):
        """速度は printer.cfg から読み、前段全体の時間は計算するので、machine.toml には書けない（移行が消す）."""
        path = tmp_path / "machine.toml"
        path.write_text(
            MINIMAL_MACHINE_TOML.read_text() + f"\n[solder.tact]\n{key} = 10.0\n"
        )

        with pytest.raises(ConfigError, match=key):
            load_machine(path)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("fixed_sec", -1.0),
            ("probe_point_sec", -0.1),
            ("capture_sec", -0.1),
            ("align_passes", 0.5),
        ],
    )
    def test_rejects_out_of_range(self, key, value):
        with pytest.raises(ValueError, match=key):
            TactConfig(**{key: value})


class TestMachineDetection:
    """`[detection]`: 統計検出のサンプリング（セクションごと省略可）."""

    def test_defaults_whole_section_when_absent(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.detection == DetectionConfig()

    def test_reads_section(self, tmp_path):
        machine = _machine_with(
            tmp_path, "[detection]\nsample_count = 20\nminimum_sample_count = 8\n"
        )

        assert machine.detection == DetectionConfig(
            sample_count=20, minimum_sample_count=8
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("sample_count", 0),
            ("sample_count", 1.5),
            ("minimum_sample_count", 0),
        ],
    )
    def test_rejects_invalid_counts(self, key, value):
        with pytest.raises(ValueError, match=key):
            DetectionConfig(**{key: value})

    def test_rejects_minimum_above_sample_count(self):
        # 有効検出の下限が撮る枚数を超えたら、その観測は決して成立しない
        with pytest.raises(ValueError, match="minimum_sample_count"):
            DetectionConfig(sample_count=4, minimum_sample_count=5)


@pytest.mark.parametrize(
    "path",
    sorted((PROJECT_ROOT / "deploy" / "templates").glob("*/machine.toml")),
    ids=lambda path: path.parent.name,
)
class TestTemplates:
    """新しい機体の machine.toml の雛形は、そのまま読める."""

    def test_loads(self, path: Path):
        load_machine(path)
