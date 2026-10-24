"""`web.api.routers.machine_control` の仕様テスト.

計画書「routers」節 + spec §6 マシン操作パネル:

- 400: パラメータ不足（jog の axis/distance 欠落）/ focus_z 不可（z_position なし）/ limits 超過
- 409: machine_lock 取得失敗（detail に owner）
- 502: Moonraker 接続不能（テスト用 config の port 7126 への実接続で検証、モック不使用）
- 200: 実機での操作完了は受け入れ試験（docs/acceptance.md の A1）で確かめる

Phase 3 追記（計画書 webui-phase3.md「既存ルーター・app への変更」節）:

- action="gcode": gcode 欠落・空文字は 400、送信成功系は受け入れ試験（A1）

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- action="move_to_cap" はキャップ位置が未記録なら 400 で断る

設定エラー（degraded）の契約: x/y/z が揃っていない `[paste_dispenser.nozzle_cap]` は
未記録ではなく設定エラーとして 503 で断り、理由を返す（原点や PRESENT へ動かさない）
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeKlipper
from web.api.settings import Settings
from web.api.state import AppState


@pytest.mark.usefixtures("unreachable_klipper")
class TestMachineControlValidation:
    """パラメータ検証（Moonraker 接続前に 400）."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"action": "jog", "axis": "x"}, id="jog-without-distance"),
            pytest.param({"action": "jog", "distance": 1.0}, id="jog-without-axis"),
            pytest.param({"action": "move"}, id="move-without-axes"),
        ],
    )
    def test_missing_parameters_return_400(
        self, client: TestClient, body: dict[str, object]
    ):
        response = client.post("/api/machine-control", json=body)

        assert response.status_code == 400

    def test_focus_z_without_z_position_returns_400(
        self, client: TestClient, config_dir: Path
    ):
        calibration = config_dir / "ov9281_test_fixture.json"
        data = json.loads(calibration.read_text(encoding="utf-8"))
        data["z_position"] = None
        calibration.write_text(json.dumps(data), encoding="utf-8")

        response = client.post("/api/machine-control", json={"action": "focus_z"})

        assert response.status_code == 400


class TestKlipperErrors:
    """Klipper へ届かない・断られた操作は 502 で理由を返す."""

    def test_refused_send_returns_502_with_moonraker_reason(
        self, client: TestClient, mount_klipper: Callable[[FakeKlipper], None]
    ):
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: True)
        mount_klipper(klipper)

        response = client.post("/api/machine-control", json={"action": "relax"})

        assert response.status_code == 502
        assert response.json() == {"detail": "Bad Request"}


@pytest.mark.usefixtures("unreachable_klipper")
class TestJournal:
    """REST の操作は当日の G-code ジャーナルに残る."""

    def test_send_is_journaled_even_when_moonraker_is_down(
        self, client: TestClient, webui_settings: Settings
    ):
        response = client.post(
            "/api/machine-control", json={"action": "home", "axes": ["x"]}
        )

        assert response.status_code == 502
        [journal] = webui_settings.journal_dir.glob("*-rest.gcode")
        assert (
            journal.read_text(encoding="utf-8") == "# send timeout=60.0\nG28 X\nM400\n"
        )


class TestGcodeAction:
    """Action="gcode"（Phase 3: dev タブの任意 G-code 送信）."""

    @pytest.mark.usefixtures("unreachable_klipper")
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"action": "gcode"}, id="missing-gcode"),
            pytest.param({"action": "gcode", "gcode": ""}, id="empty-gcode"),
        ],
    )
    def test_blank_gcode_returns_400(self, client: TestClient, body: dict[str, object]):
        response = client.post("/api/machine-control", json=body)

        assert response.status_code == 400


@pytest.mark.usefixtures("unreachable_klipper")
class TestMoveToCap:
    """Action="move_to_cap"（nozzle-cap-parking 計画書「API 契約」節）.

    キャップ未記録は 400。記録済みなら G-code 送信まで到達し、テスト用 config の Klipper（port 7126
    非リッスン）で 502 になる = バリデーション通過の証明。
    """

    def test_unrecorded_cap_returns_400(self, client: TestClient):
        response = client.post("/api/machine-control", json={"action": "move_to_cap"})

        assert response.status_code == 400
        assert "ノズルキャップ" in response.text

    def test_partially_recorded_cap_is_refused_as_a_config_error(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """X だけ書かれた `[paste_dispenser.nozzle_cap]` は設定エラーとして 503 で断る.

        未記録として扱って原点や PRESENT へ動かさない。理由は応答に載せる。
        """
        response = client.post("/api/machine-control", json={"action": "move_to_cap"})

        assert response.status_code == 503
        assert "x / y / z の 3 つ" in response.json()["detail"]

    def test_recorded_cap_passes_validation_and_returns_502(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )

        response = client.post("/api/machine-control", json={"action": "move_to_cap"})

        assert response.status_code == 502


class TestMachineControlExclusion:
    """ジョブ共有ロックとの排他."""

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/machine-control", json={"action": "relax"})

        assert response.status_code == 409
        assert "pytest-job" in response.text


@pytest.mark.usefixtures("unreachable_klipper")
class TestMachineControlMoonrakerDown:
    """Moonraker 不達（port 7126 = 非リッスン、実接続で検証）."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"action": "home"}, id="home"),
            pytest.param({"action": "relax"}, id="relax"),
            pytest.param({"action": "gcode", "gcode": "M400"}, id="gcode"),
        ],
    )
    def test_unreachable_moonraker_returns_502(
        self, client: TestClient, body: dict[str, object]
    ):
        response = client.post("/api/machine-control", json=body)

        assert response.status_code == 502
