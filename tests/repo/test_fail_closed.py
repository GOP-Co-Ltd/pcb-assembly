"""テストから実機の Klipper・サーボとサービスに届かない契約テスト.

テストは本番機の上でも走るので、root conftest がテストごとに実機への経路を塞ぐ。
本番機にはコントローラもつながっているので、人の操作がテストの結果を変えないよう、ゲームパッドを開く口も塞ぐ。
"""

import subprocess
from pathlib import Path

import pytest

from pcbasm import hal
from web.selfupdate.settings import UpdateSettings


class TestFailClosed:
    def test_open_klipper_fails_the_test(self):
        with pytest.raises(pytest.fail.Exception):
            hal.open_klipper("127.0.0.1", 7126)

    def test_open_servo_bus_fails_the_test(self, tmp_path: Path):
        # 塞がっていなくても、存在しないパスなので実機のサーボには届かない
        with pytest.raises(pytest.fail.Exception):
            hal.open_servo_bus(str(tmp_path / "no-such-tty"))

    def test_open_gamepad_fails_the_test(self):
        # 塞がっていなければ、本番機につながっている実機のコントローラを読んでしまう
        with pytest.raises(pytest.fail.Exception):
            hal.open_gamepad()

    @pytest.mark.parametrize("field", ["systemctl_bin", "sudo_bin"])
    def test_update_settings_default_bin_refuses_to_run(self, field: str):
        # 塞がっていなければ実物の --version が成功するだけで、サービスには触れない
        result = subprocess.run(
            [getattr(UpdateSettings(), field), "--version"],
            capture_output=True,
            timeout=10,
        )

        assert result.returncode != 0
