from pathlib import Path

import pytest

from pcbasm.config import (
    BoardAlign,
    Camera,
    CameraCrop,
    Corner,
    Klipper,
    Machine,
    NozzleCap,
    PadAlign,
    PasteDispenser,
    Probe,
    ReferencePoint,
    Toolhead,
    get_machine_config,
    resolve_paste_height,
)
from pcbasm.geometry import Point2d, Shift
from tests.helpers import TESTING_DATA_DIR


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
            ul_per_mm2=0.2,
            solder_paste_density=3.78,
            dispense_mode="auto",
            auto_line_aspect_ratio=1.618,
        )
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
            offset=(0.0, -5.0),
            corner=Corner.TOP_LEFT,
        )

    def test_default_values(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        assert machine.klipper == Klipper(host="localhost", port=7125)
        assert machine.camera.device_id == 0
        assert machine.camera.format == "YUYV"

    def test_pad_align_defaults_when_section_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.pad_align == PadAlign()
        assert machine.paste_dispenser.pad_align.tolerance == pytest.approx(0.05)

    def test_solder_paste_density_defaults_when_absent(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace("solder_paste_density = 3.78\n", ""),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.solder_paste_density == pytest.approx(3.78)

    def test_pad_align_section_overrides_defaults(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source + "\n[paste_dispenser.pad_align]\ntolerance = 0.08\nmin_roi = 5.0\n"
        )

        pad_align = Machine(path).paste_dispenser.pad_align

        assert pad_align.tolerance == pytest.approx(0.08)
        assert pad_align.min_roi == pytest.approx(5.0)
        assert pad_align.canny_low == pytest.approx(100.0)  # 未指定はデフォルト

    def test_air_pump_enabled_defaults_true_when_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.air_pump_enabled is True

    def test_air_pump_enabled_explicit_false(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                "[paste_dispenser]\n",
                "[paste_dispenser]\nair_pump_enabled = false\n",
                1,
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.air_pump_enabled is False

    def test_initial_purge_ul_defaults_when_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.initial_purge_ul == pytest.approx(0.1)

    def test_auto_area_short_side_factor_defaults_when_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.auto_area_short_side_factor == pytest.approx(3.0)

    def test_initial_purge_ul_reads_explicit_value(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                "[paste_dispenser]\n",
                "[paste_dispenser]\ninitial_purge_ul = 0.25\n",
                1,
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.initial_purge_ul == pytest.approx(0.25)

    def test_initial_purge_ul_allows_zero_to_disable(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace(
                "[paste_dispenser]\n",
                "[paste_dispenser]\ninitial_purge_ul = 0.0\n",
                1,
            ),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.initial_purge_ul == pytest.approx(0.0)

    def test_effective_retract_rate_falls_back_to_max_dispense_rate_when_absent(
        self, tmp_path
    ):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace("retract_rate = 50.0\n", ""),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.retract_rate is None
        assert machine.paste_dispenser.effective_retract_rate == pytest.approx(
            machine.paste_dispenser.max_dispense_rate
        )
        assert machine.paste_dispenser.effective_retract_rate == pytest.approx(5.0)

    def test_initial_purge_ul_rejects_negative_value(self):
        with pytest.raises(ValueError, match="initial_purge_ul"):
            _paste_dispenser(initial_purge_ul=-0.01)

    @pytest.mark.parametrize("factor", [0.0, -1.0])
    def test_auto_area_short_side_factor_rejects_non_positive(self, factor):
        with pytest.raises(ValueError, match="auto_area_short_side_factor"):
            _paste_dispenser(auto_area_short_side_factor=factor)

    def test_effective_retract_rate_returns_explicit_value(self):
        dispenser = _paste_dispenser()

        assert dispenser.effective_retract_rate == pytest.approx(50.0)

    def test_raises_key_error_when_config_not_defined(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        with pytest.raises(
            KeyError, match="'paste_dispenser' は設定ファイルに定義されていません"
        ):
            machine.paste_dispenser


class TestPadAlignMaxFailures:
    """PadAlign.max_failures のテスト（paste-align-max-failures 計画書「公開 IF」節）.

    照合失敗の許容部品数。デフォルト 0（1 部品でも失敗したら塗布ジョブを即中止）。
    """

    def test_defaults_to_zero_when_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.pad_align.max_failures == 0

    def test_reads_explicit_value(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source + "\n[paste_dispenser.pad_align]\nmax_failures = 2\n",
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.pad_align.max_failures == 2

    def test_rejects_negative_value(self):
        with pytest.raises(ValueError, match="max_failures"):
            PadAlign(max_failures=-1)


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


class TestNozzleCap:
    """Machine.nozzle_cap のテスト（nozzle-cap-parking 計画書「公開インターフェース」節）.

    未記録（[nozzle_cap] セクションなし）が正常状態なので None を返す。
    """

    def test_missing_section_returns_none(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.nozzle_cap is None

    def test_reads_recorded_position(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source + "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.nozzle_cap == NozzleCap(x=10.0, y=20.0, z=3.5)


class TestCamera:
    """Cameraクラスのテスト."""

    def test_size(self):
        camera = Camera(
            width=640,
            height=480,
            fps=30.0,
            crop=CameraCrop(width=400, height=400),
            calibration_file=Path("calibration.json"),
        )

        assert camera.size == (640, 480)


class TestCameraCrop:
    """CameraCropクラスのテスト."""

    def test_size(self):
        crop = CameraCrop(width=400, height=300)

        assert crop.size == (400, 300)


class TestToolhead:
    """Toolheadクラスのテスト."""

    def test_to_transform(self):
        toolhead = Toolhead(x=13.2, y=54.7)

        assert toolhead.to_transform() == Shift(x=13.2, y=54.7)


class TestCorner:
    """Corner.board_position のテスト（board-corner-calibration 計画書「Config スキーマ」節）.

    基板寸法 (width, height) から各コーナーの board 座標を返す。 TL=(0,0), TR=(w,0),
    BL=(0,h), BR=(w,h)。
    """

    @pytest.mark.parametrize(
        ("corner", "expected"),
        [
            (Corner.TOP_LEFT, Point2d(0.0, 0.0)),
            (Corner.TOP_RIGHT, Point2d(30.0, 0.0)),
            (Corner.BOTTOM_LEFT, Point2d(0.0, 20.0)),
            (Corner.BOTTOM_RIGHT, Point2d(30.0, 20.0)),
        ],
    )
    def test_board_position_maps_dimensions_to_corner(
        self, corner: Corner, expected: Point2d
    ):
        assert corner.board_position(30.0, 20.0) == expected


class TestReferencePoint:
    """ReferencePoint（アンカー1コーナー + 単一 offset）のテスト.

    board-corner-calibration 計画書「Config スキーマ」節: marker = corner +
    offset。基準点はアンカーコーナー1点のみで、コーナーは TOML 文字列で指定 する（省略時 top_left）。
    """

    def test_to_point_returns_marker_machine_position(self):
        ref = ReferencePoint(x=10.0, y=20.0, target_diameter=3.0, offset=(2.5, -2.5))

        assert ref.to_point() == Point2d(10.0, 20.0)

    def test_offset_point_returns_offset_as_point(self):
        ref = ReferencePoint(x=10.0, y=20.0, target_diameter=3.0, offset=(2.5, -2.5))

        assert ref.offset_point() == Point2d(2.5, -2.5)

    def test_corner_structures_from_toml_string(self, tmp_path):
        """TOML の corner = "bottom_right" が Corner.BOTTOM_RIGHT に structure
        される."""
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace('corner = "top_left"', 'corner = "bottom_right"', 1),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.reference_point.corner == Corner.BOTTOM_RIGHT

    def test_corner_defaults_to_top_left_when_absent(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace('corner = "top_left"\n', "", 1),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.reference_point.corner == Corner.TOP_LEFT


class TestBoardAlign:
    """BoardAlign と Machine.board_align のテスト（board-corner-calibration 計画書）.

    [board_align] はトップレベル節で全項目に既定値があり、節欠落可。
    """

    def test_default_values(self):
        board_align = BoardAlign()

        assert board_align.tolerance == pytest.approx(0.05)
        assert board_align.max_correction == pytest.approx(2.0)
        assert board_align.search_window == pytest.approx(1.5)
        assert board_align.edge_length == pytest.approx(2.0)
        assert board_align.theta_range == pytest.approx(2.0)
        assert board_align.canny_low == pytest.approx(100.0)
        assert board_align.canny_high == pytest.approx(200.0)
        assert board_align.blur_ksize == 5

    def test_section_absent_returns_defaults(self, tmp_path):
        path = tmp_path / "machine.toml"
        path.write_text('machine_type = "paste"\n', encoding="utf-8")

        machine = Machine(path)

        assert machine.board_align == BoardAlign()

    def test_toml_section_overrides_defaults(self, tmp_path):
        path = tmp_path / "machine.toml"
        path.write_text(
            'machine_type = "paste"\n'
            "\n[board_align]\ntolerance = 0.08\nblur_ksize = 7\n",
            encoding="utf-8",
        )

        board_align = Machine(path).board_align

        assert board_align.tolerance == pytest.approx(0.08)
        assert board_align.blur_ksize == 7
        assert board_align.search_window == pytest.approx(1.5)  # 未指定はデフォルト


class TestProbe:
    """Probeクラスのテスト."""

    def test_sample_defaults(self):
        probe = Probe(min_radius=1.5)

        assert probe.min_samples == 6
        assert probe.max_samples == 9
        assert probe.lift_height == 1.0

    def test_valid_custom_values(self):
        probe = Probe(
            min_radius=2.0,
            lift_height=2.5,
            min_samples=7,
            max_samples=12,
        )

        assert probe.min_radius == 2.0
        assert probe.lift_height == 2.5
        assert probe.min_samples == 7
        assert probe.max_samples == 12

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


class TestGetMachineConfig:
    """get_machine_config関数のテスト."""

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
offset = [0.0, 0.0]
"""

    def test_loads_machine_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "configs" / "test_machine"
        config_dir.mkdir(parents=True)
        (config_dir / "machine.toml").write_text(self._MINIMAL_TOML)

        monkeypatch.setattr("pcbasm.config.PROJECT_ROOT", tmp_path)

        machine = get_machine_config("test_machine")

        assert isinstance(machine, Machine)
        assert machine.klipper == Klipper(host="localhost", port=7125)

    def test_raises_file_not_found_for_nonexistent_machine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pcbasm.config.PROJECT_ROOT", tmp_path)

        with pytest.raises(FileNotFoundError):
            get_machine_config("nonexistent")


class TestResolvePasteHeight:
    """resolve_paste_height: auto は ul_per_mm2（膜厚 [mm]）を、数値はその値を返す。"""

    def test_auto_returns_ul_per_mm2(self):
        assert resolve_paste_height("auto", 0.08) == pytest.approx(0.08)

    def test_numeric_returns_value(self):
        assert resolve_paste_height(0.2, 0.08) == pytest.approx(0.2)
