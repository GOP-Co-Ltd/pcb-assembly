import pytest

from pcbasm.hal import Speed


class TestSpeed:
    """Speedクラスのテスト.

    Speedは送り速度を表す値オブジェクト。絶対値[mm/s]か、max_velocityに対する
    割合[0,1]のいずれかで構築され、resolve(max_velocity)で実際の速度に解決される。
    """

    def test_absolute_ignores_max(self):
        speed = Speed.absolute(150.0)

        assert speed.resolve(300.0) == 150.0

    def test_absolute_ignores_different_max(self):
        # 絶対値はmax_velocityを無視するため、別のmaxでも同じ値を返す
        speed = Speed.absolute(150.0)

        assert speed.resolve(1000.0) == 150.0

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
