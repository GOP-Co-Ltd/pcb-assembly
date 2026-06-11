"""to_machine_transform の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md
「conjugation の厳密な式」に基づく符号ピン。

camera→machine 写像（アンカー a に対し）:
    ψ_a(o) = a − R(o)
           = Compose([offset_transform, Scale.flip(x=True, y=True), Shift.from_point(a)])
machine 空間補正 Transform:
    M = ψ_{observed_at} ∘ G ∘ ψ_{projection_anchor}⁻¹

実機検証済みアンカー (A4) `target = pos − R(o)` と全点一致することが最重要。
ψ はテスト側で計画書の式どおりに独立構築し、実装と突き合わせる。
"""

import pytest

from pcbasm.geometry import (
    Compose,
    Point2d,
    Rotation,
    Scale,
    Shift,
    Transform,
)
from pcbasm.posctrl.correction import to_machine_transform

_SAMPLE_POINTS = (Point2d(0.0, 0.0), Point2d(12.0, 34.0), Point2d(-3.5, 8.25))


def _psi(offset_transform: Transform, anchor: Point2d) -> Transform:
    """計画書の式そのままの ψ_a(o) = a − R(o)（実装と独立な ground truth）."""
    return Compose(
        [offset_transform, Scale.flip(x=True, y=True), Shift.from_point(anchor)]
    )


class TestToMachineTransform:
    """to_machine_transform の conjugation 整合のテスト."""

    @pytest.mark.parametrize("degrees", [0.0, 7.0, -30.0])
    def test_pure_shift_at_anchor_matches_a4_displacement(self, degrees: float):
        """最重要符号ピン: G=Shift(d), s_obs=s0 → 全点で M(p) − p = −R(d).

        (A4) の補正式 target = pos − R(o) による変位と完全一致する。 厳密幾何の R⁻¹
        に「修正」した実装はここで割れる。
        """
        offset_transform = Rotation(degrees)
        d = Point2d(0.5, -0.3)
        pos = Point2d(12.0, 34.0)

        machine = to_machine_transform(
            camera_transform=Shift(d.x, d.y),
            offset_transform=offset_transform,
            projection_anchor=pos,
            observed_at=pos,
        )

        expected = -1.0 * offset_transform.apply(d)
        for point in _SAMPLE_POINTS:
            moved = machine.apply(point)
            assert moved.x - point.x == pytest.approx(expected.x, abs=1e-9)
            assert moved.y - point.y == pytest.approx(expected.y, abs=1e-9)

    def test_anchor_observed_at_mismatch_adds_stage_displacement(self):
        """s_obs ≠ s0 → 並進 = (s_obs − s0) − R(d)（adjust() 累積補正と一致）."""
        offset_transform = Rotation(7.0)
        d = Point2d(0.5, -0.3)
        anchor = Point2d(10.0, 20.0)
        observed_at = Point2d(10.4, 19.7)

        machine = to_machine_transform(
            camera_transform=Shift(d.x, d.y),
            offset_transform=offset_transform,
            projection_anchor=anchor,
            observed_at=observed_at,
        )

        expected = (observed_at - anchor) - offset_transform.apply(d)
        for point in _SAMPLE_POINTS:
            moved = machine.apply(point)
            assert moved.x - point.x == pytest.approx(expected.x, abs=1e-9)
            assert moved.y - point.y == pytest.approx(expected.y, abs=1e-9)

    def test_camera_rotation_conjugates_to_same_machine_rotation(self):
        """カメラ空間の c 回り回転 θ → 機械空間でも回転 θ、固定点は ψ(c).

        R が純回転（det>0）のとき θ_machine = θ（符号反転しない）。
        """
        offset_transform = Rotation(7.0)
        theta = 1.5
        c = Point2d(0.2, -0.1)
        pos = Point2d(12.0, 34.0)
        camera_transform = Compose(
            [Shift(-c.x, -c.y), Rotation(theta), Shift(c.x, c.y)]
        )

        machine = to_machine_transform(
            camera_transform=camera_transform,
            offset_transform=offset_transform,
            projection_anchor=pos,
            observed_at=pos,
        )

        fixed = _psi(offset_transform, pos).apply(c)
        moved_fixed = machine.apply(fixed)
        assert moved_fixed.x == pytest.approx(fixed.x, abs=1e-9)
        assert moved_fixed.y == pytest.approx(fixed.y, abs=1e-9)
        tip = machine.apply(fixed + Point2d(1.0, 0.0))
        rotation = Rotation.from_points(Point2d(1.0, 0.0), tip - fixed)
        assert rotation.degrees == pytest.approx(theta, abs=1e-9)

    def test_mirror_offset_transform_flips_machine_rotation_sign(self):
        """Det<0 の R では Compose 共役が自動で θ → −θ に反転する."""
        offset_transform = Compose([Scale.flip(x=True), Rotation(7.0)])
        theta = 1.5
        c = Point2d(0.2, -0.1)
        pos = Point2d(12.0, 34.0)
        camera_transform = Compose(
            [Shift(-c.x, -c.y), Rotation(theta), Shift(c.x, c.y)]
        )

        machine = to_machine_transform(
            camera_transform=camera_transform,
            offset_transform=offset_transform,
            projection_anchor=pos,
            observed_at=pos,
        )

        fixed = _psi(offset_transform, pos).apply(c)
        moved_fixed = machine.apply(fixed)
        assert moved_fixed.x == pytest.approx(fixed.x, abs=1e-9)
        assert moved_fixed.y == pytest.approx(fixed.y, abs=1e-9)
        tip = machine.apply(fixed + Point2d(1.0, 0.0))
        rotation = Rotation.from_points(Point2d(1.0, 0.0), tip - fixed)
        assert rotation.degrees == pytest.approx(-theta, abs=1e-9)

    def test_conjugating_back_recovers_camera_transform(self):
        """ψ_{s_obs}⁻¹ ∘ M ∘ ψ_{s0} ≈ G（カメラ空間に戻すと元の照合結果）."""
        offset_transform = Rotation(7.0)
        anchor = Point2d(10.0, 20.0)
        observed_at = Point2d(10.3, 19.8)
        camera_transform = Compose([Shift(-0.1, 0.05), Rotation(0.8)])

        machine = to_machine_transform(
            camera_transform=camera_transform,
            offset_transform=offset_transform,
            projection_anchor=anchor,
            observed_at=observed_at,
        )

        psi_anchor = _psi(offset_transform, anchor)
        psi_observed = _psi(offset_transform, observed_at)
        for o in (Point2d(0.0, 0.0), Point2d(1.2, -0.7), Point2d(-0.4, 0.9)):
            recovered = psi_observed.inverse().apply(machine.apply(psi_anchor.apply(o)))
            expected = camera_transform.apply(o)
            assert recovered.x == pytest.approx(expected.x, abs=1e-9)
            assert recovered.y == pytest.approx(expected.y, abs=1e-9)
