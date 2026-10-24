"""[settle] 節：移動・プローブ後の静定待ち."""

import attrs

from pcbasm.validators import validate_non_negative_number


@attrs.frozen
class SettleConfig:
    """装置の静定待ち（[settle]。セクションごと省略可）.

    どちらも G4 dwell としてステージへ送る。ステージが止まっても像や機構は少し揺れて
    いるので、撮影・プローブの前に待つ。長くすればタクトタイムが伸び、短くすれば
    検出がぶれる。適切な値は機体の剛性で決まるので、機体ごとに調整する。

    ``move_sec`` は位置合わせ・基板計測・高さ計測・撮影など、移動後の静定すべてで
    共用する。分けるべき差（例えば Z 下降後だけ長く要る）が実測で出たら、そのとき
    設定項目を追加する。

    Attributes:
        move_sec: ステージ移動後、撮影・計測に入るまでの待ち [sec]
        probe_sec: PROBE 実行後の待ち [sec]
    """

    move_sec: float = 0.5
    probe_sec: float = 0.0

    def __attrs_post_init__(self) -> None:
        for name in ("move_sec", "probe_sec"):
            if error := validate_non_negative_number(name, getattr(self, name)):
                raise ValueError(error)
