"""Control/setup モジュールのテスト.

Phase 4（memory/agents/implementation-planner/webui-phase4.md §1）で posctrl
から cv2 表示を全廃し frame_sink 注入に統一した:

- OffsetObserver: ``window_name`` 全廃。``frame_sink``（None なら配信なし）へ
  observe 成功時に注釈付き画像を 1 枚送る
- machine_session: finally は PRESENT 優先、無ければ M84（cv2.destroyAllWindows 削除）

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）を使う。
"""

import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm.geometry import Point2d
from pcbasm.posctrl.setup import OffsetObserver, machine_session
from pcbasm.vision import Image
from pcbasm.vision.detection import OffsetStatistics
from tests.helpers import FakeCamera


class TestOffsetObserver:
    """OffsetObserver の observe() 契約と frame_sink 配信のテスト."""

    @pytest.fixture
    def camera(self) -> FakeCamera:
        return FakeCamera([Image(np.zeros((720, 1280, 3), dtype=np.uint8))])

    @pytest.fixture
    def detector(self, mocker: MockerFixture):
        """Mean (50, -30) px / 100 px/mm の検出統計を返す detector."""
        detector = mocker.Mock()
        detector.detect_with_statistics.return_value = OffsetStatistics(
            mean=Point2d(50.0, -30.0),
            std=Point2d(1.0, 1.0),
            pixel_per_mm=100.0,
            sample_count=30,
        )
        return detector

    def test_returns_mean_mm_shift_transform_without_frame_sink(self, detector, camera):
        """frame_sink=None（既定）でも observe は mean_mm の Shift を返す.

        observer 契約統一（observe() -> Transform、想定→観測）。原点に 適用すると mean_mm
        に一致する。表示なしで例外も出ない。
        """
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            sample_count=10,
        )

        transform = observer.observe()

        offset = transform.apply(Point2d(0.0, 0.0))
        assert offset.x == pytest.approx(0.5)
        assert offset.y == pytest.approx(-0.3)

    def test_success_delivers_one_annotated_frame_to_frame_sink(self, detector, camera):
        """Observe 成功時、frame_sink へ注釈付き画像が 1 枚届く."""
        frames: list[Image] = []
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            frame_sink=frames.append,
            sample_count=10,
        )

        observer.observe()

        assert len(frames) == 1
        assert frames[0].size == (1280, 720)  # カメラフレームと同サイズの合成画像

    def test_raises_on_detection_failure_without_sending_frame(self, detector, camera):
        """検出失敗時は RuntimeError を送出し、frame_sink へは何も送らない."""
        detector.detect_with_statistics.return_value = None
        frames: list[Image] = []
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            frame_sink=frames.append,
        )

        with pytest.raises(RuntimeError, match="検出に失敗しました"):
            observer.observe()

        assert frames == []


class TestMachineSession:
    """machine_session のテスト（終了処理のみをピン。cv2 依存なし）."""

    def test_sends_present_on_exit(self, mocker: MockerFixture):
        """セッション終了時に PRESENT が送信される."""
        klipper = mocker.Mock()

        with machine_session(klipper):
            pass

        klipper.send_present_or_relax.assert_called_once_with()

    def test_sends_present_even_on_exception(self, mocker: MockerFixture):
        """例外発生時でも PRESENT が送信され、例外は伝播する."""
        klipper = mocker.Mock()

        with pytest.raises(ValueError, match="test error"):
            with machine_session(klipper):
                raise ValueError("test error")

        klipper.send_present_or_relax.assert_called_once_with()
