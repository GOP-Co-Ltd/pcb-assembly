"""PasteApplicator のテスト.

塗布フローは内部で :func:`build_paste_fill_path` を呼び、戻り値の各成分
（連結成分ごとのポリライン）について 1 本ずつ ``FillSequence`` を組んで
``klipper.send_gcode`` を送る（契約メモ §4・成分ループ）。

``FillSequence.to_gcode`` は塗布吐出に ``dispenser.pushpull(amount, ...)`` を
``sync=False`` で発行し、その ``amount`` は
``retraction + extra_amount + total_amount`` である。``extra_amount`` は
``実効レート * prime_extra_delay``（既定 0）なので、塗布 pushpull の量から
各成分の ``total_amount`` を復元して検証する。

Klipper / PasteDispenser / XYZStage はいずれも自前 HAL ABC のため fake 可
（``testing-strategy`` 準拠）。3rd-party 表面はモックしない。
"""

import math

import pytest
from pytest_mock import MockerFixture
from shapely import Polygon, box

from pcbasm import gcode
from pcbasm.config import DispenseMode
from pcbasm.geometry import Compose, HeightPlane, Identity, Point2d, Point3d, Shift
from pcbasm.pasting import PasteApplicator

# 既定ノズル径 0.34（inset=0.17）で 2 成分に分裂する細首ダンベル（凹形）。
# くびれ幅 0.3 < 2*0.17 のため buffer(-0.17) が左右 2 ローブに割れる。
_DUMBBELL_NECK_03 = Polygon(
    [
        (0, 0),
        (4, 0),
        (4, 1.85),
        (6, 1.85),
        (6, 0),
        (10, 0),
        (10, 4),
        (6, 4),
        (6, 2.15),
        (4, 2.15),
        (4, 4),
        (0, 4),
    ]
)


def _machine_surface_z(x: float, y: float):
    return 0.2 + 0.01 * x - 0.005 * y + 0.0002 * x**2 + 0.0001 * y**2 - 0.00015 * x * y


def _machine_height_plane():
    points = [
        (100.0, 30.0),
        (145.0, 32.0),
        (104.0, 70.0),
        (142.0, 68.0),
        (120.0, 45.0),
        (133.0, 58.0),
    ]
    return HeightPlane(
        tuple(Point3d(x, y, _machine_surface_z(x, y)) for x, y in points)
    )


@pytest.fixture
def mock_klipper(mocker: MockerFixture):
    return mocker.Mock()


@pytest.fixture
def mock_paste_dispenser(mocker: MockerFixture):
    dispenser = mocker.Mock()
    dispenser.enable.return_value = gcode.GCode()
    dispenser.disable.return_value = gcode.GCode()
    dispenser.pushpull.return_value = gcode.GCode()
    dispenser.continue_pushpull.return_value = gcode.GCode()
    dispenser.sync.return_value = gcode.GCode()
    dispenser.rotate_revolutions.return_value = gcode.GCode()
    return dispenser


@pytest.fixture
def mock_stage(mocker: MockerFixture):
    stage = mocker.Mock()
    stage.max_velocity = 100.0
    stage.move.return_value = gcode.GCode()
    stage.to_gcode.return_value = gcode.GCode()
    return stage


@pytest.fixture
def applicator(mock_klipper, mock_paste_dispenser, mock_stage):
    return PasteApplicator(
        klipper=mock_klipper,
        paste_dispenser=mock_paste_dispenser,
        stage=mock_stage,
        nozzle_diameter=0.34,
        max_fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        ul_per_mm2=0.05,
        retraction=10.0,
        retraction_rate=10.0,
        retraction_accel_factor=2.0,
        paste_height=0.5,
        dispense_mode="area",
        lift_height=5.0,
    )


def _dispense_amounts(mock_paste_dispenser):
    """塗布吐出（sync=False の pushpull）の量を呼び出し順に取り出す."""
    return [
        call.args[0]
        for call in mock_paste_dispenser.pushpull.call_args_list
        if call.kwargs.get("sync") is False
    ]


class TestSingleComponentPad:
    """単一成分パッド: FillSequence 1 本・total_amount = area*ul_per_mm2."""

    def test_single_send_gcode_per_pad(self, applicator, mock_klipper):
        # Arrange: 単一成分の単純な矩形パッド
        polygon = box(0, 0, 5, 4)

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: 成分 1 つ → send_gcode 1 回
        assert mock_klipper.send_gcode.call_count == 1

    def test_total_amount_is_area_based(self, applicator, mock_paste_dispenser):
        # Arrange
        polygon = box(0, 0, 5, 4)  # area = 20 mm^2
        retraction = 10.0
        ul_per_mm2 = 0.05
        expected_total = polygon.area * ul_per_mm2

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: 塗布吐出量 = retraction + total_amount（extra_amount=0）
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert len(amounts) == 1
        assert amounts[0] == pytest.approx(retraction + expected_total)

    def test_apply_calls_send_gcode_per_polygon(self, applicator, mock_klipper):
        # Arrange: 単一成分パッド 2 つ
        polygons = [box(0, 0, 2, 3), box(5, 5, 8, 9)]

        # Act
        applicator.apply(polygons, transform=Identity())
        # Assert: 各パッド単一成分 → 合計 2 回
        assert mock_klipper.send_gcode.call_count == 2


class TestMultiComponentPad:
    """複数成分パッド（凹形）: FillSequence N 本・各 total_amount = area*ul/N."""

    def test_send_gcode_once_per_component(self, applicator, mock_klipper):
        # Arrange: 既定ノズル径で 2 成分に割れる細首ダンベル
        polygon = _DUMBBELL_NECK_03

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: 成分数 N=2 → send_gcode 2 回
        assert mock_klipper.send_gcode.call_count == 2

    def test_total_amount_split_evenly(self, applicator, mock_paste_dispenser):
        # Arrange: total_amount を成分数で均等配分（決定事項A）
        polygon = _DUMBBELL_NECK_03
        retraction = 10.0
        ul_per_mm2 = 0.05
        n_components = 2
        per_component_total = polygon.area * ul_per_mm2 / n_components

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: 各塗布吐出量が retraction + (area*ul / N)
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert len(amounts) == n_components
        for amount in amounts:
            assert amount == pytest.approx(retraction + per_component_total)

    def test_total_amount_sum_equals_area_based(self, applicator, mock_paste_dispenser):
        # Arrange: 成分配分の総和が元面積ベースの総量と一致（丸めのみ）
        polygon = _DUMBBELL_NECK_03
        retraction = 10.0
        ul_per_mm2 = 0.05
        expected_total = polygon.area * ul_per_mm2

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: sum(total_amount) == area*ul_per_mm2
        amounts = _dispense_amounts(mock_paste_dispenser)
        total_dispensed = sum(a - retraction for a in amounts)
        assert total_dispensed == pytest.approx(expected_total)


class TestEmptyFallback:
    """空 / 不正ポリゴン → build が [] → warning ＆ skip（send_gcode 0 回）."""

    def test_empty_polygon_skips(self, applicator, mock_klipper):
        # Arrange: 空ポリゴン → build_paste_fill_path は []
        polygon = Polygon()

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: 1 本も送信しない
        mock_klipper.send_gcode.assert_not_called()

    def test_invalid_polygon_skips(self, applicator, mock_klipper):
        # Arrange: 自己交差する不正ポリゴン → []
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
        assert not invalid.is_valid  # 前提: 不正形状

        # Act
        applicator.apply([invalid], transform=Identity())
        # Assert
        mock_klipper.send_gcode.assert_not_called()

    def test_empty_polygon_list_skips(self, applicator, mock_klipper):
        # Act
        applicator.apply([], transform=Identity())
        # Assert
        mock_klipper.send_gcode.assert_not_called()


class TestDispenseProtocol:
    """吐出プロトコル（プライム+吐出の連続動作・sync・コンテキスト）."""

    def test_dispense_uses_sync_false(self, applicator, mock_paste_dispenser):
        # Arrange: 単一成分パッド
        polygon = box(0, 0, 2, 3)

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: prime+吐出と連続リトラクションを非同期 queue し、最後に同期する。
        mock_paste_dispenser.pushpull.assert_called_once()
        assert mock_paste_dispenser.pushpull.call_args.kwargs.get("sync") is False
        mock_paste_dispenser.continue_pushpull.assert_called_once()
        assert (
            mock_paste_dispenser.continue_pushpull.call_args.kwargs.get("sync") is False
        )
        mock_paste_dispenser.sync.assert_called_once_with()

    def test_apply_waits_for_sequence_completion(self, applicator, mock_klipper):
        # Arrange: 単一成分パッド
        polygon = box(0, 0, 2, 3)

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert: pad ごとの中止境界が物理動作完了後になる
        sent = mock_klipper.send_gcode.call_args.args[0]
        assert str(sent).splitlines()[-1] == "M400"

    def test_context_manager_enables_and_disables(
        self, applicator, mock_paste_dispenser
    ):
        # Act
        with applicator:
            pass

        # Assert
        mock_paste_dispenser.enable.assert_called_once()
        mock_paste_dispenser.disable.assert_called_once()


class TestRawRotationLoading:
    """Raw rotation ローディング."""

    def test_load_rotations_uses_rotate_revolutions(
        self, applicator, mock_klipper, mock_paste_dispenser
    ):
        applicator.load_rotations(5.0, 0.5, 0.5)

        mock_paste_dispenser.rotate_revolutions.assert_called_once_with(5.0, 0.5, 0.5)
        sent = mock_klipper.send_gcode.call_args.args[0]
        assert str(sent).splitlines()[-1] == "M400"


class TestTransformApplication:
    """塗布座標変換: board_transform → toolhead_offset → height_plane."""

    def test_height_plane_evaluates_final_toolhead_machine_xy(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        paste_height = 0.5
        board_transform = Shift(x=20.0, y=10.0, z=0.0)
        toolhead_offset = Shift(x=100.0, y=30.0, z=0.0)
        before_height_plane = Compose([board_transform, toolhead_offset])
        transform = Compose([board_transform, toolhead_offset, _machine_height_plane()])
        applicator = PasteApplicator(
            klipper=mock_klipper,
            paste_dispenser=mock_paste_dispenser,
            stage=mock_stage,
            nozzle_diameter=0.34,
            max_fill_speed=2.0,
            max_dispense_rate=5.0,
            dispense_accel=10.0,
            ul_per_mm2=0.05,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            transform=transform,
            paste_height=paste_height,
            lift_height=5.0,
        )

        applicator.apply([box(0.0, 0.0, 5.0, 4.0)], transform=transform)
        assert len(mock_stage.move.call_args_list) >= 2
        down_move = mock_stage.move.call_args_list[1].kwargs
        assert down_move["z"] == pytest.approx(
            paste_height + _machine_surface_z(down_move["x"], down_move["y"])
        )
        board_space_point = before_height_plane.inverse().apply(
            Point3d(down_move["x"], down_move["y"], 0.0)
        )
        board_space_z = _machine_surface_z(board_space_point.x, board_space_point.y)
        assert down_move["z"] != pytest.approx(paste_height + board_space_z)


class TestAutoPasteHeight:
    """paste_height=auto は dispense_mode によらず ul_per_mm2（膜厚 [mm]）を高さに使う。"""

    def _applicator(
        self,
        mock_klipper,
        mock_paste_dispenser,
        mock_stage,
        *,
        nozzle_diameter: float,
        dispense_mode: DispenseMode,
        ul_per_mm2: float = 0.05,
    ) -> PasteApplicator:
        return PasteApplicator(
            klipper=mock_klipper,
            paste_dispenser=mock_paste_dispenser,
            stage=mock_stage,
            nozzle_diameter=nozzle_diameter,
            max_fill_speed=2.0,
            max_dispense_rate=5.0,
            dispense_accel=10.0,
            ul_per_mm2=ul_per_mm2,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_height="auto",
            dispense_mode=dispense_mode,
            lift_height=5.0,
        )

    def _down_z(self, mock_stage) -> float:
        assert len(mock_stage.move.call_args_list) >= 2
        return mock_stage.move.call_args_list[1].kwargs["z"]

    @pytest.mark.parametrize(
        ("dispense_mode", "polygon"),
        [
            ("area", box(0, 0, 5, 4)),
            ("line", box(0, 0, 1, 4)),
            ("dot", box(0, 0, 1, 1)),
        ],
    )
    def test_auto_uses_ul_per_mm2_as_height(
        self,
        mock_klipper,
        mock_paste_dispenser,
        mock_stage,
        dispense_mode: DispenseMode,
        polygon,
    ):
        applicator = self._applicator(
            mock_klipper,
            mock_paste_dispenser,
            mock_stage,
            nozzle_diameter=0.5,
            dispense_mode=dispense_mode,
            ul_per_mm2=0.08,
        )

        applicator.apply([polygon], transform=Identity())
        assert self._down_z(mock_stage) == pytest.approx(0.08)


class TestInitValidation:
    """初期化バリデーション."""

    def test_retraction_accel_factor_must_exceed_one(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        with pytest.raises(ValueError, match="retraction_accel_factor"):
            PasteApplicator(
                klipper=mock_klipper,
                paste_dispenser=mock_paste_dispenser,
                stage=mock_stage,
                nozzle_diameter=0.34,
                max_fill_speed=2.0,
                max_dispense_rate=5.0,
                dispense_accel=1.0,
                ul_per_mm2=0.05,
                retraction=10.0,
                retraction_rate=10.0,
                retraction_accel_factor=0.5,
            )

    @pytest.mark.parametrize(
        ("max_fill_speed", "max_dispense_rate", "match"),
        [
            (0.0, 5.0, "max_fill_speedは正の値"),
            (2.0, 0.0, "max_dispense_rateは正の値"),
        ],
    )
    def test_rate_and_speed_must_be_positive(
        self,
        mock_klipper,
        mock_paste_dispenser,
        mock_stage,
        max_fill_speed,
        max_dispense_rate,
        match,
    ):
        # max_fill_speed / max_dispense_rate が 0 だと _effective_rate=0 →
        # prime_time 計算で ZeroDivisionError になるため、入口で弾く。
        with pytest.raises(ValueError, match=match):
            PasteApplicator(
                klipper=mock_klipper,
                paste_dispenser=mock_paste_dispenser,
                stage=mock_stage,
                nozzle_diameter=0.34,
                max_fill_speed=max_fill_speed,
                max_dispense_rate=max_dispense_rate,
                dispense_accel=1.0,
                ul_per_mm2=0.05,
                retraction=10.0,
                retraction_rate=10.0,
                retraction_accel_factor=2.0,
            )


class TestCalibrate:
    """流量キャリブレーション."""

    def test_calibrate_rotates_and_sends(
        self, applicator, mock_klipper, mock_paste_dispenser
    ):
        # Act
        applicator.calibrate(rotations=10.0, rate=1.0, accel=10.0)

        # Assert
        mock_paste_dispenser.rotate_revolutions.assert_called_once_with(10.0, 1.0, 10.0)
        mock_klipper.send_gcode.assert_called_once()


def _dispenser_config(**overrides: object):
    """pcbasm.config.PasteDispenser を既定値込みで構築する（from_config 用）."""
    import attrs

    from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead

    config = PasteDispenserConfig(
        rotations_per_ul=45.0,
        nozzle_diameter=0.34,
        max_fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        retract_amount=10.0,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        lift_height=2.0,
        ul_per_mm2=0.05,
        dispense_mode="area",
        auto_line_aspect_ratio=1.618,
        prime_extra_delay=0.0,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
    )
    return attrs.evolve(config, **overrides)


class TestFromConfig:
    """from_config（Phase 5 で追加。計画書 webui-phase5.md §1）.

    scripts / webui に重複していた 12 kwargs の構築 boilerplate を classmethod
    に集約する。config の各キーが手動 kwargs 構築と同じ動作（同じ dispenser 呼び出し）に配線されることを
    retract / apply の 1 操作で検証する。
    """

    def _manual_applicator(self, config, mocker: MockerFixture):
        klipper = mocker.Mock()
        dispenser = mocker.Mock()
        dispenser.enable.return_value = gcode.GCode()
        dispenser.disable.return_value = gcode.GCode()
        dispenser.pushpull.return_value = gcode.GCode()
        dispenser.continue_pushpull.return_value = gcode.GCode()
        dispenser.sync.return_value = gcode.GCode()
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.move.return_value = gcode.GCode()
        stage.to_gcode.return_value = gcode.GCode()
        applicator = PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_diameter=config.nozzle_diameter,
            max_fill_speed=config.max_fill_speed,
            max_dispense_rate=config.max_dispense_rate,
            dispense_accel=config.dispense_accel,
            ul_per_mm2=config.ul_per_mm2,
            retraction=config.retract_amount,
            retraction_rate=config.retract_rate,
            retraction_accel_factor=config.retract_accel_factor,
            paste_height=config.paste_height,
            lift_height=config.lift_height,
            dispense_mode=config.dispense_mode,
            auto_line_aspect_ratio=config.auto_line_aspect_ratio,
            prime_extra_delay=config.prime_extra_delay,
            bead_width_factor=config.bead_width_factor,
            overlap=config.overlap,
            boundary_margin=config.boundary_margin,
        )
        return applicator, klipper, dispenser

    def _config_applicator(self, config, mocker: MockerFixture):
        klipper = mocker.Mock()
        dispenser = mocker.Mock()
        dispenser.enable.return_value = gcode.GCode()
        dispenser.disable.return_value = gcode.GCode()
        dispenser.pushpull.return_value = gcode.GCode()
        dispenser.continue_pushpull.return_value = gcode.GCode()
        dispenser.sync.return_value = gcode.GCode()
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.move.return_value = gcode.GCode()
        stage.to_gcode.return_value = gcode.GCode()
        applicator = PasteApplicator.from_config(klipper, dispenser, stage, config)
        return applicator, klipper, dispenser

    def test_retract_matches_manual_construction(self, mocker: MockerFixture):
        """Retract の dispenser 呼び出し（量・レート・加速度）が手動構築と一致."""
        config = _dispenser_config(
            retract_amount=8.0, retract_rate=4.0, retract_accel_factor=3.0
        )
        manual, _, manual_dispenser = self._manual_applicator(config, mocker)
        via_config, config_klipper, config_dispenser = self._config_applicator(
            config, mocker
        )

        manual.retract()
        via_config.retract()

        assert (
            config_dispenser.pushpull.call_args == manual_dispenser.pushpull.call_args
        )
        config_klipper.send_gcode.assert_called_once()

    def test_apply_matches_manual_construction(self, mocker: MockerFixture):
        """Apply の吐出列（ul_per_mm2 / フィル経路パラメータ）が手動構築と一致."""
        config = _dispenser_config(ul_per_mm2=0.08)
        polygon = box(0, 0, 5, 4)
        manual, manual_klipper, manual_dispenser = self._manual_applicator(
            config, mocker
        )
        via_config, config_klipper, config_dispenser = self._config_applicator(
            config, mocker
        )

        manual.apply([polygon], transform=Identity())
        via_config.apply([polygon], transform=Identity())
        assert (
            config_dispenser.pushpull.call_args_list
            == manual_dispenser.pushpull.call_args_list
        )
        assert (
            config_klipper.send_gcode.call_count == manual_klipper.send_gcode.call_count
        )

    def test_deposit_uses_configured_lift_height(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        config = _dispenser_config(paste_height=0.5, lift_height=4.0)
        applicator = PasteApplicator.from_config(
            mock_klipper, mock_paste_dispenser, mock_stage, config
        )

        applicator.deposit_at(Point2d(1.0, 2.0), amount=0.1, transform=Identity())
        assert [call.kwargs["z"] for call in mock_stage.move.call_args_list] == [
            4.5,
            0.5,
            4.5,
        ]

    def test_invalid_retract_accel_factor_in_config_raises(self, mocker: MockerFixture):
        """Config の retract_accel_factor <= 1.0 は __init__ の検証で ValueError."""
        config = _dispenser_config(retract_accel_factor=0.5)
        klipper = mocker.Mock()
        dispenser = mocker.Mock()
        stage = mocker.Mock()

        with pytest.raises(ValueError, match="retraction_accel_factor"):
            PasteApplicator.from_config(klipper, dispenser, stage, config)


class TestDrawLine:
    """公開 draw_line（キャリブ用の 1 本線塗布プリミティブ）.

    apply のポリゴン経路生成を通さず ``[start, end]`` を直接 1 本の
    FillSequence として送信する。Z 補正・吐出同期は apply の塗布と同一機構。
    """

    def test_sends_single_blocking_gcode(self, applicator, mock_klipper):
        # 1 本の線 → send_gcode 1 回、末尾は M400（動作完了待ち）。
        applicator.draw_line(Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=2.0)

        assert mock_klipper.send_gcode.call_count == 1
        sent = mock_klipper.send_gcode.call_args.args[0]
        assert str(sent).splitlines()[-1] == "M400"

    def test_dispense_amount_is_retraction_plus_amount(
        self, applicator, mock_paste_dispenser
    ):
        # extra_amount=0（prime_extra_delay 既定 0）→ 塗布吐出量 = retraction + amount。
        retraction = 10.0
        applicator.draw_line(Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=2.0)

        amounts = _dispense_amounts(mock_paste_dispenser)
        assert len(amounts) == 1
        assert amounts[0] == pytest.approx(retraction + 2.0)

    def test_returns_effective_fill_speed(self, applicator):
        # L=10, amount=2.0, max_fill_speed=2.0, max_dispense_rate=5.0:
        # r_desired = 2*2/10 = 0.4 ≤ 5 → 非 cap。速度 = max_fill_speed = 2.0。
        speed = applicator.draw_line(Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=2.0)

        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(2.0)

    def test_explicit_paste_height_is_used_for_descent(self, applicator, mock_stage):
        # paste_height を明示すると下降 Z（2 番目の move）にそのまま使われる。
        applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=2.0, paste_height=0.7
        )

        down_move = mock_stage.move.call_args_list[1].kwargs
        assert down_move["z"] == pytest.approx(0.7)

    def test_rate_cap_inf_disables_capping(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        # cap 無効だと低 max_dispense_rate でも頭打ちされず、移動速度のみで律速。
        # max_dispense_rate=0.1, max_fill_speed=2.0, L=10, amount=2.0:
        # r_desired = 2*2/10 = 0.4。cap=None なら 0.1 に頭打ち→減速、cap=inf なら 0.4。
        applicator = PasteApplicator(
            klipper=mock_klipper,
            paste_dispenser=mock_paste_dispenser,
            stage=mock_stage,
            nozzle_diameter=0.34,
            max_fill_speed=2.0,
            max_dispense_rate=0.1,
            dispense_accel=10.0,
            ul_per_mm2=0.05,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_height=0.5,
            lift_height=5.0,
        )

        speed = applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=2.0, rate_cap=math.inf
        )

        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(2.0)
        assert _dispense_amounts(mock_paste_dispenser)[0] == pytest.approx(10.0 + 2.0)

    def test_max_fill_speed_override_exceeds_instance_default(self, applicator):
        # ③ 速度スイープの肝: per-line の max_fill_speed 上書きで、インスタンス既定
        # （2.0）を超える速度を実際に達成できる（頭打ちさせない）。
        # max_fill_speed=8.0, rate_cap=inf, L=10, amount=2.0:
        # r_desired = 2*8/10 = 1.6、cap=inf → 非 cap。速度 = 8.0（既定 2.0 ではない）。
        speed = applicator.draw_line(
            Point2d(0.0, 0.0),
            Point2d(10.0, 0.0),
            amount=2.0,
            max_fill_speed=8.0,
            rate_cap=math.inf,
        )

        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(8.0)

    def test_auto_height_uses_ul_per_mm2(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        # paste_height="auto" の draw_line は ul_per_mm2（膜厚 [mm]）を下降 Z に使う。
        applicator = PasteApplicator(
            klipper=mock_klipper,
            paste_dispenser=mock_paste_dispenser,
            stage=mock_stage,
            nozzle_diameter=0.5,
            max_fill_speed=2.0,
            max_dispense_rate=5.0,
            dispense_accel=10.0,
            ul_per_mm2=0.05,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_height="auto",
            lift_height=5.0,
        )

        applicator.draw_line(Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount=1.5)

        down_move = mock_stage.move.call_args_list[1].kwargs
        assert down_move["z"] == pytest.approx(0.05)


class TestDepositAt:
    """公開 deposit_at（初回パージ用の単点塗布プリミティブ）."""

    def test_sends_single_blocking_gcode(self, applicator, mock_klipper):
        applicator.deposit_at(
            Point2d(4.0, 5.0), amount=0.2, paste_height=0.6, transform=Identity()
        )
        assert mock_klipper.send_gcode.call_count == 1
        sent = mock_klipper.send_gcode.call_args.args[0]
        assert str(sent).splitlines()[-1] == "M400"

    def test_uses_single_point_without_stage_path_motion(self, applicator, mock_stage):
        point = Point2d(4.0, 5.0)

        applicator.deposit_at(point, amount=0.2, paste_height=0.6, transform=Identity())
        first_move = mock_stage.move.call_args_list[0].kwargs
        down_move = mock_stage.move.call_args_list[1].kwargs
        assert first_move["x"] == pytest.approx(point.x)
        assert first_move["y"] == pytest.approx(point.y)
        assert down_move["x"] == pytest.approx(point.x)
        assert down_move["y"] == pytest.approx(point.y)
        assert down_move["z"] == pytest.approx(0.6)
        mock_stage.to_gcode.assert_not_called()

    def test_dispense_amount_is_retraction_plus_amount(
        self, applicator, mock_paste_dispenser
    ):
        retraction = 10.0

        applicator.deposit_at(
            Point2d(4.0, 5.0), amount=0.2, paste_height=0.6, transform=Identity()
        )
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert len(amounts) == 1
        assert amounts[0] == pytest.approx(retraction + 0.2)


class TestPerPadOverride:
    """Apply() の per-pad override 引数（None は __init__ 値を使う）.

    pad ごとに塗布設定を変えるための上書き。各経路（total_amount / FillSequence /
    build_paste_fill_path）へ実効値が伝わることを代表的に 検証する。
    """

    def test_ul_per_mm2_override_changes_total(self, applicator, mock_paste_dispenser):
        # Arrange: init は ul_per_mm2=0.05。override で 0.08 に
        polygon = box(0, 0, 5, 4)  # area = 20 mm^2
        retraction = 10.0

        # Act
        applicator.apply([polygon], ul_per_mm2=0.08, transform=Identity())
        # Assert: total_amount = area * 上書き値
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert amounts[0] == pytest.approx(retraction + 20 * 0.08)

    def test_no_override_uses_init_value(self, applicator, mock_paste_dispenser):
        # Arrange: 後方互換 — override 無しは init の ul_per_mm2=0.05
        polygon = box(0, 0, 5, 4)
        retraction = 10.0

        # Act
        applicator.apply([polygon], transform=Identity())
        # Assert
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert amounts[0] == pytest.approx(retraction + 20 * 0.05)

    def test_prime_extra_delay_override_adds_extra_amount(
        self, applicator, mock_paste_dispenser
    ):
        # Arrange: delay=0 の基準量を取る
        polygon = box(0, 0, 5, 4)
        applicator.apply([polygon], transform=Identity())
        base_amount = _dispense_amounts(mock_paste_dispenser)[0]
        mock_paste_dispenser.pushpull.reset_mock()

        # Act: prime_extra_delay>0 で extra_amount = 実効レート*delay が加算
        applicator.apply([polygon], prime_extra_delay=1.0, transform=Identity())
        # Assert
        delayed_amount = _dispense_amounts(mock_paste_dispenser)[0]
        assert delayed_amount > base_amount

    def test_boundary_margin_override_changes_fill_path(self, applicator, mock_klipper):
        # Arrange: 外周マージン 0 の塗布 GCode を取る
        polygon = box(0, 0, 10, 10)
        applicator.apply([polygon], boundary_margin=0.0, transform=Identity())
        without_margin = list(mock_klipper.send_gcode.call_args_list)
        mock_klipper.send_gcode.reset_mock()

        # Act: boundary_margin を上書きすると build_paste_fill_path の経路が変わる
        applicator.apply([polygon], boundary_margin=2.0, transform=Identity())
        with_margin = list(mock_klipper.send_gcode.call_args_list)

        # Assert: 上書きが build 経路に伝わり、送信される塗布パスが変化する
        assert without_margin != with_margin
