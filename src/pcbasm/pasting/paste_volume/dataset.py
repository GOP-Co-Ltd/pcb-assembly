"""1 sample を素のまま読み出す層.

``decode`` しかしない。resize も rotate も float 化も標準化も行わず、
:mod:`pcbasm.pasting.paste_volume.batch` の collator へ ``uint8`` のまま渡す。

幾何変換を collate 側へ寄せるのは、augmentation の parameter と間引く view が
batch と epoch から決まるため。1 sample を読む時点ではまだどちらも決まっていない。
"""

from __future__ import annotations

import attrs
from torch import Tensor

from ml.data.image import decode_rgb_image
from pcbasm.pasting.paste_volume.index import (
    PasteVolumeSampleEntry,
    PasteVolumeSampleIndex,
)


@attrs.frozen(eq=False)
class PasteVolumeRawSample:
    """読み出しただけの 1 sample.

    ``view_images`` は view ごとの ``(pre, post)`` で、いずれも RGB ``uint8 [3, H, W]``。
    この対の順序がそのまま channel 連結の順序（0-2 が塗布前、3-5 が塗布後）になる。
    """

    entry: PasteVolumeSampleEntry
    view_images: tuple[tuple[Tensor, Tensor], ...]


class PasteVolumeDataset:
    """登録済みの sample の画像を読み出す."""

    def __init__(self, index: PasteVolumeSampleIndex) -> None:
        self._index = index

    @property
    def index(self) -> PasteVolumeSampleIndex:
        """読み出し対象の sample index."""

        return self._index

    def sample(self, sample_id: str) -> PasteVolumeRawSample:
        """1 sample の全 view を読み出す.

        path の実在も寸法の一致も index を作る時点で確立済みの不変条件なので、 ここでは検査しない。破れていれば
        decode が例外を投げる。
        """

        entry = self._index.entry_for(sample_id)
        return PasteVolumeRawSample(
            entry=entry,
            view_images=tuple(
                (decode_rgb_image(view.pre), decode_rgb_image(view.post))
                for view in entry.views
            ),
        )


__all__ = [
    "PasteVolumeDataset",
    "PasteVolumeRawSample",
]
