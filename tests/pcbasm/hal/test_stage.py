import pytest

from pcbasm.geometry import Path, Point3d
from pcbasm.hal import Speed
from pcbasm.hal.fake import FakeKlipper
from pcbasm.hal.stage import (
    TRAVEL_VELOCITY,
    TRAVEL_Z,
    Limits,
    ScalarLimits,
    XYZStage,
)

_CONFIG = {
    "stepper_x": {"position_min": "0", "position_max": "100"},
    "stepper_y": {"position_min": "0", "position_max": "200"},
    "stepper_z": {"position_min": "0", "position_max": "50"},
    "printer": {"max_velocity": "300"},
}


class TestXYZStage:
    """XYZStageクラスのテスト."""

    @pytest.fixture
    def stage(self) -> XYZStage:
        return XYZStage(FakeKlipper(_CONFIG))

    def test_max_velocity(self, stage: XYZStage):
        assert stage.max_velocity == 300.0

    @pytest.mark.parametrize(
        ("homed_axes", "expected"), [("", False), ("xy", False), ("xyz", True)]
    )
    def test_is_homed_requires_all_axes(self, homed_axes: str, expected: bool):
        klipper = FakeKlipper(_CONFIG)
        klipper.set_status("toolhead", "homed_axes", homed_axes)

        assert XYZStage(klipper).is_homed() is expected

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            (
                {"printer": {"max_velocity": "300"}},
                r"printer\.cfgに\[stepper_x\]セクション",
            ),
            (
                {
                    **{f"stepper_{axis}": {"step_pin": "PC0"} for axis in "xyz"},
                    "printer": {"max_velocity": "300"},
                },
                r"position_minとposition_max",
            ),
            (
                {key: value for key, value in _CONFIG.items() if key != "printer"},
                r"printer\.cfgに\[printer\]セクション",
            ),
            ({**_CONFIG, "printer": {"kinematics": "cartesian"}}, r"max_velocity"),
        ],
        ids=["stepper-section", "position-keys", "printer-section", "max-velocity"],
    )
    def test_limits_name_the_missing_printer_cfg_entry(
        self, config: dict[str, dict[str, str]], message: str
    ):
        stage = XYZStage(FakeKlipper(config))

        with pytest.raises(KeyError, match=message):
            _ = stage.limits

    def test_to_gcode_emits_one_g1_per_point(self, stage: XYZStage):
        # Path の各点を解決済み feed 付き G1 として 1 行ずつ発行する。
        # speed=Speed.absolute(100) → F = 100*60 = 6000.0。
        path = Path([Point3d(50, 100, 25), Point3d(60, 100, 25)])

        result = stage.to_gcode(path, speed=Speed.absolute(100))

        assert result.to_list() == [
            "G1 X50.0 Y100.0 Z25.0 F6000.0",
            "G1 X60.0 Y100.0 Z25.0 F6000.0",
        ]

    def test_to_gcode_raises_when_any_point_out_of_limits(self, stage: XYZStage):
        # x=150 は x∈[0,100] の範囲外。
        path = Path([Point3d(150, 100, 25)])

        with pytest.raises(ValueError, match="制限外"):
            stage.to_gcode(path, speed=Speed.absolute(100))

    @pytest.fixture
    def stage_at_10(self) -> XYZStage:
        # move() の部分/相対座標解決用に、現在位置を (10, 10, 10) に置いた stage。limits は stage と同じ。
        klipper = FakeKlipper(_CONFIG)
        klipper.set_status("gcode_move", "gcode_position", [10.0, 10.0, 10.0, 0.0])
        return XYZStage(klipper)

    def test_move_emits_single_g1_for_absolute_point(self, stage: XYZStage):
        # 全座標指定なので現在位置の解決は不要。
        result = stage.move(x=50, y=100, z=25, speed=Speed.absolute(100))

        assert result.to_list() == ["G1 X50.0 Y100.0 Z25.0 F6000.0"]

    def test_move_emits_only_specified_axes(self, stage_at_10: XYZStage):
        # x, y は省略。現在位置 (10, 10, 10) で補完せず、指定した z のみ出力する。
        result = stage_at_10.move(z=25, speed=Speed.absolute(100))

        assert result.to_list() == ["G1 Z25.0 F6000.0"]

    def test_move_relative_adds_to_current_position(self, stage_at_10: XYZStage):
        # relative=True は現在位置 (10, 10, 10) への加算。z は省略のため出力しない。
        result = stage_at_10.move(x=5, y=3, speed=Speed.absolute(100), relative=True)

        assert result.to_list() == ["G1 X15.0 Y13.0 F6000.0"]

    @pytest.mark.parametrize("relative", [False, True])
    def test_move_without_axes_raises(self, stage: XYZStage, relative: bool):
        with pytest.raises(ValueError, match="軸が指定されていません"):
            stage.move(relative=relative)

    def test_move_relative_raises_when_resolved_out_of_limits(
        self, stage_at_10: XYZStage
    ):
        # 現在位置 x=10 に +95 で 105 となり x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            stage_at_10.move(x=95, speed=Speed.absolute(100), relative=True)

    def test_move_defaults_speed_to_max_velocity(self, stage: XYZStage):
        # speed 未指定なら max_velocity(=300) で解決され F = 300*60。
        result = stage.move(x=50, y=50, z=25)

        assert result.to_list() == ["G1 X50.0 Y50.0 Z25.0 F18000.0"]

    @pytest.mark.parametrize(
        ("x", "y", "z"),
        [(None, None, 51.0), (150.0, 50.0, 25.0)],
        ids=["specified-axis", "resolved-point"],
    )
    def test_move_raises_when_out_of_limits(
        self,
        stage: XYZStage,
        x: float | None,
        y: float | None,
        z: float | None,
    ):
        # z=51 は z∈[0,50]、x=150 は x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            stage.move(x=x, y=y, z=z, speed=Speed.absolute(100))


class TestMoveSettled:
    """撮影やプローブの前の移動: 移動 → 静定待ち（G4）→ 到達待ち（M400）."""

    def test_waits_for_settle_then_for_motion_done(self):
        stage = XYZStage(FakeKlipper(_CONFIG))

        result = stage.move_settled(
            x=50, y=100, speed=Speed.absolute(100), settle_sec=0.25
        )

        assert result.to_list() == ["G1 X50.0 Y100.0 F6000.0", "G4 P250", "M400"]

    def test_zero_settle_only_waits_for_motion_done(self):
        stage = XYZStage(FakeKlipper(_CONFIG))

        result = stage.move_settled(z=25, speed=Speed.absolute(100), settle_sec=0.0)

        assert result.to_list() == ["G1 Z25.0 F6000.0", "M400"]


class TestTravelTo:
    """ノズルキャップやクリーニング位置への退避移動.

    Z を退避高さへ上げてから XY へ移り、最後に目的の Z へ下ろす。

    XY の段に Z を含めず（先に上げる意味を壊さない）、M400 も含めない。
    """

    @pytest.mark.parametrize(
        ("max_velocity", "feed"),
        [("10", 600.0), ("300", TRAVEL_VELOCITY * 60)],
    )
    def test_lifts_z_before_xy_at_capped_travel_speed(
        self, max_velocity: str, feed: float
    ):
        config = {**_CONFIG, "printer": {"max_velocity": max_velocity}}
        stage = XYZStage(FakeKlipper(config))

        result = stage.travel_to(Point3d(10.0, 20.0, 3.5))

        assert result.to_list() == [
            "G90",
            f"G1 Z{TRAVEL_Z} F{feed}",
            f"G1 X10.0 Y20.0 F{feed}",
            f"G1 Z3.5 F{feed}",
        ]

    def test_out_of_limits_target_raises(self):
        stage = XYZStage(FakeKlipper(_CONFIG))

        with pytest.raises(ValueError, match="制限外.*x=999.0"):
            stage.travel_to(Point3d(999.0, 20.0, 3.5))


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
        assert limits.contains(x=point.x, y=point.y, z=point.z, feed=feed) == expected

    def test_contains_ignores_unspecified_values(self, limits: Limits):
        # z と feed を渡さなければ、XY だけで判定する
        assert limits.contains(x=50.0, y=100.0)
        assert not limits.contains(x=50.0, y=201.0)

    def test_outside_names_each_violating_value(self, limits: Limits):
        assert limits.outside(x=-1.0, y=100.0, z=51.0, feed=301.0) == [
            "x=-1.0",
            "z=51.0",
            "feed=301.0",
        ]


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
