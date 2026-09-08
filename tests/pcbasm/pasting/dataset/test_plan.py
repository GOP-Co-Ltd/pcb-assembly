"""銅板のセル格子・吐出量スイープ・多視点計画（純ロジック層）の公開契約.

装置・カメラ・PCB を一切使わない純関数層なので、fake も実データも要らない。

検証する契約は次のとおり。

- 格子ピッチと行優先採番
- パージセル除外
- 使用セルが板全体へ散ること
- 量割り当ての決定性と multiset 一致
- blank セルの混在
- crop 寸法とセルピッチの関係
- 装置を動かす前の収まり検証（frame / 撮影・塗布の可動域 / 最小回転数）
- 多視点の角度
- 配置不能でも描ける診断用 preview と派生カウント
"""

import math
from collections import Counter

import pytest

from pcbasm.geometry import Shift
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dataset.plan import (
    MIN_COMMANDED_ROTATIONS,
    DotGridPlan,
    DotGridSpec,
    plan_dot_grid,
    plan_views,
    preview_dot_grid,
    validate_capture_reach,
    validate_crop_in_frame,
    validate_dispense_reach,
    validate_min_rotations,
)

# 板 20x20 / 余白 2 → 有効領域 Rect(2, 2, 16, 16)。セル 2 + 間隔 1 = ピッチ 3 で
# 5 行 5 列（x, y = 2, 5, 8, 11, 14）が入り、左上のパージセルが 1 枚を潰す。
PITCH = 3.0
USABLE = Rect(2.0, 2.0, 16.0, 16.0)


def _spec(**overrides: float | int) -> DotGridSpec:
    values: dict[str, float | int] = {
        "plate_width_mm": 20.0,
        "plate_height_mm": 20.0,
        "edge_margin_mm": 2.0,
        "cell_size_mm": 2.0,
        "cell_gap_mm": 1.0,
        "crop_size_mm": 2.0,
        "purge_cell_size_mm": 2.0,
        "volume_min_ul": 0.05,
        "volume_max_ul": 0.2,
        "volume_divisions": 5,
        "samples_per_volume": 3,
        "blank_count": 4,
        "shuffle_seed": 20260908,
    }
    values.update(overrides)
    return DotGridSpec(**values)  # type: ignore[arg-type]


def _planned(**overrides: float | int) -> DotGridPlan:
    plan, error = plan_dot_grid(_spec(**overrides))

    assert error is None, error
    assert plan is not None
    return plan


def _expanded(rect: Rect, margin: float) -> Rect:
    return Rect(
        x=rect.x - margin,
        y=rect.y - margin,
        width=rect.width + 2.0 * margin,
        height=rect.height + 2.0 * margin,
    )


class TestDotGridSpec:
    """セル格子・量スイープ設定の None 返却バリデーションと派生量."""

    def test_valid_spec_returns_none(self):
        assert _spec().validate() is None

    def test_sample_count_is_divisions_times_samples_per_volume(self):
        assert _spec(volume_divisions=5, samples_per_volume=3).sample_count == 15
        assert _spec(volume_divisions=1, samples_per_volume=1).sample_count == 1

    def test_target_count_adds_the_blank_cells_to_the_dispensed_samples(self):
        spec = _spec(volume_divisions=5, samples_per_volume=3, blank_count=4)

        assert spec.target_count == 19

    def test_zero_blank_count_leaves_the_target_count_at_the_sample_count(self):
        spec = _spec(volume_divisions=5, samples_per_volume=3, blank_count=0)

        assert spec.validate() is None
        assert spec.target_count == spec.sample_count

    def test_volumes_are_equally_spaced_between_min_and_max(self):
        volumes = _spec(
            volume_min_ul=0.05, volume_max_ul=0.2, volume_divisions=4
        ).volumes_ul

        assert volumes == pytest.approx((0.05, 0.1, 0.15, 0.2))

    def test_single_division_uses_minimum_volume_only(self):
        volumes = _spec(
            volume_min_ul=0.05, volume_max_ul=0.2, volume_divisions=1
        ).volumes_ul

        assert volumes == pytest.approx((0.05,))

    def test_equal_min_and_max_degenerates_to_one_volume_value(self):
        spec = _spec(volume_min_ul=0.1, volume_max_ul=0.1, volume_divisions=5)

        assert spec.validate() is None
        assert list(spec.volumes_ul) == pytest.approx([0.1] * len(spec.volumes_ul))

    def test_crop_size_may_equal_the_cell_pitch(self):
        spec = _spec(cell_size_mm=2.0, cell_gap_mm=1.0, crop_size_mm=3.0)

        assert spec.validate() is None

    def test_crop_size_above_the_cell_pitch_is_rejected(self):
        spec = _spec(cell_size_mm=2.0, cell_gap_mm=1.0, crop_size_mm=3.001)

        error = spec.validate()

        assert error is not None
        assert "3.001" in error

    def test_crop_size_smaller_than_the_cell_is_allowed(self):
        assert _spec(crop_size_mm=1.0).validate() is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("plate_width_mm", 0.0),
            ("plate_width_mm", float("nan")),
            ("plate_height_mm", -1.0),
            ("edge_margin_mm", -1.0),
            ("cell_size_mm", 0.0),
            ("cell_gap_mm", -1.0),
            ("crop_size_mm", 0.0),
            ("crop_size_mm", float("inf")),
            ("purge_cell_size_mm", 0.0),
            ("volume_min_ul", 0.0),
            ("volume_max_ul", 0.01),
            ("volume_divisions", 0),
            ("samples_per_volume", 0),
            ("blank_count", -1),
        ],
    )
    def test_rejects_invalid_field_and_names_the_offending_value(
        self, field: str, value: float | int
    ):
        error = _spec(**{field: value}).validate()

        assert error is not None
        assert repr(value) in error


class TestPlanDotGridLayout:
    """有効領域・格子ピッチ・行優先採番・パージセル除外の幾何契約."""

    def test_usable_area_is_plate_minus_edge_margin_on_all_sides(self):
        plan = _planned()

        assert plan.usable_area == USABLE

    def test_purge_cell_sits_at_top_left_of_usable_area(self):
        plan = _planned(purge_cell_size_mm=4.0)

        assert plan.purge_cell == Rect(USABLE.x, USABLE.y, 4.0, 4.0)
        assert plan.purge_center.x == pytest.approx(USABLE.x + 2.0)
        assert plan.purge_center.y == pytest.approx(USABLE.y + 2.0)

    def test_capacity_counts_grid_cells_left_after_purge_exclusion(self):
        # 5 行 5 列 = 25 セル。パージ 2 mm 角 + 間隔 1 mm は左上 1 枚だけを潰す。
        assert _planned().capacity == 24

    def test_larger_purge_cell_excludes_every_intersecting_grid_cell(self):
        # パージ 4 mm 角（セル 2 mm 角と別寸法）を間隔 1 mm 広げた矩形は
        # 左上 2 行 2 列の 4 セルと重なる。
        assert _planned(purge_cell_size_mm=4.0).capacity == 21

    @pytest.mark.parametrize("purge_cell_size_mm", [1.0, 2.0, 3.0, 4.0, 5.5])
    def test_no_remaining_target_touches_the_expanded_purge_cell(
        self, purge_cell_size_mm: float
    ):
        # パージが大きいほど残るセルが減るので、収まる小さめのスイープで見る
        plan = _planned(
            purge_cell_size_mm=purge_cell_size_mm,
            volume_divisions=3,
            samples_per_volume=2,
            blank_count=2,
        )
        keepout = _expanded(plan.purge_cell, plan.spec.cell_gap_mm)

        assert not [
            target for target in plan.targets if target.rect.intersects(keepout)
        ]

    def test_targets_are_numbered_from_one_in_row_major_order(self):
        plan = _planned()

        assert [target.index for target in plan.targets] == list(
            range(1, plan.spec.target_count + 1)
        )
        positions = [(target.rect.y, target.rect.x) for target in plan.targets]
        assert positions == sorted(positions)

    def test_targets_use_cell_size_and_pitch_inside_the_usable_area(self):
        plan = _planned()

        for target in plan.targets:
            assert target.rect.width == pytest.approx(plan.spec.cell_size_mm)
            assert target.rect.height == pytest.approx(plan.spec.cell_size_mm)
            assert USABLE.contains(target.rect)
            column = (target.rect.x - USABLE.x) / PITCH
            row = (target.rect.y - USABLE.y) / PITCH
            assert column == pytest.approx(round(column), abs=1e-9)
            assert row == pytest.approx(round(row), abs=1e-9)

    def test_target_center_is_the_center_of_its_rect(self):
        for target in _planned().targets:
            assert target.center.x == pytest.approx(
                target.rect.x + target.rect.width / 2
            )
            assert target.center.y == pytest.approx(
                target.rect.y + target.rect.height / 2
            )

    def test_plans_exactly_target_count_cells_out_of_the_capacity(self):
        plan = _planned(volume_divisions=5, samples_per_volume=3, blank_count=4)

        assert len(plan.targets) == 19
        assert len(plan.cells) == 15
        assert len(plan.blanks) == 4
        assert plan.capacity == 24

    @pytest.mark.parametrize("shuffle_seed", [1, 1234, 20260908])
    def test_used_cells_are_spread_over_the_whole_plate(self, shuffle_seed: int):
        # 既定の 40x40 板は格子 12x12（パージ除外後 143 セル）に対し撮影対象 19 セル。
        # 先頭から詰めると上端 2 行に固まるので、複数行・複数列へ散ることをピンする。
        plan = _planned(
            plate_width_mm=40.0, plate_height_mm=40.0, shuffle_seed=shuffle_seed
        )
        rows = {round((target.rect.y - USABLE.y) / PITCH) for target in plan.targets}
        columns = {round((target.rect.x - USABLE.x) / PITCH) for target in plan.targets}

        assert plan.capacity == 143
        assert len(plan.targets) == 19
        assert len(rows) >= 6
        assert len(columns) >= 6
        # 板の上端付近だけでなく下半分にも入っていること
        assert max(rows) >= 6

    def test_spec_is_kept_on_the_plan(self):
        spec = _spec()

        plan, _ = plan_dot_grid(spec)

        assert plan is not None
        assert plan.spec == spec


class TestPlanDotGridVolumeAssignment:
    """シード付きシャッフルによる量割り当ての決定性と multiset 保存."""

    def test_assigned_volumes_are_the_sweep_repeated_per_sample_count(self):
        plan = _planned(volume_divisions=5, samples_per_volume=3)

        expected = Counter(plan.spec.volumes_ul * 3)
        assert Counter(cell.commanded_volume_ul for cell in plan.cells) == expected

    def test_volume_index_points_at_the_assigned_volume(self):
        plan = _planned()
        volumes = plan.spec.volumes_ul

        for cell in plan.cells:
            assert 0 <= cell.volume_index < len(volumes)
            assert volumes[cell.volume_index] == pytest.approx(cell.commanded_volume_ul)

    def test_same_seed_reproduces_the_same_placement(self):
        first = _planned(shuffle_seed=1234)
        second = _planned(shuffle_seed=1234)

        assert [
            (cell.index, cell.rect, cell.commanded_volume_ul, cell.volume_index)
            for cell in first.cells
        ] == [
            (cell.index, cell.rect, cell.commanded_volume_ul, cell.volume_index)
            for cell in second.cells
        ]

    def test_different_seed_changes_the_placement(self):
        first = _planned(shuffle_seed=1234)
        other = _planned(shuffle_seed=5678)

        assert [(cell.index, cell.commanded_volume_ul) for cell in first.cells] != [
            (cell.index, cell.commanded_volume_ul) for cell in other.cells
        ]

    def test_seed_also_chooses_which_grid_cells_are_used(self):
        first = _planned(shuffle_seed=1234)
        other = _planned(shuffle_seed=5678)

        assert [target.rect for target in first.targets] != [
            target.rect for target in other.targets
        ]

    def test_dispense_order_runs_from_one_in_cell_index_order(self):
        plan = _planned()

        assert [cell.order for cell in plan.cells] == list(
            range(1, len(plan.cells) + 1)
        )
        assert [cell.index for cell in plan.cells] == sorted(
            cell.index for cell in plan.cells
        )

    def test_single_sample_places_the_minimum_volume_once(self):
        plan = _planned(volume_divisions=1, samples_per_volume=1, blank_count=0)

        assert len(plan.cells) == 1
        assert plan.cells[0].index == 1
        assert plan.cells[0].order == 1
        assert plan.cells[0].volume_index == 0
        assert plan.cells[0].commanded_volume_ul == pytest.approx(0.05)


class TestPlanDotGridBlankCells:
    """塗布しない blank セル（真値 0）の混在と識別."""

    def test_blank_cells_are_planned_in_the_requested_count(self):
        plan = _planned(volume_divisions=5, samples_per_volume=3, blank_count=4)

        assert len(plan.blanks) == 4
        assert len(plan.cells) == 15

    def test_blank_cells_carry_no_volume_at_all(self):
        plan = _planned()

        for blank in plan.blanks:
            assert not hasattr(blank, "commanded_volume_ul")
            assert not hasattr(blank, "volume_index")

    def test_blank_and_dispensed_indices_form_one_numbering(self):
        plan = _planned()

        indices = [cell.index for cell in plan.cells] + [
            blank.index for blank in plan.blanks
        ]
        assert sorted(indices) == list(range(1, plan.spec.target_count + 1))

    def test_blank_cells_are_shuffled_in_with_the_dispensed_cells(self):
        # blank が末尾に固まっていれば、板の特定領域（採番の後半）へ偏る。
        plan = _planned(shuffle_seed=1234)
        blank_indices = [blank.index for blank in plan.blanks]

        assert blank_indices != list(
            range(plan.spec.sample_count + 1, plan.spec.target_count + 1)
        )

    def test_blank_placement_follows_the_seed(self):
        first = [b.index for b in _planned(shuffle_seed=1234).blanks]
        same = [b.index for b in _planned(shuffle_seed=1234).blanks]
        other = [b.index for b in _planned(shuffle_seed=5678).blanks]

        assert first == same
        assert first != other

    def test_zero_blank_count_plans_no_blank(self):
        plan = _planned(blank_count=0)

        assert plan.blanks == ()
        assert len(plan.targets) == plan.spec.sample_count


class TestPlanDotGridRejections:
    """装置を開く前に失敗させる条件（不正設定・容量不足）."""

    def test_invalid_spec_is_rejected_before_planning(self):
        spec = _spec(cell_size_mm=0.0)

        plan, error = plan_dot_grid(spec)

        assert plan is None
        assert error == spec.validate()

    def test_capacity_shortfall_reports_cell_and_target_counts(self):
        # 板 14x14 / 余白 2 → 有効領域 10x10 に 3 行 3 列。パージが左上 1 枚を潰して
        # 8 セルしか残らないので、量 6 点 + blank 4 点 = 10 点は収まらない。
        plan, error = plan_dot_grid(
            _spec(
                plate_width_mm=14.0,
                plate_height_mm=14.0,
                volume_divisions=3,
                samples_per_volume=2,
                blank_count=4,
            )
        )

        assert plan is None
        assert error is not None
        assert "8" in error
        assert "10" in error

    def test_blank_cells_count_towards_the_capacity_requirement(self):
        plan, error = plan_dot_grid(
            _spec(
                plate_width_mm=14.0,
                plate_height_mm=14.0,
                volume_divisions=3,
                samples_per_volume=2,
                blank_count=2,
            )
        )

        assert error is None
        assert plan is not None
        assert len(plan.targets) == 8


class TestPlanViews:
    """中心 view と周辺 view の角度・個数・半径検証."""

    def test_zero_count_plans_the_central_view_only(self):
        views, error = plan_views(0, 1.0)

        assert error is None
        assert views is not None
        assert len(views) == 1
        assert views[0].number == 0
        assert views[0].offset_x_mm == 0.0
        assert views[0].offset_y_mm == 0.0

    def test_zero_count_ignores_the_radius(self):
        views, error = plan_views(0, 0.0)

        assert error is None
        assert views is not None
        assert len(views) == 1

    @pytest.mark.parametrize("count", [1, 3, 4, 8])
    def test_peripheral_views_are_evenly_spaced_from_plus_x(self, count: int):
        radius = 1.5

        views, error = plan_views(count, radius)

        assert error is None
        assert views is not None
        assert [view.number for view in views] == list(range(count + 1))
        for view in views[1:]:
            angle = 2.0 * math.pi * (view.number - 1) / count
            assert view.offset_x_mm == pytest.approx(radius * math.cos(angle))
            assert view.offset_y_mm == pytest.approx(radius * math.sin(angle))

    def test_all_planned_views_pass_their_own_validation(self):
        views, _ = plan_views(4, 1.0)

        assert views is not None
        assert [view.validate() for view in views] == [None] * 5

    @pytest.mark.parametrize("radius_mm", [0.0, -1.0, float("nan"), float("inf")])
    def test_rejects_unusable_radius_when_peripheral_views_are_requested(
        self, radius_mm: float
    ):
        views, error = plan_views(4, radius_mm)

        assert views is None
        assert error is not None
        assert "view" in error

    def test_rejects_negative_view_count(self):
        views, error = plan_views(-1, 1.0)

        assert views is None
        assert error is not None
        assert "view" in error


def _views(count: int, radius_mm: float):
    views, error = plan_views(count, radius_mm)

    assert error is None, error
    assert views is not None
    return views


class TestValidateCropInFrame:
    """View offset を足しても crop が camera frame へ収まるかを装置前に見る.

    判定式は ``max(|dx|, |dy|) * pixel_per_mm + crop_size_px / 2 + 1
    <= min(resolution) / 2``（frame 中心にセル中心が来る前提）。
    """

    def test_centered_view_only_fits_easily(self):
        assert (
            validate_crop_in_frame(
                _views(0, 1.0),
                crop_size_px=241,
                pixel_per_mm=120.5,
                resolution=(1280, 720),
            )
            is None
        )

    def test_exactly_fitting_offset_is_accepted(self):
        # min(resolution)/2 = 360、crop_size_px/2 + 1 = 61.5 → offset に使えるのは
        # 298.5 px。pixel_per_mm = 100 なら 2.985 mm でちょうど等号。
        assert (
            validate_crop_in_frame(
                _views(4, 2.985),
                crop_size_px=121,
                pixel_per_mm=100.0,
                resolution=(1280, 720),
            )
            is None
        )

    def test_one_pixel_beyond_the_frame_is_rejected(self):
        error = validate_crop_in_frame(
            _views(4, 2.995),
            crop_size_px=121,
            pixel_per_mm=100.0,
            resolution=(1280, 720),
        )

        assert error is not None
        assert "収まりません" in error

    def test_short_axis_of_the_resolution_decides(self):
        # 横 1280 には余裕があるが、縦 720 が効いて 401 px の crop は収まらない。
        assert (
            validate_crop_in_frame(
                _views(4, 2.985),
                crop_size_px=401,
                pixel_per_mm=100.0,
                resolution=(1280, 720),
            )
            is not None
        )

    def test_larger_crop_needs_a_smaller_offset(self):
        views = _views(4, 2.0)

        assert (
            validate_crop_in_frame(
                views, crop_size_px=241, pixel_per_mm=100.0, resolution=(1280, 720)
            )
            is None
        )
        assert (
            validate_crop_in_frame(
                views, crop_size_px=321, pixel_per_mm=100.0, resolution=(1280, 720)
            )
            is not None
        )

    def test_rejects_non_positive_pixel_per_mm(self):
        error = validate_crop_in_frame(
            _views(4, 1.0),
            crop_size_px=241,
            pixel_per_mm=0.0,
            resolution=(1280, 720),
        )

        assert error is not None
        assert "pixel_per_mm" in error


class TestValidateCaptureReach:
    """全（セル × view）の撮影目標がステージ可動域に入るかを装置前に見る."""

    def test_accepts_a_plan_that_fits_the_soft_limits(self):
        plan = _planned()

        assert (
            validate_capture_reach(
                plan,
                _views(4, 1.0),
                board_to_stage=Shift(100.0, 50.0),
                x_limits=(0.0, 300.0),
                y_limits=(0.0, 300.0),
            )
            is None
        )

    def test_rejects_when_a_view_offset_crosses_the_soft_limit(self):
        plan = _planned()

        error = validate_capture_reach(
            plan,
            _views(4, 1.0),
            board_to_stage=Shift(100.0, 50.0),
            x_limits=(0.0, 100.5),
            y_limits=(0.0, 300.0),
        )

        assert error is not None
        assert "可動域" in error

    def test_blank_cells_are_checked_as_well(self):
        plan = _planned()
        last_blank = max(blank.index for blank in plan.blanks)
        # blank だけが外れる可動域を作れないので、blank を含む全 target が
        # 検査対象であることを「全点が入る限界」で確認する。
        assert last_blank >= 1
        assert (
            validate_capture_reach(
                plan,
                _views(0, 1.0),
                board_to_stage=Shift(0.0, 0.0),
                x_limits=(0.0, 20.0),
                y_limits=(0.0, 20.0),
            )
            is None
        )
        assert (
            validate_capture_reach(
                plan,
                _views(0, 1.0),
                board_to_stage=Shift(0.0, 0.0),
                x_limits=(0.0, 5.0),
                y_limits=(0.0, 20.0),
            )
            is not None
        )


class TestValidateDispenseReach:
    """パージ点と全塗布セルのノズル目標がステージ可動域に入るかを装置前に見る."""

    def test_accepts_a_plan_that_fits_the_soft_limits(self):
        plan = _planned()

        assert (
            validate_dispense_reach(
                plan,
                board_to_machine=Shift(100.0, 50.0),
                x_limits=(0.0, 300.0),
                y_limits=(0.0, 300.0),
            )
            is None
        )

    def test_rejects_when_the_toolhead_offset_pushes_a_cell_out(self):
        # 撮影位置（board 変換のみ）は入るが、toolhead offset ぶんずれた塗布位置は
        # 可動域を外れる、という取りこぼしを検出する。
        plan = _planned()
        board_to_stage = Shift(0.0, 0.0)
        board_to_machine = Shift(0.0, 22.8349)
        # パージ点（board y = 3）は toolhead offset を足しても収まり、板の下側の
        # 塗布セル（board y = 15）だけが外れる上限にする。
        limits = {"x_limits": (0.0, 20.0), "y_limits": (0.0, 26.0)}

        assert (
            validate_capture_reach(
                plan, _views(0, 1.0), board_to_stage=board_to_stage, **limits
            )
            is None
        )
        error = validate_dispense_reach(
            plan, board_to_machine=board_to_machine, **limits
        )

        assert error is not None
        assert "塗布位置" in error
        assert "可動域" in error

    def test_purge_point_is_checked_as_well(self):
        # パージ点は有効領域の左上（中心 3, 3）にあり、塗布セルはそれより右か下にある。
        # 左上を切り落とす可動域では、まずパージ点が理由として返る。
        plan = _planned()

        error = validate_dispense_reach(
            plan,
            board_to_machine=Shift(0.0, 0.0),
            x_limits=(3.5, 20.0),
            y_limits=(3.5, 20.0),
        )

        assert error is not None
        assert "パージ位置" in error

    def test_blank_cells_are_not_dispense_targets(self):
        # seed 4 は塗布セル (3, 9)・blank (12, 6)。塗布点とパージだけが入る可動域では
        # blank が外にあっても通る（blank は塗布しないので可動域の制約にならない）。
        plan = _planned(
            volume_divisions=1, samples_per_volume=1, blank_count=1, shuffle_seed=4
        )
        limits = {"x_limits": (0.0, 3.5), "y_limits": (0.0, 20.0)}

        assert plan.cells[0].center.x == pytest.approx(3.0)
        assert plan.blanks[0].center.x == pytest.approx(12.0)
        assert (
            validate_dispense_reach(plan, board_to_machine=Shift(0.0, 0.0), **limits)
            is None
        )
        assert (
            validate_capture_reach(
                plan, _views(0, 1.0), board_to_stage=Shift(0.0, 0.0), **limits
            )
            is not None
        )


class TestValidateMinRotations:
    """最小の指令吐出量が意味のある回転数になるかを装置前に見る."""

    def test_accepts_a_minimum_volume_above_the_rotation_floor(self):
        assert (
            validate_min_rotations(_spec(volume_min_ul=0.05), rotations_per_ul=20.0)
            is None
        )

    def test_rejects_a_minimum_volume_that_barely_turns_the_stepper(self):
        # 0.05 uL x 0.001 rev/uL = 5e-5 rev で 1 マイクロステップに届かない
        error = validate_min_rotations(
            _spec(volume_min_ul=0.05), rotations_per_ul=0.001
        )

        assert error is not None
        assert "0.05" in error

    def test_the_default_floor_is_one_microstep_of_the_paste_screw(self):
        # 200 step/rev を 64 分割した 1 マイクロステップが指令の分解能
        assert MIN_COMMANDED_ROTATIONS == pytest.approx(1.0 / 12800)

    def test_boundary_at_the_rotation_floor_is_accepted(self):
        # 0.05 uL x 2.0 rev/uL = 0.1 rev = 既定の下限そのもの
        assert (
            validate_min_rotations(
                _spec(volume_min_ul=0.05), rotations_per_ul=2.0, min_rotations=0.1
            )
            is None
        )

    def test_just_below_the_rotation_floor_is_rejected(self):
        assert (
            validate_min_rotations(
                _spec(volume_min_ul=0.05), rotations_per_ul=1.9, min_rotations=0.1
            )
            is not None
        )

    @pytest.mark.parametrize("rotations_per_ul", [0.0, -1.0, float("nan")])
    def test_rejects_invalid_rotations_per_ul(self, rotations_per_ul: float):
        error = validate_min_rotations(_spec(), rotations_per_ul=rotations_per_ul)

        assert error is not None
        assert "rotations_per_ul" in error


class TestPreviewDotGrid:
    """WebUI へ返す診断用 preview（配置不能でも描ける）と派生カウントの契約.

    router / JS が撮影枚数や総点数を再導出しないよう、派生値はここで確定させる。
    """

    def test_valid_spec_carries_the_plan_and_the_whole_grid(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=1.0)

        assert preview.error is None
        plan = _planned()
        assert [cell.index for cell in preview.cells] == [
            cell.index for cell in plan.cells
        ]
        assert [blank.index for blank in preview.blanks] == [
            blank.index for blank in plan.blanks
        ]
        assert preview.capacity == plan.capacity
        assert preview.usable_area == USABLE
        assert preview.purge_cell == plan.purge_cell

    def test_grid_includes_cells_that_no_sample_uses(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=1.0)

        used = {(cell.rect.x, cell.rect.y) for cell in preview.cells}
        used |= {(blank.rect.x, blank.rect.y) for blank in preview.blanks}
        grid = {(rect.x, rect.y) for rect in preview.grid}

        # 格子はパージ除外前の全セルなので、使用セルを真に含む。
        assert used < grid
        assert len(preview.grid) > preview.capacity

    def test_view_count_includes_the_central_view(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=1.0)

        assert preview.views_per_cell == 5

    def test_single_view_collection_counts_only_the_central_view(self):
        preview = preview_dot_grid(_spec(), view_count=0, view_offset_mm=1.0)

        assert preview.views_per_cell == 1

    def test_image_count_covers_every_target_view_and_phase(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=1.0)

        assert preview.target_count == preview.sample_count + _spec().blank_count
        assert preview.image_count == preview.target_count * preview.views_per_cell * 2

    def test_volumes_are_reported_for_the_legend(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=1.0)

        assert preview.volumes_ul == _spec().volumes_ul

    def test_over_capacity_keeps_the_geometry_and_reports_the_reason(self):
        preview = preview_dot_grid(
            _spec(samples_per_volume=100), view_count=4, view_offset_mm=1.0
        )

        assert preview.error is not None
        assert preview.cells == ()
        assert preview.blanks == ()
        # 収まらなくても板・有効領域・パージ・格子は描ける。
        assert preview.usable_area == USABLE
        assert preview.purge_cell is not None
        assert preview.grid != ()

    def test_invalid_spec_still_reports_the_plate(self):
        preview = preview_dot_grid(
            _spec(edge_margin_mm=-1.0), view_count=4, view_offset_mm=1.0
        )

        assert preview.error is not None
        assert preview.plate == Rect(0.0, 0.0, 20.0, 20.0)
        assert preview.usable_area is None
        assert preview.grid == ()

    def test_invalid_view_settings_are_reported(self):
        preview = preview_dot_grid(_spec(), view_count=4, view_offset_mm=0.0)

        assert preview.error is not None
