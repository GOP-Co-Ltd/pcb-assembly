"""[detection] 節：統計検出のサンプリング."""

import attrs

from pcbasm.validators import validate_positive_int


@attrs.frozen
class DetectionConfig:
    r"""統計検出のサンプリング（[detection]。セクションごと省略可）.

    円・塗布痕の位置は 1 枚の画像では揺れるので、複数フレームの平均を 1 観測とする
    （:class:`~pcbasm.posctrl.OffsetObserver`）。枚数を増やすと平均のばらつきは
    :math:`1/\sqrt{n}` で下がるが、撮影時間はそのまま伸びる（30 fps なら 10 枚で約 0.33 秒）。

    ``minimum_sample_count`` は品質ゲートで、撮った枚数のうち何枚で対象を検出できれば
    その観測を採用するかを決める。検出枚数がこの値に届かなければ観測は失敗する。
    小さすぎると、たまたま写った 1 枚だけで位置を決めてしまう。

    この設定は、基準点の円（機械的なマーカー）とツールヘッドオフセットの塗布痕
    （濡れた円）の両方に適用される。両者は写りやすさが違う。一方だけ検出しにくい
    ときは、検出しやすい側にも同じ値が適用されることを踏まえて決める。

    Attributes:
        sample_count: 1 観測で撮るフレーム数
        minimum_sample_count: 1 観測に必要な有効検出数（``sample_count`` 以下）
    """

    sample_count: int = 10
    minimum_sample_count: int = 5

    def __attrs_post_init__(self) -> None:
        for name in ("sample_count", "minimum_sample_count"):
            if error := validate_positive_int(name, getattr(self, name)):
                raise ValueError(error)
        if self.minimum_sample_count > self.sample_count:
            raise ValueError(
                "minimum_sample_countはsample_count以下である必要があります: "
                f"minimum_sample_count={self.minimum_sample_count}, "
                f"sample_count={self.sample_count}"
            )
