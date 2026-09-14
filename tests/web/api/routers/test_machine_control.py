"""`web.api.routers.machine_control` の仕様テスト.

計画書「routers」節 + spec §6 マシン操作パネル:

- 400: パラメータ不足（jog の axis/distance 欠落）/ focus_z 不可（z_position なし）/ limits 超過
- 409: machine_lock 取得失敗（detail に owner）
- 502: Moonraker 接続不能（テスト用 config の port 7126 への実接続で検証、モック不使用）
- 200: 実機（実 Moonraker）での操作完了 → `@mark_hardware`（ユーザー実行）

Phase 3 追記（計画書 webui-phase3.md「既存ルーター・app への変更」節）:

- action="gcode": gcode 欠落・空文字は 400、送信成功系は実機区分

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- action="move_to_cap" は `AppState.nozzle_cap()` を読む。x/y/z が揃っていない
  `[paste_dispenser.nozzle_cap]` も「未記録」として 400 で断る（500 にしない）
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import mark_hardware
from web.api.state import AppState


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


class TestGcodeAction:
    """Action="gcode"（Phase 3: dev タブの任意 G-code 送信）."""

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

    @mark_hardware
    def test_gcode_send_returns_status(self, real_client: TestClient):
        response = real_client.post(
            "/api/machine-control", json={"action": "gcode", "gcode": "M400"}
        )

        assert response.status_code == 200
        assert response.json()["connected"] is True


class TestMoveToCap:
    """Action="move_to_cap"（nozzle-cap-parking 計画書「API 契約」節）.

    キャップ未記録は 400。記録済みなら G-code 送信まで到達し、テスト用 config の Klipper（port 7126
    非リッスン）で 502 になる = バリデーション通過の証明。
    """

    def test_unrecorded_cap_returns_400(self, client: TestClient):
        response = client.post("/api/machine-control", json={"action": "move_to_cap"})

        assert response.status_code == 400
        assert "ノズルキャップ" in response.text

    def test_partially_recorded_cap_returns_400(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """X だけ保存された `[paste_dispenser.nozzle_cap]` は「未記録」として 400 で断る（MR2）.

        座標の欠けたテーブルは「未記録」として扱われるので、未記録と同じ 400 へ落ちる。
        """
        response = client.post("/api/machine-control", json={"action": "move_to_cap"})

        assert response.status_code == 400
        assert response.json()["detail"] == "ノズルキャップ位置が未記録です"

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


class TestMachineControlHardware:
    """実 Moonraker に対する操作。ユーザーが実行する."""

    @mark_hardware
    def test_relax_returns_status(self, real_client: TestClient):
        response = real_client.post("/api/machine-control", json={"action": "relax"})

        assert response.status_code == 200
        assert response.json()["connected"] is True

    @mark_hardware
    def test_jog_unhomed_returns_502(self, real_client: TestClient):
        # M84（relax）はステッパーを無効化し homed 状態をクリアする
        assert (
            real_client.post(
                "/api/machine-control", json={"action": "relax"}
            ).status_code
            == 200
        )

        response = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": 0.1},
        )

        assert response.status_code == 502

    @mark_hardware
    def test_home_then_jog_round_trip(self, real_client: TestClient):
        home = real_client.post("/api/machine-control", json={"action": "home"})
        assert home.status_code == 200
        assert home.json()["homed_axes"] == "xyz"
        start_x = home.json()["position"]["x"]

        forward = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": 0.1},
        )
        assert forward.status_code == 200
        assert forward.json()["position"]["x"] == pytest.approx(start_x + 0.1, abs=0.01)

        back = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": -0.1},
        )
        assert back.status_code == 200
        assert back.json()["position"]["x"] == pytest.approx(start_x, abs=0.01)

    @mark_hardware
    def test_move_beyond_limits_returns_400(self, real_client: TestClient):
        assert (
            real_client.post(
                "/api/machine-control", json={"action": "home"}
            ).status_code
            == 200
        )

        response = real_client.post(
            "/api/machine-control", json={"action": "move", "x": 9999.0}
        )

        assert response.status_code == 400
