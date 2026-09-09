"""1 sample を素のまま読み出す層の公開契約.

Dataset は decode しかしない。resize も rotate も float 化も標準化もしないことを、 合成 PNG
の画素値をそのまま突き合わせて固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from ml.data.image import ImageConstraints
from ml.paste_volume.dataset import PasteVolumeDataset
from ml.paste_volume.index import PasteVolumeSampleIndex
from tests.ml.paste_volume.helpers import (
    CROP_SIZE_PX,
    POST_GREEN,
    POST_RED_BASE,
    PRE_BLUE,
    PRE_GREEN_BASE,
    VIEW_COUNT,
    SyntheticCell,
    write_session,
)

CONSTRAINTS = ImageConstraints()

CELLS = (
    SyntheticCell(index=1, commanded_volume_ul=0.10, x_mm=0.5),
    SyntheticCell(index=2, commanded_volume_ul=None, x_mm=2.7),
)


def _dataset(tmp_path: Path) -> PasteVolumeDataset:
    index, reason = PasteVolumeSampleIndex.from_roots(
        [write_session(tmp_path / "session", cells=CELLS)], constraints=CONSTRAINTS
    )
    assert index is not None, reason
    return PasteVolumeDataset(index)


class TestPasteVolumeDataset:
    """1 sample の読み出し."""

    def test_returns_every_view_as_a_pre_post_pair(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        entry = dataset.index.entries[0]

        sample = dataset.sample(entry.sample_id)

        assert sample.entry is entry
        assert len(sample.view_images) == VIEW_COUNT
        assert all(len(pair) == 2 for pair in sample.view_images)

    def test_returns_rgb_uint8_tensors_of_the_source_size(self, tmp_path: Path):
        """読み出したままの ``uint8 [3, H, W]`` を返す.

        float 化と標準化は collator の責務。ここで型が変わっていると、collator が
        ``preprocess`` へ渡す前提（RGB uint8）が崩れる。
        """

        dataset = _dataset(tmp_path)
        sample = dataset.sample(dataset.index.entries[0].sample_id)

        for pre, post in sample.view_images:
            for image in (pre, post):
                assert image.dtype == torch.uint8
                assert tuple(image.shape) == (3, CROP_SIZE_PX, CROP_SIZE_PX)

    def test_keeps_the_pre_image_first_in_each_pair(self, tmp_path: Path):
        """対の順序が ``(pre, post)`` であること.

        collator はこの順で channel 連結するので、入れ替わると 0-2 が塗布後、3-5 が 塗布前になる。合成
        fixture は pre だけ R が水平に変化し、post だけ B が垂直に
        変化するので、どちらがどちらかを画素で見分けられる。
        """

        dataset = _dataset(tmp_path)
        pre, post = dataset.sample(dataset.index.entries[0].sample_id).view_images[0]

        # pre の R は列方向にだけ変化する（行同士は一致し、列同士は食い違う）
        assert bool(torch.equal(pre[0][0], pre[0][1]))
        assert not bool(torch.equal(pre[0][:, 0], pre[0][:, 1]))
        # post の B は行方向にだけ変化する
        assert not bool(torch.equal(post[2][0], post[2][1]))
        assert bool(torch.equal(post[2][:, 0], post[2][:, 1]))
        # 入れ替わっていれば、この 2 つが逆になる
        assert bool((post[0] == POST_RED_BASE).all())
        assert bool((pre[2] == PRE_BLUE).all())

    def test_does_not_transform_the_pixels(self, tmp_path: Path):
        """画素値が合成した PNG のまま.

        pre は R が列番号、G が 64、B が 96。何らかの前処理が挟まると崩れる。
        """

        dataset = _dataset(tmp_path)
        pre, _ = dataset.sample(dataset.index.entries[0].sample_id).view_images[0]

        expected_red = torch.arange(CROP_SIZE_PX, dtype=torch.uint8).expand(
            CROP_SIZE_PX, CROP_SIZE_PX
        )

        assert bool(torch.equal(pre[0], expected_red))
        assert bool((pre[1] == PRE_GREEN_BASE).all())
        assert bool((pre[2] == PRE_BLUE).all())

    def test_returns_the_views_in_the_order_the_entry_lists_them(self, tmp_path: Path):
        """画像の並びが entry の view 列と一致する.

        合成画像は pre の G と post の R へ view 番号を足してあるので、並びが崩れたら 画素で分かる。view
        数が変わっても平均 pooling が成立するのは view が対等だから だが、pre と post の対応が崩れると別の
        view 同士を比べることになる。
        """

        dataset = _dataset(tmp_path)
        entry = dataset.index.entries[0]

        sample = dataset.sample(entry.sample_id)

        assert [view.number for view in entry.views] == list(range(VIEW_COUNT))
        for view, (pre, post) in zip(entry.views, sample.view_images, strict=True):
            assert bool((pre[1] == PRE_GREEN_BASE + view.number).all())
            assert bool((post[0] == POST_RED_BASE + view.number).all())
            assert bool((post[1] == POST_GREEN).all())

    def test_reads_a_blank_cell_the_same_way(self, tmp_path: Path):
        """塗布しない cell も同じ経路で読む.

        真値 0 の sample を特別扱いしない。
        """

        dataset = _dataset(tmp_path)
        blank = next(entry for entry in dataset.index.entries if entry.is_blank)

        sample = dataset.sample(blank.sample_id)

        assert len(sample.view_images) == VIEW_COUNT

    def test_rejects_a_sample_id_that_is_not_in_the_index(self, tmp_path: Path):
        """登録されていない ID は呼び出し側の不変条件違反なので例外にする."""

        dataset = _dataset(tmp_path)

        with pytest.raises(ValueError, match="sample_id"):
            dataset.sample("unknown:000001")
