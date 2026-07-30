from pathlib import Path

import pytest

from pcbasm.config import (
    Audio,
    Camera,
    CameraCrop,
    Corner,
    CornerOffsets,
    Klipper,
    Machine,
    NozzleCap,
    PadAlign,
    PasteDispenser,
    Probe,
    ReferencePoint,
    Toolhead,
    get_config_dir,
    get_machine_config,
    resolve_paste_height,
)
from pcbasm.geometry import Point2d, Shift
from tests.helpers import PROJECT_ROOT, TESTING_DATA_DIR


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
            ),
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

    def test_initial_purge_ul_defaults_when_absent(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.paste_dispenser.initial_purge_ul == pytest.approx(0.1)

    def test_lift_height_defaults_when_absent(self, tmp_path):
        source = (TESTING_DATA_DIR / "machine.toml").read_text()
        path = tmp_path / "machine.toml"
        path.write_text(
            source.replace("lift_height = 3.0\n", ""),
            encoding="utf-8",
        )

        machine = Machine(path)

        assert machine.paste_dispenser.lift_height == pytest.approx(2.0)

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

    @pytest.mark.parametrize("height", [0.0, -1.0])
    def test_lift_height_rejects_non_positive_value(self, height):
        with pytest.raises(ValueError, match="lift_height"):
            _paste_dispenser(lift_height=height)

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


class TestCornerOffsets:
    """CornerOffsetsクラスのテスト."""

    def test_requires_at_least_two_non_top_left_corners(self):
        with pytest.raises(ValueError, match="少なくとも2つ"):
            CornerOffsets(top_left=(0.0, -5.0))

    def test_requires_at_least_two_non_top_left_corners_with_one(self):
        with pytest.raises(ValueError, match="少なくとも2つ"):
            CornerOffsets(top_left=(0.0, -5.0), top_right=(0.0, -5.0))

    def test_has_corner_returns_true_for_defined_corners(self):
        offsets = CornerOffsets(
            top_left=(0.0, -5.0),
            top_right=(0.0, -5.0),
            bottom_left=(5.0, 5.0),
        )

        assert offsets.has_corner(Corner.TOP_LEFT) is True
        assert offsets.has_corner(Corner.TOP_RIGHT) is True
        assert offsets.has_corner(Corner.BOTTOM_LEFT) is True
        assert offsets.has_corner(Corner.BOTTOM_RIGHT) is False

    def test_get_returns_point2d(self):
        offsets = CornerOffsets(
            top_left=(1.0, -2.0),
            top_right=(3.0, -4.0),
            bottom_left=(5.0, 5.0),
        )

        assert offsets.get(Corner.TOP_LEFT) == Point2d(1.0, -2.0)
        assert offsets.get(Corner.TOP_RIGHT) == Point2d(3.0, -4.0)

    def test_get_raises_for_undefined_corner(self):
        offsets = CornerOffsets(
            top_left=(0.0, -5.0),
            top_right=(0.0, -5.0),
            bottom_left=(5.0, 5.0),
        )

        with pytest.raises(ValueError, match="BOTTOM_RIGHT"):
            offsets.get(Corner.BOTTOM_RIGHT)


class TestReferencePoint:
    """ReferencePointクラスのテスト."""

    def test_to_point(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        assert ref.to_point() == Point2d(10.0, 20.0)

    def test_get_reference_position_default_is_top_left(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
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

    def test_sample_defaults(self):
        probe = Probe(min_radius=1.5)

        assert probe.board_edge_margin == 2.5
        assert probe.min_samples == 6
        assert probe.max_samples == 9
        assert probe.lift_height == 1.0

    def test_valid_custom_values(self):
        probe = Probe(
            min_radius=2.0,
            board_edge_margin=3.0,
            lift_height=2.5,
            min_samples=7,
            max_samples=12,
        )

        assert probe.min_radius == 2.0
        assert probe.board_edge_margin == 3.0
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
