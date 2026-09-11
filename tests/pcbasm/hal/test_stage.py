import pytest
from pytest_mock import MockerFixture

from pcbasm.geometry import Path, Point3d
from pcbasm.hal import Speed
from pcbasm.hal.klipper import Klipper
from pcbasm.hal.stage import Limits, ScalarLimits, XYZStage
from tests.helpers import mark_hardware


class TestXYZStage:
    """XYZStageクラスのテスト."""

    @pytest.fixture
    def stage(self) -> XYZStage:
        klipper = Klipper()
        stage = XYZStage(klipper.readonly)
        return stage

    @pytest.fixture
    def mock_stage(self, mocker: MockerFixture) -> XYZStage:
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
                "printer": {"max_velocity": "300"},
            },
        )
        return XYZStage(klipper.readonly)

    @mark_hardware
    def test_get_position(self, stage: XYZStage):
        position = stage.get_position()

        assert isinstance(position, Point3d)

    @mark_hardware
    def test_limits(self, stage: XYZStage):
        limits = stage.limits

        assert isinstance(limits, Limits)
        assert isinstance(limits.x, ScalarLimits)
        assert isinstance(limits.y, ScalarLimits)
        assert isinstance(limits.z, ScalarLimits)
        assert isinstance(limits.v, ScalarLimits)
        assert limits.x.min < limits.x.max
        assert limits.y.min < limits.y.max
        assert limits.z.min < limits.z.max
        assert limits.v.min < limits.v.max

    def test_max_velocity(self, mock_stage: XYZStage):
        assert mock_stage.max_velocity == 300.0

    def test_limits_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"printer": {"max_velocity": "300"}},
        )
        stage = XYZStage(klipper.readonly)

        with pytest.raises(KeyError, match=r"printer\.cfgに\[stepper_x\]セクション"):
            _ = stage.limits

    def test_limits_missing_position_keys(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"step_pin": "PC0"},
                "stepper_y": {"step_pin": "PC1"},
                "stepper_z": {"step_pin": "PC2"},
                "printer": {"max_velocity": "300"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"position_minとposition_max"):
            _ = stage.limits

    def test_limits_missing_printer_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"printer\.cfgに\[printer\]セクション"):
            _ = stage.limits

    def test_limits_missing_max_velocity(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
                "printer": {"kinematics": "cartesian"},
            },
        )

        stage = XYZStage(klipper.readonly)
        with pytest.raises(KeyError, match=r"max_velocity"):
            _ = stage.limits

    def test_to_gcode_emits_one_g1_per_point(self, mock_stage: XYZStage):
        # Path の各点を解決済み feed 付き G1 として 1 行ずつ発行する。
        # speed=Speed.absolute(100) → F = 100*60 = 6000.0。
        path = Path([Point3d(50, 100, 25), Point3d(60, 100, 25)])

        result = mock_stage.to_gcode(path, speed=Speed.absolute(100))

        assert result.to_list() == [
            "G1 X50.0 Y100.0 Z25.0 F6000.0",
            "G1 X60.0 Y100.0 Z25.0 F6000.0",
        ]

    def test_to_gcode_raises_when_any_point_out_of_limits(self, mock_stage: XYZStage):
        # x=150 は x∈[0,100] の範囲外。
        path = Path([Point3d(150, 100, 25)])

        with pytest.raises(ValueError, match="制限外"):
            mock_stage.to_gcode(path, speed=Speed.absolute(100))

    @pytest.fixture
    def mock_stage_at_10(self, mocker: MockerFixture) -> XYZStage:
        # move() の部分/相対座標解決用に、現在位置を (10, 10, 10) に固定した stage。
        # limits は mock_stage と同じ。get_position は
        # get_status("gcode_move","gcode_position") の [0:3] を読むため列を返す。
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                "stepper_x": {"position_min": "0", "position_max": "100"},
                "stepper_y": {"position_min": "0", "position_max": "200"},
                "stepper_z": {"position_min": "0", "position_max": "50"},
                "printer": {"max_velocity": "300"},
            },
        )
        mocker.patch.object(
            klipper.readonly,
            "get_status",
            return_value=[10.0, 10.0, 10.0, 0.0],
        )
        return XYZStage(klipper.readonly)

    def test_move_emits_single_g1_for_absolute_point(self, mock_stage: XYZStage):
        # 全座標指定なので現在位置の解決は不要。
        result = mock_stage.move(x=50, y=100, z=25, speed=Speed.absolute(100))

        assert result.to_list() == ["G1 X50.0 Y100.0 Z25.0 F6000.0"]

    def test_move_emits_only_specified_axes(self, mock_stage_at_10: XYZStage):
        # x, y は省略。現在位置 (10, 10, 10) で補完せず、指定した z のみ出力する。
        result = mock_stage_at_10.move(z=25, speed=Speed.absolute(100))

        assert result.to_list() == ["G1 Z25.0 F6000.0"]

    def test_move_relative_adds_to_current_position(self, mock_stage_at_10: XYZStage):
        # relative=True は現在位置 (10, 10, 10) への加算。z は省略のため出力しない。
        result = mock_stage_at_10.move(
            x=5, y=3, speed=Speed.absolute(100), relative=True
        )

        assert result.to_list() == ["G1 X15.0 Y13.0 F6000.0"]

    @pytest.mark.parametrize("relative", [False, True])
    def test_move_without_axes_raises(self, mock_stage: XYZStage, relative: bool):
        with pytest.raises(ValueError, match="軸が指定されていません"):
            mock_stage.move(relative=relative)

    def test_move_relative_raises_when_resolved_out_of_limits(
        self, mock_stage_at_10: XYZStage
    ):
        # 現在位置 x=10 に +95 で 105 となり x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            mock_stage_at_10.move(x=95, speed=Speed.absolute(100), relative=True)

    def test_move_defaults_speed_to_max_velocity(self, mock_stage: XYZStage):
        # speed 未指定なら max_velocity(=300) で解決され F = 300*60。
        result = mock_stage.move(x=50, y=50, z=25)

        assert result.to_list() == ["G1 X50.0 Y50.0 Z25.0 F18000.0"]

    @pytest.mark.parametrize(
        ("x", "y", "z"),
        [(None, None, 51.0), (150.0, 50.0, 25.0)],
        ids=["specified-axis", "resolved-point"],
    )
    def test_move_raises_when_out_of_limits(
        self,
        mock_stage: XYZStage,
        x: float | None,
        y: float | None,
        z: float | None,
    ):
        # z=51 は z∈[0,50]、x=150 は x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            mock_stage.move(x=x, y=y, z=z, speed=Speed.absolute(100))


class TestScalarLimits:
    """ScalarLimitsクラスのテスト."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, True),  # 最小値
            (100.0, True),  # 最大値
            (50.0, True),  # 中間値
            (-0.1, False),  # 最小値未満
            (100.1, False),  # 最大値超過
        ],
    )
    def test_contains(self, value: float, expected: bool):
        limits = ScalarLimits(min=0.0, max=100.0)

        assert (value in limits) == expected


class TestLimits:
    """Limitsクラスのテスト."""

    @pytest.fixture
    def limits(self) -> Limits:
        return Limits(
            x=ScalarLimits(min=0.0, max=100.0),
            y=ScalarLimits(min=0.0, max=200.0),
            z=ScalarLimits(min=0.0, max=50.0),
            v=ScalarLimits(min=0.0, max=300.0),
        )

    @pytest.mark.parametrize(
        ("point", "feed", "expected"),
        [
            (Point3d(x=50.0, y=100.0, z=25.0), 150.0, True),  # 全制限内
            (Point3d(x=0.0, y=0.0, z=0.0), 0.0, True),  # 全最小値
            (Point3d(x=100.0, y=200.0, z=50.0), 300.0, True),  # 全最大値
            (Point3d(x=-1.0, y=100.0, z=25.0), 150.0, False),  # x軸が範囲外
            (Point3d(x=50.0, y=201.0, z=25.0), 150.0, False),  # y軸が範囲外
            (Point3d(x=50.0, y=100.0, z=51.0), 150.0, False),  # z軸が範囲外
            (Point3d(x=50.0, y=100.0, z=25.0), 301.0, False),  # 速度が範囲外
        ],
    )
    def test_contains_point_feed(
        self, limits: Limits, point: Point3d, feed: float, expected: bool
    ):
        assert limits.contains(point, feed) == expected


class TestSpeed:
    """Speedクラスのテスト.

    Speedは送り速度を表す値オブジェクト。絶対値[mm/s]か、max_velocityに対する
    割合[0,1]のいずれかで構築され、resolve(max_velocity)で実際の速度に解決される。
    """

    @pytest.mark.parametrize("max_velocity", [300.0, 1000.0])
    def test_absolute_ignores_max(self, max_velocity: float):
        # 絶対値はmax_velocityを無視するため、どのmaxでも同じ値を返す
        speed = Speed.absolute(150.0)

        assert speed.resolve(max_velocity) == 150.0

    @pytest.mark.parametrize(
        ("fraction", "max_velocity", "expected"),
        [
            (0.5, 300.0, 150.0),
            (1.0, 300.0, 300.0),
            (0.0, 300.0, 0.0),
        ],
    )
    def test_rate_resolves_to_fraction_of_max(self, fraction, max_velocity, expected):
        speed = Speed.rate(fraction)

        assert speed.resolve(max_velocity) == expected

    @pytest.mark.parametrize("fraction", [-0.1, 1.1])
    def test_rate_out_of_range_raises(self, fraction):
        with pytest.raises(ValueError):
            Speed.rate(fraction)
