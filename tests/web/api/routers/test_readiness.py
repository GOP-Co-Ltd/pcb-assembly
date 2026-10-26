"""`web.api.routers.readiness` の仕様テスト.

- GET /api/readiness は立ち上げの各段の状態を、手順の順に返す（判定は ``pcbasm.commissioning`` が持つ）
- 装置診断の段は、装置診断ジョブの直近の結果で判定する（不合格で終わったジョブの結果も使う）
- 直近の結果を読めなくても 200 を返し、装置診断の段でやり直しを促す
- 設定エラー（degraded）の間も 200 を返す（チェックリストとトップバーを描けるように）
"""

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from pcbasm.commissioning import STEPS
from pcbasm.hal.fake import FAKE_MACHINE_PRINTER_CONFIG, FakeKlipper
from tests.support.polling import wait_until
from web.api.env import Env


def _readiness(client: TestClient) -> dict:
    response = client.get("/api/readiness")
    assert response.status_code == 200
    return response.json()


def _step(client: TestClient, name: str) -> dict:
    return next(step for step in _readiness(client)["steps"] if step["name"] == name)


def _job_status(client: TestClient) -> str:
    return client.get("/api/jobs/current").json()["job"]["status"]


class TestReadinessApi:
    def test_returns_steps_in_commissioning_order(self, client: TestClient):
        body = _readiness(client)

        assert [step["name"] for step in body["steps"]] == [s.name for s in STEPS]
        assert body["steps"][1] == {
            "name": "machine_check",
            "label": "装置診断",
            "state": "todo",
            "state_label": "未完了",
            "detail": "まだ診断していません",
            "redo_when": STEPS[1].redo_when,
        }
        assert body["complete"] is False
        assert body["label"] == "立ち上げ 4/7"

    def test_machine_check_job_completes_the_step(
        self,
        fake_camera_client: TestClient,
        mount_klipper: Callable[[FakeKlipper], None],
    ):
        mount_klipper(FakeKlipper(FAKE_MACHINE_PRINTER_CONFIG))

        started = fake_camera_client.post(
            "/api/jobs/machine_check", json={"params": {}}
        )
        assert started.status_code == 201
        wait_until(lambda: _job_status(fake_camera_client) == "succeeded")

        step = _step(fake_camera_client, "machine_check")
        assert (step["state"], step["detail"]) == ("done", STEPS[1].done_when)

    def test_failed_machine_check_job_names_the_failed_items(
        self,
        fake_camera_client: TestClient,
        mount_klipper: Callable[[FakeKlipper], None],
    ):
        config = dict(FAKE_MACHINE_PRINTER_CONFIG)
        del config["load_cell_probe"]
        mount_klipper(FakeKlipper(config))

        started = fake_camera_client.post(
            "/api/jobs/machine_check", json={"params": {}}
        )
        assert started.status_code == 201
        wait_until(lambda: _job_status(fake_camera_client) == "failed")

        step = _step(fake_camera_client, "machine_check")
        assert (step["state"], step["detail"]) == (
            "todo",
            "直近の診断で不合格: プローブ",
        )

    def test_answers_while_the_last_result_is_unreadable(
        self, client: TestClient, webui_settings: Env
    ):
        webui_settings.machine_check_file.parent.mkdir(parents=True, exist_ok=True)
        webui_settings.machine_check_file.write_text("[{", encoding="utf-8")

        step = _step(client, "machine_check")

        assert (step["state"], step["detail"]) == (
            "todo",
            "直近の結果 machine_check.json を読めません。装置診断をやり直してください",
        )

    def test_answers_while_machine_toml_is_broken(
        self, client: TestClient, config_dir: Path
    ):
        (config_dir / "machine.toml").write_text("[camera\n")

        step = _step(client, "camera_calibration")

        assert (step["state"], step["detail"]) == (
            "todo",
            "machine.toml を読めないため判定できません",
        )
