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

import pytest
from pytest_mock import MockerFixture
from shapely import Polygon, box

from pcbasm import gcode
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


@pytest.fixture
def mock_klipper(mocker: MockerFixture):
    return mocker.Mock()


@pytest.fixture
def mock_paste_dispenser(mocker: MockerFixture):
    dispenser = mocker.Mock()
    dispenser.enable.return_value = gcode.GCode()
    dispenser.disable.return_value = gcode.GCode()
    dispenser.pushpull.return_value = gcode.GCode()
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
        fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        ul_per_mm2=0.05,
        retraction=10.0,
        retraction_rate=10.0,
        retraction_accel_factor=2.0,
        paste_height=0.5,
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
        applicator.apply([polygon])

        # Assert: 成分 1 つ → send_gcode 1 回
        assert mock_klipper.send_gcode.call_count == 1

    def test_total_amount_is_area_based(self, applicator, mock_paste_dispenser):
        # Arrange
        polygon = box(0, 0, 5, 4)  # area = 20 mm^2
        retraction = 10.0
        ul_per_mm2 = 0.05
        expected_total = polygon.area * ul_per_mm2

        # Act
        applicator.apply([polygon])

        # Assert: 塗布吐出量 = retraction + total_amount（extra_amount=0）
        amounts = _dispense_amounts(mock_paste_dispenser)
        assert len(amounts) == 1
        assert amounts[0] == pytest.approx(retraction + expected_total)

    def test_apply_calls_send_gcode_per_polygon(self, applicator, mock_klipper):
        # Arrange: 単一成分パッド 2 つ
        polygons = [box(0, 0, 2, 3), box(5, 5, 8, 9)]

        # Act
        applicator.apply(polygons)

        # Assert: 各パッド単一成分 → 合計 2 回
        assert mock_klipper.send_gcode.call_count == 2


class TestMultiComponentPad:
    """複数成分パッド（凹形）: FillSequence N 本・各 total_amount = area*ul/N."""

    def test_send_gcode_once_per_component(self, applicator, mock_klipper):
        # Arrange: 既定ノズル径で 2 成分に割れる細首ダンベル
        polygon = _DUMBBELL_NECK_03

        # Act
        applicator.apply([polygon])

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
        applicator.apply([polygon])

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
        applicator.apply([polygon])

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
        applicator.apply([polygon])

        # Assert: 1 本も送信しない
        mock_klipper.send_gcode.assert_not_called()

    def test_invalid_polygon_skips(self, applicator, mock_klipper):
        # Arrange: 自己交差する不正ポリゴン → []
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
        assert not invalid.is_valid  # 前提: 不正形状

        # Act
        applicator.apply([invalid])

        # Assert
        mock_klipper.send_gcode.assert_not_called()

    def test_empty_polygon_list_skips(self, applicator, mock_klipper):
        # Act
        applicator.apply([])

        # Assert
        mock_klipper.send_gcode.assert_not_called()


class TestDispenseProtocol:
    """吐出プロトコル（プライム+吐出の連続動作・sync・コンテキスト）."""

    def test_dispense_uses_sync_false(self, applicator, mock_paste_dispenser):
        # Arrange: 単一成分パッド
        polygon = box(0, 0, 2, 3)

        # Act
        applicator.apply([polygon])

        # Assert: プライム+吐出（sync=False）、リトラクション（sync=True）の 2 回
        calls = mock_paste_dispenser.pushpull.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs.get("sync") is False
        assert "sync" not in calls[1].kwargs or calls[1].kwargs["sync"] is True

    def test_context_manager_enables_and_disables(
        self, applicator, mock_paste_dispenser
    ):
        # Act
        with applicator:
            pass

        # Assert
        mock_paste_dispenser.enable.assert_called_once()
        mock_paste_dispenser.disable.assert_called_once()


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
                fill_speed=2.0,
                max_dispense_rate=5.0,
                dispense_accel=1.0,
                ul_per_mm2=0.05,
                retraction=10.0,
                retraction_rate=10.0,
                retraction_accel_factor=0.5,
            )

    @pytest.mark.parametrize(
        ("fill_speed", "max_dispense_rate", "match"),
        [
            (0.0, 5.0, "fill_speedは正の値"),
            (2.0, 0.0, "max_dispense_rateは正の値"),
        ],
    )
    def test_rate_and_speed_must_be_positive(
        self,
        mock_klipper,
        mock_paste_dispenser,
        mock_stage,
        fill_speed,
        max_dispense_rate,
        match,
    ):
        # fill_speed / max_dispense_rate が 0 だと _effective_rate=0 →
        # prime_time 計算で ZeroDivisionError になるため、入口で弾く。
        with pytest.raises(ValueError, match=match):
            PasteApplicator(
                klipper=mock_klipper,
                paste_dispenser=mock_paste_dispenser,
                stage=mock_stage,
                nozzle_diameter=0.34,
                fill_speed=fill_speed,
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


def _dispenser_config(**overrides: float):
    """pcbasm.config.PasteDispenser を既定値込みで構築する（from_config 用）."""
    import attrs

    from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead

    config = PasteDispenserConfig(
        rotations_per_ul=45.0,
        nozzle_diameter=0.34,
        fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        retract_amount=10.0,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        ul_per_mm2=0.05,
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
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.move.return_value = gcode.GCode()
        stage.to_gcode.return_value = gcode.GCode()
        applicator = PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_diameter=config.nozzle_diameter,
            fill_speed=config.fill_speed,
            max_dispense_rate=config.max_dispense_rate,
            dispense_accel=config.dispense_accel,
            ul_per_mm2=config.ul_per_mm2,
            retraction=config.retract_amount,
            retraction_rate=config.retract_rate,
            retraction_accel_factor=config.retract_accel_factor,
            paste_height=config.paste_height,
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

        manual.apply([polygon])
        via_config.apply([polygon])

        assert (
            config_dispenser.pushpull.call_args_list
            == manual_dispenser.pushpull.call_args_list
        )
        assert (
            config_klipper.send_gcode.call_count == manual_klipper.send_gcode.call_count
        )

    def test_invalid_retract_accel_factor_in_config_raises(self, mocker: MockerFixture):
        """Config の retract_accel_factor <= 1.0 は __init__ の検証で ValueError."""
        config = _dispenser_config(retract_accel_factor=0.5)
        klipper = mocker.Mock()
        dispenser = mocker.Mock()
        stage = mocker.Mock()

        with pytest.raises(ValueError, match="retraction_accel_factor"):
            PasteApplicator.from_config(klipper, dispenser, stage, config)
