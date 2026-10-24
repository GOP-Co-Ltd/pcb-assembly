"""`web.api.backend.open_backend` の仕様テスト.

- fake の機体（`Env.fake`）のカメラは、選択中の PCB を基準点どおりに置いた基板を映し、PCB を選び直すと映し直す
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import attrs
import pytest

from tests.web.api.conftest import COPPER_PCB_FIXTURE
from web.api.backend import Backend, open_backend
from web.api.env import Env


class TestFakeCamera:
    """Fake の機体のカメラは、選択中の PCB を基準点どおりに置いた基板を映す."""

    @pytest.fixture
    def fake_backend(self, webui_settings: Env) -> Iterator[Backend]:
        backend = open_backend(attrs.evolve(webui_settings, fake=True))
        yield backend
        backend.close()

    @staticmethod
    def _center_at_reference(backend: Backend) -> int:
        """基準点の真上から撮った画像中心の明るさ."""
        hub = backend.devices.frame_hub()
        hub.start()
        camera = hub.subscribe()
        reference = backend.machine_config.machine().reference_point
        backend.devices.klipper().send_gcode(
            f"G28\nG90\nG1 X{reference.x} Y{reference.y}"
        )
        image = camera.capture().numpy()
        return int(image[image.shape[0] // 2, image.shape[1] // 2, 0])

    def test_selecting_a_pcb_puts_its_top_left_marker_on_the_reference_point(
        self, fake_backend: Backend, webui_settings: Env
    ):
        empty = self._center_at_reference(fake_backend)
        shutil.copy(COPPER_PCB_FIXTURE, webui_settings.pcb_browse_root)

        fake_backend.selection.select(Path(COPPER_PCB_FIXTURE.name))

        assert self._center_at_reference(fake_backend) > empty
