from typing import Self

import attrs


@attrs.frozen
class Speed:
    """送り速度を表すイミュータブルなクラス.

    絶対値[mm/s]またはmax_velocityに対する割合[0,1]で表現する。
    インスタンスは ``absolute`` / ``rate`` クラスメソッド経由で生成する。

    Attributes:
        _value: 速度の値（絶対値[mm/s]または割合[0,1]）
        _is_fraction: 値が割合表現かどうか
    """

    _value: float = attrs.field(alias="value")
    _is_fraction: bool = attrs.field(alias="is_fraction")

    @classmethod
    def absolute(cls, mm_per_s: float) -> Self:
        """絶対速度[mm/s]からSpeedを生成する.

        Args:
            mm_per_s: 送り速度[mm/s]

        Returns:
            絶対速度を表すSpeedインスタンス
        """
        return cls(value=mm_per_s, is_fraction=False)

    @classmethod
    def rate(cls, fraction: float) -> Self:
        """max_velocityに対する割合[0,1]からSpeedを生成する.

        Args:
            fraction: max_velocityに対する割合（0以上1以下）

        Returns:
            割合表現のSpeedインスタンス

        Raises:
            ValueError: fractionが[0, 1]の範囲外の場合
        """
        if not 0.0 <= fraction <= 1.0:
            msg = f"rateは[0, 1]の範囲内である必要があります。与えられた値: {fraction}"
            raise ValueError(msg)
        return cls(value=fraction, is_fraction=True)

    def resolve(self, max_velocity: float) -> float:
        """最大速度を用いて速度を絶対値[mm/s]に解決する.

        Args:
            max_velocity: ステージの最大速度[mm/s]

        Returns:
            解決された絶対速度[mm/s]
        """
        if self._is_fraction:
            return self._value * max_velocity
        return self._value
