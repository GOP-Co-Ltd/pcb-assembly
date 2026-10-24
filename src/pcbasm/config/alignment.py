"""[alignment] 節：重複領域の銅箔照合による位置合わせ."""

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import (
    as_validator,
    is_finite_number,
    validate_non_negative_number,
    validate_positive_int,
    validate_positive_number,
    validate_positive_odd_integer,
)

_MATCH = "銅箔照合"

_positive = as_validator(validate_positive_number)
_positive_int = as_validator(validate_positive_int)


def _region_overlap(_: object, __: attrs.Attribute, value: float) -> None:
    if not is_finite_number(value) or not 0.0 <= value < 1.0:
        raise ValueError(
            f"region_overlapは0以上1未満の有限値である必要があります: {value!r}"
        )


@attrs.frozen
class AlignmentConfig:
    """重複領域の銅箔照合による位置合わせの設定."""

    # 照合領域の一辺 [px]
    region_size_px: int = attrs.field(
        default=100,
        validator=_positive_int,
        metadata=setting(
            label="照合領域の一辺", unit="px", group=_MATCH, order=1, minimum=1
        ),
    )
    # 隣接する照合領域の重なり [0, 1)
    region_overlap: float = attrs.field(
        default=0.5,
        validator=_region_overlap,
        metadata=setting(label="照合領域の重なり", group=_MATCH, order=2, minimum=0.0),
    )
    # 基板外形から照合領域までの余白 [mm]
    board_edge_margin: float = attrs.field(
        default=0.5,
        validator=_positive,
        metadata=setting(label="基板外形からの余白", unit="mm", group=_MATCH, order=3),
    )
    # 1 領域あたりの再計測上限
    max_passes: int = attrs.field(
        default=5,
        validator=_positive_int,
        metadata=setting(label="再計測の上限回数", group=_MATCH, order=6, minimum=1),
    )
    # 収束とみなす増分 [mm]
    converge_tolerance: float = attrs.field(
        default=0.03,
        validator=_positive,
        metadata=setting(label="収束判定の増分", unit="mm", group=_MATCH, order=7),
    )
    # 1 領域で許容する累積ずれ [mm]
    max_correction: float = attrs.field(
        default=1.0,
        validator=_positive,
        metadata=setting(label="最大補正量", unit="mm", group=_MATCH, order=5),
    )
    # 照合の探索窓 片側幅 [mm]
    search_window: float = attrs.field(
        default=2.0,
        validator=_positive,
        metadata=setting(label="探索窓 片側幅", unit="mm", group=_MATCH, order=4),
    )
    # pad 別逐次位置合わせの最大短辺 [mm]
    refine_max_short_side: float = attrs.field(
        default=0.4,
        validator=as_validator(validate_non_negative_number),
        metadata=setting(
            label="逐次位置合わせ対象の最大短辺",
            unit="mm",
            group="pad ごとの逐次位置合わせ",
            order=11,
            minimum=0.0,
        ),
    )
    # Canny エッジ検出の下側閾値
    canny_low: float = attrs.field(
        default=100.0, metadata=setting(label="Canny下側閾値", group=_MATCH, order=8)
    )
    # Canny エッジ検出の上側閾値
    canny_high: float = attrs.field(
        default=200.0, metadata=setting(label="Canny上側閾値", group=_MATCH, order=9)
    )
    # GaussianBlur カーネルサイズ (奇数)
    blur_ksize: int = attrs.field(
        default=5,
        validator=as_validator(validate_positive_odd_integer),
        metadata=setting(
            label="ブラーカーネルサイズ", group=_MATCH, order=10, minimum=1
        ),
    )
