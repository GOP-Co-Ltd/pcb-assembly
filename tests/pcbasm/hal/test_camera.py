import pytest

from pcbasm.hal.camera import CameraInfo, Resolution, create_camera


class TestCameraInfo:
    """CameraInfoクラスのテスト."""

    @pytest.mark.parametrize(
        ("format_name", "query_resolution", "expected"),
        [
            # フォーマットと解像度が一致する場合
            ("MJPG", Resolution(1280, 720, 30.0), True),
            # フォーマットが存在しない場合
            ("YUYV", Resolution(1280, 720, 30.0), False),
            # 解像度が存在しない場合
            ("MJPG", Resolution(1920, 1080, 30.0), False),
        ],
    )
    def test_has_format(self, format_name, query_resolution, expected):
        resolution_720p = Resolution(width=1280, height=720, fps=30.0)
        info = CameraInfo(name="Test Camera", formats={"MJPG": [resolution_720p]})

        assert info.has_format(format_name, query_resolution) is expected


class TestCreateCamera:
    """create_camera ファクトリ関数のテスト."""

    def test_raises_when_backend_unknown(self):
        with pytest.raises(ValueError, match="未知のバックエンド"):
            create_camera(backend="unknown")
