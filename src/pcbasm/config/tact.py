"""[tact] 節：塗布の所要時間の予測."""

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import as_validator, validate_non_negative_number


@attrs.frozen
class TactConfig:
    """塗布の所要時間の予測（[tact]。セクションごと省略可）.

    pad を塗る時間は、塗布計画と printer.cfg の速度・加速度から求める（:mod:`pcbasm.pasting.tact`）。
    ここには、それで求まらない pad 数に依らない前段の時間だけを置く。
    ジョブコンソールの経過時間（実測）と予測を見比べて合わせる。

    Attributes:
        setup_sec: 位置合わせ・高さ計測・初回パージ・終了後の退避など、pad 数に依らない前段の時間 [sec]
    """

    setup_sec: float = attrs.field(
        default=300.0,
        validator=as_validator(validate_non_negative_number),
        metadata=setting(
            label="前段の時間", unit="s", group="所要時間の予測", order=1, minimum=0.0
        ),
    )
