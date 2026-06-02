import numpy as np

from pcbasm.geometry import Point2d
from pcbasm.vision import Image, draw_overlay


class TestDrawOverlay:
    """draw_overlay関数のテスト."""

    def test_output_shape_matches_input(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        image = Image(arr)

        result = draw_overlay(image, crop_size=(200, 200))

        assert result.width == 640
        assert result.height == 480

    def test_crosshair_is_drawn_at_center(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        image = Image(arr)
        cx, cy = 320, 240

        result = draw_overlay(image, crop_size=(100, 100))

        result_arr = result.numpy()
        assert result_arr[cy, cx, 1] > 0  # 緑チャネルが描画されている

    def test_roi_rectangle_is_drawn(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        image = Image(arr)
        cx, cy = 320, 240

        result = draw_overlay(image, crop_size=(200, 100))

        result_arr = result.numpy()
        # 矩形の上辺のピクセルが描画されている
        top_y = cy - 50
        assert result_arr[top_y, cx, 1] > 0

    def test_offset_text_is_drawn(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        image = Image(arr)
        offset = Point2d(0.5, -0.3)

        result = draw_overlay(image, crop_size=(100, 100), offset=offset)

        result_arr = result.numpy()
        # テキスト描画領域(y=30付近)に緑ピクセルが存在する
        text_region = result_arr[20:40, 10:300, 1]
        assert text_region.sum() > 0

    def test_no_text_without_offset(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        image = Image(arr)

        result = draw_overlay(image, crop_size=(100, 100))

        result_arr = result.numpy()
        # テキスト領域は十字線・矩形から離れた場所を確認
        text_region = result_arr[20:40, 10:100, 1]
        assert text_region.sum() == 0
