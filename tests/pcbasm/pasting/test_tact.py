"""塗布タクトタイム見積りの公開契約テスト.

``estimate_paste_tact`` は装置に一切触れず、pad ごとの ``FillPlan`` と
``FillSequence`` の吐出時間モデル・台形速度プロファイルだけで塗布ループの所要時間を
積算する。ジョブ開始前（フォーム表示時）に呼べることが契約なので、Klipper / ステージを
参照しない純関数として検証する。
"""

from __future__ import annotations

import math

import attrs
import pytest
from shapely import Polygon

from pcbasm.config import PasteDispenser, Tact, Toolhead
from pcbasm.geometry import Path, Point2d
from pcbasm.pasting.fill_path import FillPlan
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.params import DispenseSettings, PasteParams
from pcbasm.pasting.tact import estimate_paste_tact
from pcbasm.pcb import Layer, Pad

_TACT = Tact(
    travel_speed=10.0, travel_accel=50.0, z_speed=5.0, z_accel=10.0, setup_sec=300.0
)


def _config(**overrides) -> PasteDispenser:
    """実機 machine.toml に近い値の PasteDispenser（塗布は吐出律速）."""
    values = {
        "rotations_per_ul": 20.0,
        "nozzle_diameter": 0.3,
        "max_fill_speed": 2.0,
        "max_dispense_rate": 0.05,
        "dispense_accel": 0.5,
        "retract_amount": 0.03,
        "retract_rate": 0.1,
        "retract_accel_factor": 5.0,
        "toolhead": Toolhead(x=0.0, y=0.0),
        "paste_height": 0.2,
        "ul_per_mm2": 0.2,
        "lift_height": 1.0,
    }
    values.update(overrides)
    return PasteDispenser(**values)


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    hw, hh = w / 2.0, h / 2.0
    return Polygon(
        [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw, cy + hh)]
    )


def _pad(designator: str, *, center: Point2d, size: float = 0.5) -> Pad:
    """正方形 pad。既定の一辺 0.5 は Auto 判定が点塗布になる（< nozzle 0.3 * 3.0）."""
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="",
        layer=Layer.TOP,
        polygon=_rect(center.x, center.y, size, size),
    )


def _targets(config: PasteDispenser, *pads: Pad) -> list[tuple[Pad, PasteParams]]:
    params = PasteParams.from_config(config)
    return [(pad, params) for pad in pads]


class TestEstimatePasteTact:
    """塗布ループの積算と、その内訳の公開契約."""

    def test_no_pads_counts_only_setup(self):
        estimate = estimate_paste_tact((), config=_config(), tact=_TACT)

        assert estimate.pad_count == 0
        assert estimate.dispense_sec == 0.0
        assert estimate.setup_sec == _TACT.setup_sec
        assert estimate.total_sec == _TACT.setup_sec

    def test_pad_count_reports_number_of_pads(self):
        config = _config()
        # 一辺 1.0 は面塗布になる（成分が複数点でも 1 pad は 1 枚と数える）
        pads = [
            _pad("R1", center=Point2d(0.0, 0.0), size=1.0),
            _pad("R2", center=Point2d(5.0, 0.0), size=1.0),
            _pad("R3", center=Point2d(10.0, 0.0), size=1.0),
        ]

        estimate = estimate_paste_tact(
            _targets(config, *pads), config=config, tact=_TACT
        )

        assert estimate.pad_count == 3

    def test_single_dot_pad_sums_travel_descent_dispense_and_lift(self):
        """点塗布 1 枚の内訳を、移動・下降・吐出・上昇の実数で固定する.

        移動は停止 → 加速 → 巡航 → 減速 → 停止（各動作の前後で必ず停止する）、 下降と上昇は純 Z 移動なので Z
        軸の速度・加速度で走る。
        """
        config = _config()
        settings = DispenseSettings.from_config(config)
        pad = _pad("R1", center=Point2d(0.0, 0.0))
        params = PasteParams.from_config(config)
        plan = FillPlan.for_pad(pad.polygon, config=config, params=params)
        (dot,) = plan.paths[0]
        sequence = FillSequence(
            path=Path([dot.to3d(params.paste_height_mm)]),
            total_amount_ul=pad.area * params.ul_per_mm2,
            settings=settings,
            prime_extra_delay=params.prime_extra_delay,
        )
        # XY 10 mm: 加減速で使う距離 v^2/a = 100/50 = 2.0 < 10 → 2*10/50 + 8/10
        travel = 2 * 10.0 / 50.0 + (10.0 - 2.0) / 10.0
        # Z 1.0 mm: v^2/a = 25/10 = 2.5 ≥ 1.0 → 三角プロファイル 2*sqrt(d/a)
        lift = 2 * math.sqrt(1.0 / 10.0)
        # リトラクション 0.03 uL: a = 5.0 * 0.1^2 / 0.03、v^2/a = 0.006 < 0.03
        retract_accel = 5.0 * 0.1**2 / 0.03
        retract = 2 * 0.1 / retract_accel + (0.03 - 0.1**2 / retract_accel) / 0.1

        estimate = estimate_paste_tact(
            _targets(config, pad),
            config=config,
            tact=_TACT,
            start=Point2d(dot.x - 10.0, dot.y),
        )

        assert lift > retract  # 上昇がリトラクションを覆う（同時に始まるため）
        assert estimate.dispense_sec == pytest.approx(
            travel + lift + sequence.dispense_duration + lift
        )

    def test_slower_z_axis_lengthens_descent_and_lift(self):
        """下降・上昇は Z 軸の上限で走る（XY の値を使うと短く出てしまう）."""
        config = _config()
        pads = _targets(config, _pad("R1", center=Point2d(0.0, 0.0)))
        quick = attrs.evolve(_TACT, z_speed=50.0, z_accel=250.0)

        assert (
            estimate_paste_tact(pads, config=config, tact=quick).dispense_sec
            < estimate_paste_tact(pads, config=config, tact=_TACT).dispense_sec
        )

    def test_pad_without_fill_path_adds_no_time(self):
        """経路の作れない pad（空ポリゴン）は枚数に数えるが時間は積まない."""
        config = _config()
        empty = Pad(
            designator="R1",
            pad_number="1",
            net_name="",
            layer=Layer.TOP,
            polygon=Polygon(),
        )

        estimate = estimate_paste_tact(
            _targets(config, empty), config=config, tact=_TACT
        )

        assert estimate.pad_count == 1
        assert estimate.dispense_sec == 0.0

    def test_farther_pad_adds_travel_time(self):
        config = _config()
        near = _pad("R1", center=Point2d(1.0, 0.0))
        far = _pad("R1", center=Point2d(100.0, 0.0))

        near_estimate = estimate_paste_tact(
            _targets(config, near), config=config, tact=_TACT
        )
        far_estimate = estimate_paste_tact(
            _targets(config, far), config=config, tact=_TACT
        )

        assert far_estimate.dispense_sec > near_estimate.dispense_sec

    def test_faster_travel_shortens_the_estimate(self):
        config = _config()
        pads = _targets(config, _pad("R1", center=Point2d(50.0, 50.0)))
        slow = Tact(travel_speed=5.0, travel_accel=50.0, setup_sec=0.0)
        fast = Tact(travel_speed=50.0, travel_accel=50.0, setup_sec=0.0)

        assert (
            estimate_paste_tact(pads, config=config, tact=fast).dispense_sec
            < estimate_paste_tact(pads, config=config, tact=slow).dispense_sec
        )

    def test_more_paste_takes_longer(self):
        thin = _config(ul_per_mm2=0.1)
        thick = _config(ul_per_mm2=0.4)
        pad = _pad("R1", center=Point2d(0.0, 0.0))

        assert (
            estimate_paste_tact(
                _targets(thick, pad), config=thick, tact=_TACT
            ).dispense_sec
            > estimate_paste_tact(
                _targets(thin, pad), config=thin, tact=_TACT
            ).dispense_sec
        )

    def test_setup_is_added_to_the_total(self):
        config = _config()
        pads = _targets(config, _pad("R1", center=Point2d(0.0, 0.0)))
        tact = Tact(travel_speed=10.0, travel_accel=50.0, setup_sec=120.0)

        estimate = estimate_paste_tact(pads, config=config, tact=tact)

        assert estimate.total_sec == pytest.approx(120.0 + estimate.dispense_sec)
