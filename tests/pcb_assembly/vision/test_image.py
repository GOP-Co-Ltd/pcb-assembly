from pathlib import Path

import numpy as np
import pytest

from pcb_assembly.vision import Image, safe_move_distance


class TestImage:
    """Imageクラスのテスト."""

    def test_width_and_height(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)

        image = Image(arr)

        assert image.width == 640
        assert image.height == 480

    def test_size(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)

        image = Image(arr)

        assert image.size == (640, 480)

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

    def test_crop_center(self):
        arr = np.zeros((100, 200, 3), dtype=np.uint8)
        arr[40:60, 80:120] = 255  # 中央に白い領域を配置
        image = Image(arr)

        cropped = image.crop_center((40, 20))

        assert cropped.size == (40, 20)
        assert np.all(cropped.numpy() == 255)


class TestSafeMoveDistance:
    """safe_move_distance関数のテスト."""

    @pytest.mark.parametrize(
        ("roi_size", "margin", "expected"),
        [
            ((100.0, 200.0), 0.2, 40.0),  # 短辺を使用
            ((200.0, 100.0), 0.2, 40.0),  # 短辺を使用（逆順）
            ((100.0, 100.0), 0.2, 40.0),  # 正方形
            ((100.0, 100.0), 0.5, 25.0),  # カスタムマージン
            ((100.0, 100.0), 0.0, 50.0),  # マージンなし
        ],
    )
    def test_safe_move_distance(self, roi_size, margin, expected):
        result = safe_move_distance(roi_size, margin=margin)

        assert result == expected
