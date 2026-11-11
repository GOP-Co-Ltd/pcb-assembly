"""Pnp-motion ファームのステージのテストが共有する fixture（設定と MCU 役は
``tests/support/motion_mcu.py``）."""

from collections.abc import Iterator

import pytest

from pcbasm.hal.mcu.settings import McuMotionSettings
from tests.support.motion_mcu import motion_settings, open_wire
from tests.support.wire_mcu import WireMcu


@pytest.fixture
def mcu() -> Iterator[WireMcu]:
    wire = open_wire()
    yield wire
    wire.close()


@pytest.fixture
def settings(mcu: WireMcu) -> McuMotionSettings:
    return motion_settings(mcu.path)
