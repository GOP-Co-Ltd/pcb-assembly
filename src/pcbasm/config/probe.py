"""[probe] 節：高さ面を測るロードセルプローブ."""

import attrs


def validate_probe_board_edge_margin(value: float) -> str | None:
    """基板外形からの probe 点マージンを検証する."""
    if value <= 0:
        return (
            "board_edge_marginは正の値である必要があります。"
            f"board_edge_margin={value}"
        )
    return None


@attrs.frozen
class ProbeConfig:
    """ロードセルプローブの設定."""

    min_radius: float  # サンプル点が銅箔境界から確保すべき最小距離 [mm] (ノズルが銅箔島の外に出ないためのクリアランス)
    board_edge_margin: float = 2.5  # サンプル点が基板外形から確保すべき最小距離 [mm]
    lift_height: float = 1.0  # PROBE 実行後に接触点から持ち上げる高さ [mm]
    min_samples: int = (
        6  # 最小サンプル数 (HeightPlane の 2 次曲面フィットに必要な最小点数)
    )
    max_samples: int = 9  # 最大サンプル数

    def __attrs_post_init__(self) -> None:
        if self.min_radius <= 0:
            raise ValueError(
                f"min_radiusは正の値である必要があります。min_radius={self.min_radius}"
            )
        if error := validate_probe_board_edge_margin(self.board_edge_margin):
            raise ValueError(error)
        if self.min_samples < 6:
            raise ValueError(
                f"min_samplesは6以上である必要があります。min_samples={self.min_samples}"
            )
        if self.min_samples > self.max_samples:
            raise ValueError(
                "min_samplesはmax_samples以下である必要があります。"
                f"min_samples={self.min_samples}, max_samples={self.max_samples}"
            )
