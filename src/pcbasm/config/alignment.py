"""[paste_dispenser.pad_align] 節：重複領域の銅箔照合による位置合わせ."""

import attrs

from pcbasm.validators import (
    is_finite_number,
    validate_non_negative_number,
    validate_positive_number,
    validate_positive_odd_integer,
)


def validate_region_overlap(value: float) -> str | None:
    """照合領域の重なり率を検証する."""
    if not is_finite_number(value) or not 0.0 <= value < 1.0:
        return f"region_overlapは0以上1未満の有限値である必要があります: {value!r}"
    return None


@attrs.frozen
class AlignmentConfig:
    """重複領域の銅箔照合による位置合わせの設定."""

    region_size_px: int = 100  # 照合領域の一辺 [px]
    region_overlap: float = 0.5  # 隣接する照合領域の重なり [0, 1)
    board_edge_margin: float = 0.5  # 基板外形から照合領域までの余白 [mm]
    max_passes: int = 5  # 1 領域あたりの再計測上限
    converge_tolerance: float = 0.03  # 収束とみなす増分 [mm]
    max_correction: float = 1.0  # 1 領域で許容する累積ずれ [mm]
    search_window: float = 2.0  # 照合の探索窓 片側幅 [mm]
    refine_max_short_side: float = 0.4  # pad 別逐次位置合わせの最大短辺 [mm]
    canny_low: float = 100.0  # Canny エッジ検出の下側閾値
    canny_high: float = 200.0  # Canny エッジ検出の上側閾値
    blur_ksize: int = 5  # GaussianBlur カーネルサイズ (奇数)

    def __attrs_post_init__(self) -> None:
        if (
            isinstance(self.region_size_px, bool)
            or not isinstance(self.region_size_px, int)
            or self.region_size_px < 1
        ):
            raise ValueError(
                f"region_size_pxは1以上の整数である必要があります: "
                f"{self.region_size_px}"
            )
        if error := validate_region_overlap(self.region_overlap):
            raise ValueError(error)
        if (
            isinstance(self.max_passes, bool)
            or not isinstance(self.max_passes, int)
            or self.max_passes < 1
        ):
            raise ValueError(
                f"max_passesは1以上の整数である必要があります: {self.max_passes}"
            )
        for name in (
            "board_edge_margin",
            "converge_tolerance",
            "max_correction",
            "search_window",
        ):
            if error := validate_positive_number(name, getattr(self, name)):
                raise ValueError(error)
        if error := validate_non_negative_number(
            "refine_max_short_side", self.refine_max_short_side
        ):
            raise ValueError(error)
        if error := validate_positive_odd_integer("blur_ksize", self.blur_ksize):
            raise ValueError(error)
