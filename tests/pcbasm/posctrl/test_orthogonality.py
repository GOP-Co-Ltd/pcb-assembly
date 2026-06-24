"""`pcbasm.posctrl.orthogonality` の仕様テスト.

board_transform → 直行性指標（軸間角ずれ・軸スケール）を導出する純粋計算。 剛体変換は誤差ゼロ、shear
で既知の角度誤差を返す。符号の物理的解釈は実機 検証待ちのため、大きさのみをピンする。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from pcbasm.geometry import Compose, Identity, Matrix2d, Rotation, Shift
from pcbasm.posctrl import OrthogonalityMetrics


class TestOrthogonalityMetrics:
    """OrthogonalityMetrics.from_transform（board_transform → 直行性指標を導出）.

    数値定義: axis_angle_error_deg = 変換後の X/Y 軸間角の 90° からのずれ、 scale_x =
    |T(1,0)−T(0,0)|, scale_y = |T(0,1)−T(0,0)|。
    """

    def test_identity_has_zero_error_and_unit_scales(self):
        metrics = OrthogonalityMetrics.from_transform(Identity())

        assert metrics.axis_angle_error_deg == pytest.approx(0.0, abs=1e-9)
        assert metrics.scale_x == pytest.approx(1.0, abs=1e-9)
        assert metrics.scale_y == pytest.approx(1.0, abs=1e-9)

    def test_rigid_transform_has_zero_error(self):
        """回転 + 並進（実機の正常な board_transform 相当）は誤差ゼロ."""
        metrics = OrthogonalityMetrics.from_transform(
            Compose([Rotation(30.0), Shift(10.0, -5.0)])
        )

        assert metrics.axis_angle_error_deg == pytest.approx(0.0, abs=1e-9)
        assert metrics.scale_x == pytest.approx(1.0, abs=1e-9)
        assert metrics.scale_y == pytest.approx(1.0, abs=1e-9)

    def test_shear_yields_known_axis_angle_error(self):
        """X 軸方向の shear（tan 2°）→ 軸間角が 88° = ずれの大きさ 2°.

        符号の物理的解釈は実機検証待ちのため、大きさのみをピンする。
        """
        shear = Matrix2d(np.array([[1.0, math.tan(math.radians(2.0))], [0.0, 1.0]]))

        metrics = OrthogonalityMetrics.from_transform(shear)

        assert abs(metrics.axis_angle_error_deg) == pytest.approx(2.0, abs=1e-6)
        assert metrics.scale_x == pytest.approx(1.0, abs=1e-9)
        # Y 軸単位ベクトル (tan2°, 1) の長さ = 1/cos2°
        assert metrics.scale_y == pytest.approx(
            1.0 / math.cos(math.radians(2.0)), abs=1e-9
        )

    def test_shear_error_is_rotation_invariant(self):
        """剛体変換の合成は指標を変えない（座標系の取り方に依存しない）."""
        shear = Matrix2d(np.array([[1.0, math.tan(math.radians(2.0))], [0.0, 1.0]]))
        composed = Compose([shear, Rotation(45.0), Shift(3.0, 7.0)])

        plain = OrthogonalityMetrics.from_transform(shear)
        rotated = OrthogonalityMetrics.from_transform(composed)

        assert rotated.axis_angle_error_deg == pytest.approx(
            plain.axis_angle_error_deg, abs=1e-9
        )
        assert rotated.scale_x == pytest.approx(plain.scale_x, abs=1e-9)
        assert rotated.scale_y == pytest.approx(plain.scale_y, abs=1e-9)

    def test_axis_scales_match_diagonal_matrix(self):
        metrics = OrthogonalityMetrics.from_transform(
            Matrix2d(np.array([[2.0, 0.0], [0.0, 0.5]]))
        )

        assert metrics.axis_angle_error_deg == pytest.approx(0.0, abs=1e-9)
        assert metrics.scale_x == pytest.approx(2.0, abs=1e-9)
        assert metrics.scale_y == pytest.approx(0.5, abs=1e-9)
