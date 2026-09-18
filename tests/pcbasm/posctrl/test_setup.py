"""Control/setup モジュールのテスト.

Phase 4（memory/agents/implementation-planner/webui-phase4.md §1）で posctrl
から cv2 表示を全廃し frame_sink 注入に統一した:

- OffsetObserver: ``window_name`` 全廃。``frame_sink``（None なら配信なし）へ
  observe 成功時に注釈付き画像を 1 枚送る
- machine_session: 終了時の退避は park_or_present へ委譲
  （nozzle-cap-parking 計画書「呼び出し 3 箇所の差し替え」節:
  ``machine_session(klipper, machine)`` の 2 引数に破壊的変更）

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）を使う。
"""

from contextlib import nullcontext

import cv2
import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm.config import Machine
from pcbasm.geometry import Point2d
from pcbasm.posctrl.setup import (
    CircleDetectionError,
    OffsetObserver,
    machine_session,
)
from pcbasm.vision import CircleDetector, Image
from tests.helpers import TESTING_DATA_DIR, FakeCamera


def _circle_image(offset_x: int = 10) -> Image:
    image = np.full((200, 200, 3), 255, dtype=np.uint8)
    cv2.circle(image, (100 + offset_x, 100), 15, (0, 0, 0), -1)
    return Image(image)


def _blank_image() -> Image:
    return Image(np.full((200, 200, 3), 255, dtype=np.uint8))


class TestOffsetObserver:
    """OffsetObserver の observe() 契約と frame_sink 配信のテスト."""

    @pytest.fixture
    def detector(self) -> CircleDetector:
        return CircleDetector(
            pixel_per_mm=10.0,
            target_diameter_mm=3.0,
            diameter_tolerance_mm=1.0,
            crop_size=(200, 200),
        )

    def test_returns_mean_mm_shift_transform_without_frame_sink(self, detector):
        """frame_sink=None（既定）でも observe は mean_mm の Shift を返す.

        observer 契約統一（observe() -> Transform、想定→観測）。原点に適用すると mean_mm
        に一致する。表示なしで例外も出ない。
        """
        camera = FakeCamera([_circle_image()])
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            sample_count=3,
            minimum_sample_count=1,
        )

        transform = observer.observe()

        offset = transform.apply(Point2d(0.0, 0.0))
        assert offset.x == pytest.approx(1.0, abs=0.2)
        assert offset.y == pytest.approx(0.0, abs=0.2)

    def test_success_delivers_one_annotated_frame_to_frame_sink(self, detector):
        """Observe 成功時、frame_sink へ注釈付き画像が 1 枚届く."""
        frames: list[Image] = []
        camera = FakeCamera([_circle_image()])
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            frame_sink=frames.append,
            sample_count=3,
            minimum_sample_count=1,
        )

        observer.observe()

        assert len(frames) == 1
        assert frames[0].size == (200, 200)

    def test_retries_after_too_few_detections(self, detector):
        """必要検出数に届かないバッチの次の試行で再取得する."""
        camera = FakeCamera(
            [
                _circle_image(),
                _blank_image(),
                _circle_image(),
                _circle_image(),
            ]
        )
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            sample_count=2,
            minimum_sample_count=2,
            max_attempts=2,
        )

        transform = observer.observe()

        offset = transform.apply(Point2d(0.0, 0.0))
        assert offset.x == pytest.approx(1.0, abs=0.2)

    def test_raises_after_all_detection_attempts_without_sending_frame(self, detector):
        """全試行で検出品質を満たさない場合は専用例外を送出する."""
        frames: list[Image] = []
        camera = FakeCamera([_blank_image()])
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            frame_sink=frames.append,
            sample_count=2,
            minimum_sample_count=2,
            max_attempts=2,
        )

        with pytest.raises(CircleDetectionError) as exc_info:
            observer.observe()

        assert "2回" in str(exc_info.value)
        assert frames == []

    def test_raises_when_detected_positions_are_too_variable(self, detector):
        """検出数を満たしても標準偏差上限を超える観測は採用しない."""
        camera = FakeCamera([_circle_image(5), _circle_image(25)])
        observer = OffsetObserver(
            detector=detector,
            camera=camera,
            crop_size=(200, 200),
            sample_count=2,
            minimum_sample_count=2,
            max_standard_deviation_mm=0.1,
        )

        with pytest.raises(CircleDetectionError) as exc_info:
            observer.observe()

        assert "標準偏差" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"sample_count": 0}, "sample_count"),
            (
                {"sample_count": 2, "minimum_sample_count": 3},
                "minimum_sample_count",
            ),
            ({"max_attempts": 0}, "max_attempts"),
            ({"retry_sec": -0.1}, "retry_sec"),
            ({"max_standard_deviation_mm": 0.0}, "max_standard_deviation_mm"),
        ],
    )
    def test_rejects_invalid_quality_settings(self, detector, overrides, expected):
        with pytest.raises(ValueError) as exc_info:
            OffsetObserver(
                detector=detector,
                camera=FakeCamera([_circle_image()]),
                crop_size=(200, 200),
                **{"sample_count": 3, "minimum_sample_count": 1, **overrides},
            )

        assert expected in str(exc_info.value)


class TestMachineSession:
    """machine_session のテスト（終了処理のみをピン）.

    nozzle-cap-parking 計画書「呼び出し 3 箇所の差し替え」節が契約: 終了時の退避判断（キャップ駐機 /
    PRESENT フォールバック）は park_or_present に集約されるため、ここでは委譲だけをピンする。
    """

    @pytest.mark.parametrize(
        "raises", [False, True], ids=["normal-exit", "on-exception"]
    )
    def test_delegates_to_park_or_present_on_exit(
        self, mocker: MockerFixture, raises: bool
    ):
        """例外の有無によらず park_or_present(klipper, machine) が 1 回呼ばれる."""
        park = mocker.patch("pcbasm.posctrl.setup.park_or_present")
        klipper = mocker.Mock()
        machine = Machine(TESTING_DATA_DIR / "machine.toml")
        expected = pytest.raises(ValueError, match="test error")

        with expected if raises else nullcontext():
            with machine_session(klipper, machine):
                if raises:
                    raise ValueError("test error")

        park.assert_called_once_with(klipper, machine)
