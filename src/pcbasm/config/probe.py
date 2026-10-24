"""[probe] 節：高さ面を測るロードセルプローブ."""

import attrs

from pcbasm.config.fields import setting


def _positive(_: object, attribute: attrs.Attribute, value: float) -> None:
    if value <= 0:
        name = attribute.name
        raise ValueError(f"{name}は正の値である必要があります。{name}={value}")


def _at_least_six(_: object, __: attrs.Attribute, value: int) -> None:
    if value < 6:
        raise ValueError(f"min_samplesは6以上である必要があります。min_samples={value}")


@attrs.frozen
class ProbeConfig:
    """ロードセルプローブの設定."""

    # サンプル点が銅箔境界から確保すべき最小距離 [mm] (ノズルが銅箔島の外に出ないためのクリアランス)
    min_radius: float = attrs.field(
        validator=_positive,
        metadata=setting(
            label="銅箔境界からの最小距離", unit="mm", group="プローブ", order=2
        ),
    )
    # サンプル点が基板外形から確保すべき最小距離 [mm]
    board_edge_margin: float = attrs.field(
        default=2.5,
        validator=_positive,
        metadata=setting(
            label="基板外形からの最小距離", unit="mm", group="プローブ", order=3
        ),
    )
    # PROBE 実行後に接触点から持ち上げる高さ [mm]
    lift_height: float = attrs.field(
        default=1.0,
        metadata=setting(
            label="プローブ後の上昇高さ", unit="mm", group="プローブ", order=1
        ),
    )
    # 最小サンプル数 (HeightPlane の 2 次曲面フィットに必要な最小点数)
    min_samples: int = attrs.field(
        default=6,
        validator=_at_least_six,
        metadata=setting(label="最小サンプル数", group="プローブ", order=4, minimum=6),
    )
    max_samples: int = attrs.field(
        default=9, metadata=setting(label="最大サンプル数", group="プローブ", order=5)
    )

    def __attrs_post_init__(self) -> None:
        if self.min_samples > self.max_samples:
            raise ValueError(
                "min_samplesはmax_samples以下である必要があります。"
                f"min_samples={self.min_samples}, max_samples={self.max_samples}"
            )
