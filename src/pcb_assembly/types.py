import attrs


@attrs.define(slots=True, frozen=True)
class Position:
    """位置情報を保持するクラス."""

    x: float
    y: float
    z: float
