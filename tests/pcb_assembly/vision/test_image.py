from pathlib import Path

import numpy as np
import pytest

from pcb_assembly.vision import Image


class TestImage:
    """Imageクラスのテスト."""

    def test_width_and_height(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)

        image = Image(arr)

        assert image.width == 640
        assert image.height == 480

    def test_numpy_returns_internal_array(self):
        arr = np.zeros((100, 100, 3), dtype=np.uint8)
        image = Image(arr)

        result = image.numpy()

        assert result.shape == (100, 100, 3)
        assert result.dtype == np.uint8

    def test_copy_returns_independent_instance(self):
        arr = np.zeros((100, 100, 3), dtype=np.uint8)
        image = Image(arr)

        copied = image.copy()
        copied.numpy()[0, 0, 0] = 255

        assert image.numpy()[0, 0, 0] == 0

    @pytest.mark.parametrize(
        "input_shape",
        [
            (100, 100),  # grayscale
            (100, 100, 3),  # BGR
            (100, 100, 4),  # BGRA
        ],
    )
    def test_init_converts_to_bgr(self, input_shape: tuple[int, ...]):
        arr = np.zeros(input_shape, dtype=np.uint8)

        image = Image(arr)

        assert image.numpy().shape == (100, 100, 3)

    def test_init_raises_on_invalid_shape(self):
        arr = np.zeros((100, 100, 5), dtype=np.uint8)

        with pytest.raises(ValueError, match="不正な画像形状"):
            Image(arr)

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        arr = np.full((100, 100, 3), 128, dtype=np.uint8)
        image = Image(arr)
        path = tmp_path / "test.png"

        image.save(path)
        loaded = Image.load(path)

        assert np.array_equal(image.numpy(), loaded.numpy())

    def test_load_raises_on_missing_file(self, tmp_path: Path):
        path = tmp_path / "nonexistent.png"

        with pytest.raises(FileNotFoundError, match="画像を読み込めません"):
            Image.load(path)
