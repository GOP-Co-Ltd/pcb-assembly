import numpy as np
import pytest

from pcbasm.geometry import Point2d
from pcbasm.vision import (
    CheckerboardView,
    Image,
    draw_crosshair,
    draw_overlay,
    draw_scan_coverage,
)
from tests.helpers import SyntheticCheckerboardCamera

SCAN_IMAGE_SIZE = (1280, 720)


class TestDrawCrosshair:
    """draw_crosshair関数のテスト."""

    def test_crosshair_is_drawn_at_center_in_place(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        cx, cy = 320, 240

        draw_crosshair(arr)

        assert arr[cy, cx, 1] > 0  # 緑チャネルが中心に描画されている
        assert arr[cy, cx - 30, 1] > 0  # 横線の端
        assert arr[cy - 30, cx, 1] > 0  # 縦線の端

    def test_pixels_outside_crosshair_untouched(self):
        arr = np.zeros((480, 640, 3), dtype=np.uint8)

        draw_crosshair(arr)

        assert arr[10, 10].sum() == 0


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


class TestDrawScanCoverage:
    """draw_scan_coverage: 校正データがどの半径まで届いているかの図.

    計画書 §8 の `corner_coverage.png`。crop を広げたときに校正済み領域から
    出ないかを操作者が判定するための素材。
    """

    @pytest.fixture
    def views(self) -> tuple[CheckerboardView, ...]:
        # 画像レンダリングは要らないので cv2.projectPoints から直接コーナーを作る
        camera = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0), image_size=SCAN_IMAGE_SIZE
        )
        return tuple(
            camera.project_view(position)
            for position in (Point2d(-5.0, -3.0), Point2d(5.0, 3.0))
        )

    def test_output_size_matches_the_requested_frame(
        self, views: tuple[CheckerboardView, ...]
    ):
        result = draw_scan_coverage(SCAN_IMAGE_SIZE, views)

        assert result.size == SCAN_IMAGE_SIZE

    def test_every_corner_is_marked_and_empty_areas_stay_black(
        self, views: tuple[CheckerboardView, ...]
    ):
        result = draw_scan_coverage(SCAN_IMAGE_SIZE, views).numpy()

        for view in views:
            for point in np.asarray(view.corners).reshape(-1, 2):
                assert result[round(point[1]), round(point[0]), 1] > 0
        # 盤の外周（フレーム隅）にはコーナーが無いので背景のまま
        assert result[0:20, 0:20].sum() == 0

    def test_crop_rectangles_are_drawn_only_when_requested(
        self, views: tuple[CheckerboardView, ...]
    ):
        width, height = SCAN_IMAGE_SIZE
        centre_x, centre_y = width // 2, height // 2

        without = draw_scan_coverage(SCAN_IMAGE_SIZE, views).numpy()
        with_crops = draw_scan_coverage(
            SCAN_IMAGE_SIZE, views, crop_sizes=((300, 300), (600, 600))
        ).numpy()

        # コーナー点と十字線は赤成分を持たないので、赤チャネルが矩形の有無を示す
        assert without[:, :, 2].sum() == 0
        assert with_crops[centre_y, centre_x - 150, 2] > 0
        assert with_crops[centre_y, centre_x - 300, 2] > 0
