from pathlib import Path

import pytest

from pcbasm.config import (
    Audio,
    Camera,
    CameraCrop,
    Corner,
    CornerOffsets,
    FlowCalibration,
    Klipper,
    Machine,
    NozzleCap,
    NozzleClean,
    PadAlign,
    PasteDispenser,
    Probe,
    ReferencePoint,
    Toolhead,
    get_config_dir,
    get_machine_config,
    resolve_paste_height,
)
from pcbasm.geometry import Point2d
from tests.helpers import PROJECT_ROOT, TESTING_DATA_DIR


def _machine_with(tmp_path: Path, extra: str) -> Machine:
    """検証用 machine.toml の末尾に ``extra`` を足した Machine を作る."""
    path = tmp_path / "machine.toml"
    source = (TESTING_DATA_DIR / "machine.toml").read_text()
    path.write_text(f"{source}\n{extra}", encoding="utf-8")
    return Machine(path)


def _paste_dispenser(**overrides):
    values = {
        "rotations_per_ul": 1.0,
        "nozzle_diameter": 0.21,
        "max_fill_speed": 2.0,
        "max_dispense_rate": 5.0,
        "dispense_accel": 10.0,
        "retract_amount": 10.0,
        "retract_rate": 50.0,
        "retract_accel_factor": 2.0,
        "toolhead": Toolhead(x=13.2, y=54.7),
        "paste_height": "auto",
        "ul_per_mm2": 0.2,
        "solder_paste_density": 3.78,
        "dispense_mode": "auto",
        "auto_line_aspect_ratio": 1.618,
    }
    values.update(overrides)
    return PasteDispenser(**values)


class TestMachine:
    """Machineクラスのテスト."""

    def test_load_config(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.klipper == Klipper(host="192.168.1.100", port=7125)
        assert machine.paste_dispenser == PasteDispenser(
            rotations_per_ul=1.0,
            nozzle_diameter=0.21,
            max_fill_speed=2.0,
            max_dispense_rate=5.0,
            dispense_accel=10.0,
            retract_amount=10.0,
            retract_rate=50.0,
            retract_accel_factor=2.0,
            toolhead=Toolhead(x=13.2, y=54.7),
            paste_height="auto",
            lift_height=3.0,
            ul_per_mm2=0.2,
            solder_paste_density=3.78,
            dispense_mode="auto",
            auto_line_aspect_ratio=1.618,
        )
        assert machine.paste_dispenser.line_direction == "unconstrained"
        assert machine.camera == Camera(
            device_id=0,
            width=640,
            height=480,
            fps=30.0,
            format="YUYV",
            crop=CameraCrop(width=400, height=400),
            calibration_file=TESTING_DATA_DIR / "calibration.json",
        )
        assert machine.reference_point == ReferencePoint(
            x=23.1,
            y=8.3,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(0.0, -5.0),
                top_right=(0.0, -5.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(5.0, 5.0),
            ),
        )

    def test_default_values(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        assert machine.klipper == Klipper(host="localhost", port=7125)
        assert machine.camera.device_id == 0
        assert machine.camera.format == "YUYV"

    def test_rejects_legacy_three_corner_offsets(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace("bottom_right = [5.0, 5.0]\n", ""),
            encoding="utf-8",
        )
        machine = Machine(path)

        with pytest.raises(ExceptionGroup):
            machine.reference_point

    def test_pad_align_defaults_when_section_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.pad_align == PadAlign()
        assert machine.paste_dispenser.pad_align.region_size_px == 100
        assert machine.paste_dispenser.pad_align.refine_max_short_side == pytest.approx(
            0.4
        )

    @pytest.mark.parametrize(
        ("removed_line", "attribute", "expected"),
        [
            ("solder_paste_density = 3.78\n", "solder_paste_density", 3.78),
            ("lift_height = 3.0\n", "lift_height", 2.0),
            ("", "initial_purge_ul", 0.1),
            ("", "auto_area_short_side_factor", 3.0),
        ],
    )
    def test_optional_keys_fall_back_to_defaults(
        self, tmp_path, removed_line: str, attribute: str, expected: float
    ):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        if removed_line:
            source = source.replace(removed_line, "")
        path = tmp_path / "machine.toml"
        path.write_text(source, encoding="utf-8")

        dispenser = Machine(path).paste_dispenser

        assert getattr(dispenser, attribute) == pytest.approx(expected)

    def test_pad_align_section_overrides_defaults(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source + "\n[paste_dispenser.pad_align]\nregion_size_px = 160\n"
            "region_overlap = 0.25\n"
            "refine_max_short_side = 0.25\n"
            "blur_ksize = 3\n"
        )

        pad_align = Machine(path).paste_dispenser.pad_align

        assert pad_align.region_size_px == 160
        assert pad_align.region_overlap == pytest.approx(0.25)
        assert pad_align.refine_max_short_side == pytest.approx(0.25)
        assert pad_align.canny_low == pytest.approx(100.0)  # 未指定はデフォルト
        assert pad_align.blur_ksize == 3

    @pytest.mark.parametrize("amount", [0.25, 0.0], ids=["explicit", "zero-disables"])
    def test_initial_purge_ul_reads_explicit_value(self, tmp_path, amount: float):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                "[paste_dispenser]\n",
                f"[paste_dispenser]\ninitial_purge_ul = {amount}\n",
                1,
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.initial_purge_ul == pytest.approx(amount)

    @pytest.mark.parametrize(
        ("retract_rate", "expected"),
        [(None, 5.0), (50.0, 50.0)],
        ids=["absent-falls-back-to-max-dispense-rate", "explicit"],
    )
    def test_effective_retract_rate(self, retract_rate: float | None, expected: float):
        dispenser = _paste_dispenser(retract_rate=retract_rate)

        assert dispenser.effective_retract_rate == pytest.approx(expected)

    def test_initial_purge_ul_rejects_negative_value(self):
        with pytest.raises(ValueError, match="initial_purge_ul"):
            _paste_dispenser(initial_purge_ul=-0.01)

    def test_unknown_line_direction_is_rejected(self):
        with pytest.raises(ValueError) as raised:
            _paste_dispenser(line_direction="sideways")

        assert "線走行方向" in str(raised.value)

    @pytest.mark.parametrize("height", [0.0, -1.0])
    def test_lift_height_rejects_non_positive_value(self, height):
        with pytest.raises(ValueError, match="lift_height"):
            _paste_dispenser(lift_height=height)

    @pytest.mark.parametrize("factor", [0.0, -1.0])
    def test_auto_area_short_side_factor_rejects_non_positive(self, factor):
        with pytest.raises(ValueError, match="auto_area_short_side_factor"):
            _paste_dispenser(auto_area_short_side_factor=factor)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("nozzle_diameter", 0.0),
            ("max_fill_speed", 0.0),
            ("max_dispense_rate", -1.0),
            ("retract_amount", 0.0),
            ("retract_accel_factor", 1.0),
            ("bead_width_factor", 0.0),
            ("overlap", -0.1),
            ("overlap", 1.0),
            ("boundary_margin", -0.01),
        ],
    )
    def test_dispense_dynamics_out_of_range_is_rejected(self, key, value):
        # FillSequence / PasteApplicator は検証済み値を前提にするため入口で弾く。
        with pytest.raises(ValueError, match=key):
            _paste_dispenser(**{key: value})

    def test_dispense_dynamics_boundary_values_are_accepted(self):
        dispenser = _paste_dispenser(overlap=0.0, boundary_margin=0.0)

        assert dispenser.overlap == 0.0
        assert dispenser.boundary_margin == 0.0

    def test_raises_key_error_when_config_not_defined(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        with pytest.raises(
            KeyError, match="'paste_dispenser' は設定ファイルに定義されていません"
        ):
            machine.paste_dispenser


class TestAudio:
    """Raspberry Pi 本体から再生する通知音の出力設定."""

    def test_defaults_to_system_default_device_and_75_percent(self):
        assert Audio() == Audio("default", 0.75)

    def test_strips_device(self):
        assert Audio(device="  plughw:CARD=Audio,DEV=0  ") == Audio(
            device="plughw:CARD=Audio,DEV=0"
        )

    @pytest.mark.parametrize("volume", [0.0, 0.25, 1.0])
    def test_accepts_volume_in_closed_unit_interval(self, volume: float):
        assert Audio(device="default", volume=volume).volume == pytest.approx(volume)

    @pytest.mark.parametrize(
        "volume",
        [True, float("nan"), float("inf"), float("-inf"), -0.01, 1.01],
        ids=["bool", "nan", "positive-infinity", "negative-infinity", "below", "above"],
    )
    def test_rejects_invalid_volume(self, volume: object):
        with pytest.raises(ValueError, match="volume"):
            Audio(device="default", volume=volume)  # type: ignore[arg-type]

    @pytest.mark.parametrize("device", ["", " ", "\t\n"])
    def test_rejects_blank_device(self, device: str):
        with pytest.raises(ValueError, match="device"):
            Audio(device=device)


class TestMachineAudio:
    """Machine.audio の任意 [audio] section 読み込み（欠落は既定値）."""

    def test_reads_audio_section(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(
            '[audio]\ndevice = "  hw:0,0  "\nvolume = 0.35\n',
            encoding="utf-8",
        )

        assert Machine(path).audio == Audio(device="hw:0,0", volume=0.35)

    def test_defaults_volume_when_omitted(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text('[audio]\ndevice = "plughw:CARD=X,DEV=0"\n', encoding="utf-8")

        assert Machine(path).audio == Audio(device="plughw:CARD=X,DEV=0", volume=0.75)

    def test_defaults_whole_config_when_section_is_absent(self):
        assert Machine(TESTING_DATA_DIR / "machine_minimal.toml").audio == Audio()


class TestPadAlignRegionSettings:
    """重複領域による銅箔位置合わせ設定の公開契約."""

    @pytest.mark.parametrize("threshold", [0.0, 0.25])
    def test_accepts_nonnegative_refinement_threshold(self, threshold: float):
        pad_align = PadAlign(refine_max_short_side=threshold)

        assert pad_align.refine_max_short_side == pytest.approx(threshold)

    @pytest.mark.parametrize(
        "threshold",
        [True, -0.01, float("nan"), float("inf"), float("-inf")],
        ids=["bool", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_rejects_invalid_refinement_threshold(self, threshold):
        with pytest.raises(ValueError, match="refine_max_short_side"):
            PadAlign(refine_max_short_side=threshold)

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
            PadAlign(**{key: value})

    @pytest.mark.parametrize("overlap", [-0.01, 1.0, 1.01])
    def test_rejects_overlap_outside_half_open_unit_interval(self, overlap):
        with pytest.raises(ValueError, match="region_overlap"):
            PadAlign(region_overlap=overlap)

    @pytest.mark.parametrize("overlap", [0.0, 0.5, 0.999])
    def test_accepts_overlap_inside_half_open_unit_interval(self, overlap):
        assert PadAlign(region_overlap=overlap).region_overlap == pytest.approx(overlap)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("converge_tolerance", 0.0),
            ("converge_tolerance", -0.01),
            ("board_edge_margin", 0.0),
            ("board_edge_margin", -0.01),
        ],
    )
    def test_rejects_invalid_distance_settings(self, key, value):
        with pytest.raises(ValueError, match=key):
            PadAlign(**{key: value})

    @pytest.mark.parametrize(
        "blur_ksize",
        [0, -1, 2, 4, True, 3.5],
        ids=["zero", "negative", "even-two", "even-four", "bool", "non-integer"],
    )
    def test_rejects_blur_kernel_that_is_not_a_positive_odd_integer(self, blur_ksize):
        with pytest.raises(ValueError, match="blur_ksize"):
            PadAlign(blur_ksize=blur_ksize)

    @pytest.mark.parametrize("blur_ksize", [1, 3, 7])
    def test_accepts_valid_blur_kernel(self, blur_ksize: int):
        pad_align = PadAlign(blur_ksize=blur_ksize)

        assert pad_align.blur_ksize == blur_ksize


class TestFlowCalibration:
    """運転時流量キャリブレーション設定の公開契約."""

    def test_is_enabled_by_the_calibration_file_alone(self):
        """測定位置は基板ごとの設定なので、machine 側は校正ファイルだけで決まる."""
        assert FlowCalibration(calibration_file="c.json").enabled is True
        assert FlowCalibration().enabled is False

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
        ],
    )
    def test_rejects_invalid_values(self, key, value):
        with pytest.raises(ValueError, match=key):
            FlowCalibration(**{key: value})

    def test_accepts_zero_settle_seconds_to_skip_the_wait(self):
        assert FlowCalibration(settle_seconds=0.0).settle_seconds == 0.0


class TestMachinePasteDispenserFlowCalibration:
    """machine.toml の ``[paste_dispenser.flow_calibration]`` の読み込み."""

    def test_defaults_when_the_section_is_absent(self, tmp_path):
        machine = _machine_with(tmp_path, "")

        assert machine.paste_dispenser.flow_calibration == FlowCalibration()

    def test_reads_the_section_when_present(self, tmp_path):
        machine = _machine_with(
            tmp_path,
            "[paste_dispenser.flow_calibration]\n"
            'calibration_file = "cal.paste-volume.json"\n'
            "amount_ul = 0.15\n"
            "crop_size_mm = 2.4\n"
            "settle_seconds = 4.0\n",
        )
        flow = machine.paste_dispenser.flow_calibration

        assert flow.calibration_file == "cal.paste-volume.json"
        assert flow.amount_ul == pytest.approx(0.15)
        assert flow.crop_size_mm == pytest.approx(2.4)
        assert flow.settle_seconds == pytest.approx(4.0)
        assert flow.enabled is True


class TestMachineType:
    """Machine.machine_type のテスト（nozzle-cap-parking 計画書「公開インターフェース」節）.

    machine_type は必須キー: 欠落はアクセス時 KeyError、paste / pnp 以外は ValueError。
    """

    def test_reads_paste_from_config(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.machine_type == "paste"

    def test_reads_pnp_from_config(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace('machine_type = "paste"', 'machine_type = "pnp"', 1),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.machine_type == "pnp"

    def test_missing_machine_type_raises_key_error(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        with pytest.raises(KeyError, match="machine_type"):
            machine.machine_type

    def test_unknown_machine_type_raises_value_error(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace('machine_type = "paste"', 'machine_type = "sander"', 1),
            encoding="utf-8",
        )

        machine = Machine(path)

        with pytest.raises(ValueError, match="machine_type"):
            machine.machine_type


class TestMachineName:
    """Machine.machine_name のテスト.

    計画書 docs/plans/web-api-ui-split.md「MR2」節: 表示名は任意キーなので未設定は None
    を返す。ホスト名などへのフォールバックは環境依存なので API 層の責務。
    """

    def test_missing_key_returns_none(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.machine_name is None

    def test_reads_top_level_bare_key(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                'machine_type = "paste"',
                'machine_type = "paste"\nmachine_name = "黒兎 2 号機"',
                1,
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.machine_name == "黒兎 2 号機"

    def test_non_string_value_reads_as_none(self, tmp_path):
        """型が違う値でも例外にしない（表示名が壊れてもページを落とさない）."""
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                'machine_type = "paste"', 'machine_type = "paste"\nmachine_name = 42', 1
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.machine_name is None


class TestNozzleCap:
    """Machine.nozzle_cap のテスト（nozzle-cap-parking 計画書「公開インターフェース」節）.

    未記録（[paste_dispenser.nozzle_cap] セクションなし）が正常状態なので None を返す。
    """

    def test_missing_section_returns_none(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.nozzle_cap is None

    def test_reads_recorded_position(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path, "[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n"
        )

        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)

    def test_reads_the_legacy_top_level_section(self, tmp_path: Path):
        """旧 [nozzle_cap] のままでも読む（設定を書き換えずに動き続ける）."""
        machine = _machine_with(tmp_path, "[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n")

        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)

    def test_new_section_wins_over_the_legacy_one(self, tmp_path: Path):
        """移行済みの値を旧セクションの残骸で上書きさせない."""
        machine = _machine_with(
            tmp_path,
            "[nozzle_cap]\nx = 1.0\ny = 2.0\nz = 3.0\n"
            "[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
        )

        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)


class TestNozzleClean:
    """Machine.nozzle_clean と NozzleClean のテスト.

    未記録（[paste_dispenser.nozzle_clean] セクションなし）が正常状態なので None を返す。
    座標は既定値を持たず、欠けたテーブルは未記録として扱う（原点へ行く事故を防ぐ）。
    """

    def test_missing_section_returns_none(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.nozzle_clean is None

    def test_reads_the_legacy_top_level_section(self, tmp_path: Path):
        """旧 [nozzle_clean] のままでも読む."""
        machine = _machine_with(
            tmp_path, "[nozzle_clean]\nx = 10.0\ny = 20.0\nz = -30.0\n"
        )

        assert machine.nozzle_clean == NozzleClean(x=10.0, y=20.0, z=-30.0)

    def test_reads_position_with_defaults(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path, "[paste_dispenser.nozzle_clean]\nx = 10.0\ny = 20.0\nz = -30.0\n"
        )

        assert machine.nozzle_clean == NozzleClean(x=10.0, y=20.0, z=-30.0)

    def test_reads_recorded_values(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path,
            "[paste_dispenser.nozzle_clean]\n"
            "x = 10.0\ny = 20.0\nz = -30.0\n"
            "press_depth = 0.4\npurge_ul = 0.3\n"
            "stroke = 1.5\npasses = 3\nwipe_speed = 8.0\n",
        )

        assert machine.nozzle_clean == NozzleClean(
            x=10.0,
            y=20.0,
            z=-30.0,
            press_depth=0.4,
            purge_ul=0.3,
            stroke=1.5,
            passes=3,
            wipe_speed=8.0,
        )

    def test_press_z_subtracts_press_depth_from_surface(self):
        """こすり Z は面 Z から押し込み量だけ下がる（Z は 0 が上・負が下）."""
        clean = NozzleClean(x=1.0, y=2.0, z=-30.0, press_depth=0.4)

        assert clean.press_z == pytest.approx(-30.4)

    def test_missing_coordinate_reads_as_not_recorded(self, tmp_path: Path):
        """座標が欠けたテーブルは既定値で埋めず「未記録」として扱う."""
        machine = _machine_with(
            tmp_path, "[paste_dispenser.nozzle_clean]\npress_depth = 0.4\n"
        )

        assert machine.nozzle_clean is None

    def test_missing_coordinate_keeps_the_rest_of_paste_dispenser_readable(
        self, tmp_path: Path
    ):
        """不完全なサブテーブルで塗布パラメータ全体を巻き添えにしない.

        設定ページから押し込み量だけ保存すればこの状態になる。巻き添えにすると /settings
        の塗布パラメータが全て「未設定」になり、装置ページが 503 する。
        """
        machine = _machine_with(
            tmp_path, "[paste_dispenser.nozzle_clean]\npress_depth = 0.4\n"
        )

        assert machine.paste_dispenser.nozzle_diameter > 0
        assert machine.paste_dispenser.nozzle_clean is None

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
            ("x", float("inf")),
            ("y", float("nan")),
            ("z", "auto"),
        ],
    )
    def test_rejects_invalid_values(self, key, value):
        with pytest.raises(ValueError, match=key):
            NozzleClean(**{"x": 10.0, "y": 20.0, "z": -30.0, key: value})

    @pytest.mark.parametrize("passes", [0, 1])
    def test_accepts_zero_and_one_pass(self, passes):
        """こすり回数 0 はこすり無効として受理する."""
        assert NozzleClean(x=1.0, y=2.0, z=-3.0, passes=passes).passes == passes

    def test_accepts_zero_for_optional_steps(self):
        """0 は「その工程を行わない」を意味するので受理する."""
        assert NozzleClean(x=1.0, y=2.0, z=-3.0, press_depth=0.0).press_depth == 0.0
        assert NozzleClean(x=1.0, y=2.0, z=-3.0, purge_ul=0.0).purge_ul == 0.0
        assert NozzleClean(x=1.0, y=2.0, z=-3.0, stroke=0.0).stroke == 0.0


class TestLegacyNozzleSections:
    """旧トップレベル [nozzle_cap] / [nozzle_clean] の読み替え.

    設定ファイルを書き換えなくても動き続けるよう、読み込み時に [paste_dispenser]
    配下へ写す。ファイル自体の移行は設定を書き込むときに ConfigStore が行う
    （契約は tests/web/api/test_config_store.py::TestLegacyNozzleSectionMigration）。
    """

    def test_legacy_section_is_visible_through_paste_dispenser(self, tmp_path: Path):
        """ノズル専用のアクセサと paste_dispenser 経由で同じ値が見える.

        ここが食い違うと、/settings が「未設定」でノズル位置ページが「記録済み」と 並ぶような表示になる。
        """
        machine = _machine_with(tmp_path, "[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n")

        assert machine.paste_dispenser.nozzle_cap == machine.nozzle_cap
        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)

    def test_legacy_clean_section_is_visible_through_paste_dispenser(
        self, tmp_path: Path
    ):
        machine = _machine_with(
            tmp_path, "[nozzle_clean]\nx = 1.0\ny = 2.0\nz = -3.0\n"
        )

        assert machine.paste_dispenser.nozzle_clean == machine.nozzle_clean

    def test_new_section_wins_over_the_legacy_one(self, tmp_path: Path):
        machine = _machine_with(
            tmp_path,
            "[nozzle_cap]\nx = 1.0\ny = 2.0\nz = 3.0\n"
            "[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
        )

        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)


class TestCornerOffsets:
    """CornerOffsetsクラスのテスト."""

    @pytest.mark.parametrize(
        ("corner", "expected"),
        [
            (Corner.TOP_LEFT, Point2d(1.0, -2.0)),
            (Corner.TOP_RIGHT, Point2d(3.0, -4.0)),
            (Corner.BOTTOM_LEFT, Point2d(5.0, 5.0)),
            (Corner.BOTTOM_RIGHT, Point2d(-5.0, 5.0)),
        ],
    )
    def test_get_returns_each_corner_as_point2d(
        self, corner: Corner, expected: Point2d
    ):
        offsets = CornerOffsets(
            top_left=(1.0, -2.0),
            top_right=(3.0, -4.0),
            bottom_left=(5.0, 5.0),
            bottom_right=(-5.0, 5.0),
        )

        assert offsets.get(corner) == expected


class TestReferencePoint:
    """ReferencePointクラスのテスト."""

    def test_get_reference_position_default_is_top_left(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        assert ref.get_reference_position() == Point2d(10.0, 20.0)

    def test_get_reference_position_top_right(self):
        # ref = (10, 20), offset_top_left = (1, -2)
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_top_right = (9 + 100, 22) = (109, 22)
        # ref_top_right = (109, 22) + (3, -4) = (112, 18)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(3.0, -4.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.TOP_RIGHT, board_width=100.0
        ) == Point2d(112.0, 18.0)

    def test_get_reference_position_top_right_requires_board_width(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        with pytest.raises(ValueError, match="board_widthが必要"):
            ref.get_reference_position(Corner.TOP_RIGHT)

    def test_get_reference_position_bottom_left(self):
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_bottom_left = (9, 22 + 50) = (9, 72)
        # ref_bottom_left = (9, 72) + (5, 5) = (14, 77)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.BOTTOM_LEFT, board_height=50.0
        ) == Point2d(14.0, 77.0)

    def test_get_reference_position_bottom_left_requires_board_height(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        with pytest.raises(ValueError, match="board_heightが必要"):
            ref.get_reference_position(Corner.BOTTOM_LEFT)

    def test_get_reference_position_bottom_right(self):
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_bottom_right = (9 + 100, 22 + 50) = (109, 72)
        # ref_bottom_right = (109, 72) + (-5, 5) = (104, 77)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.BOTTOM_RIGHT, board_width=100.0, board_height=50.0
        ) == Point2d(104.0, 77.0)


class TestProbe:
    """Probeクラスのテスト."""

    def test_min_samples_less_than_6_raises(self):
        # 2次曲面フィットには6点以上が必要なため min_samples < 6 は弾かれる。
        with pytest.raises(ValueError, match="min_samplesは6以上"):
            Probe(min_radius=1.5, min_samples=5)

    def test_min_samples_greater_than_max_samples_raises(self):
        # min/max とも6以上にして順序ガードのみを検証する。
        with pytest.raises(ValueError, match="min_samplesはmax_samples以下"):
            Probe(min_radius=1.5, min_samples=8, max_samples=6)

    @pytest.mark.parametrize("min_radius", [0.0, -1.0])
    def test_min_radius_not_positive_raises(self, min_radius):
        with pytest.raises(ValueError, match="min_radiusは正の値"):
            Probe(min_radius=min_radius)

    @pytest.mark.parametrize("board_edge_margin", [0.0, -1.0])
    def test_board_edge_margin_not_positive_raises(self, board_edge_margin):
        with pytest.raises(ValueError, match="board_edge_marginは正の値"):
            Probe(min_radius=1.5, board_edge_margin=board_edge_margin)


class TestGetMachineConfig:
    """get_config_dir / get_machine_config のテスト.

    PCBASM_CONFIG_DIR env が唯一の注入口（コア層には Settings が無い）。
    """

    _MINIMAL_TOML = """\
[klipper]

[camera]
width = 640
height = 480
fps = 30.0
calibration_file = "calibration.json"

[camera.crop]
width = 400
height = 400

[reference_point]
x = 20.0
y = 10.0
target_diameter = 3.0

[reference_point.offsets]
top_left = [0.0, 0.0]
top_right = [0.0, 0.0]
bottom_left = [0.0, 0.0]
bottom_right = [0.0, 0.0]
"""

    def test_defaults_to_project_root_config(self, monkeypatch):
        monkeypatch.delenv("PCBASM_CONFIG_DIR", raising=False)

        assert get_config_dir() == PROJECT_ROOT / "config"

    def test_loads_machine_config_from_env_dir(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "machine.toml").write_text(self._MINIMAL_TOML)
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(config_dir))

        machine = get_machine_config()

        assert get_config_dir() == config_dir
        assert isinstance(machine, Machine)
        assert machine.klipper == Klipper(host="localhost", port=7125)

    def test_raises_file_not_found_when_machine_toml_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(tmp_path / "empty"))

        with pytest.raises(FileNotFoundError):
            get_machine_config()


class TestResolvePasteHeight:
    """resolve_paste_height: auto は ul_per_mm2（膜厚 [mm]）を、数値はその値を返す。"""

    def test_auto_returns_ul_per_mm2(self):
        assert resolve_paste_height("auto", 0.08) == pytest.approx(0.08)

    def test_numeric_returns_value(self):
        assert resolve_paste_height(0.2, 0.08) == pytest.approx(0.2)
