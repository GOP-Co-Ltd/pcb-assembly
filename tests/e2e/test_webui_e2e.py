"""WebUI フルスタック E2E（実 uvicorn + 実 HTTP / WebSocket / MJPEG）.

`make test-e2e` で実行する。TestClient では検証しづらい以下を実ネットワーク経由で確認する:

- 無限 MJPEG ストリーム（TestClient は完全受信まで返らずハングする）
- WebSocket のイベント往復（ジョブ起動 → ログ/進捗/プロンプト → 完走 → 設定反映）

ジョブ通しの題材には hidden の ``job_demo``（uses_machine=False・実機不要）を使う。
log / progress / prompt / prompt_resolved / apply を一通り通すための検証用ジョブで、
prompt 2 回（confirm → number）に応答すると SUCCEEDED + apply ペイロードを返す。
"""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

import httpx
from websockets.sync.client import connect

from tests.e2e.conftest import LiveServer
from tests.webui.conftest import COPPER_PCB_FIXTURE, decode_jpeg, jpeg_payload

_TERMINAL = ("succeeded", "failed", "aborted")
_HTTP_TIMEOUT = 10.0
_WS_TIMEOUT = 30.0


def _respond_prompt(
    ws: Any, prompt: dict[str, Any], answered: set[str], number_answer: float
) -> None:
    """Prompt（または job_status.pending_prompt）へ kind に応じて 1 度だけ応答する."""
    prompt_id = prompt["id"]
    if prompt_id in answered:
        return
    answer: bool | float = True if prompt["kind"] == "confirm" else number_answer
    ws.send(
        json.dumps({"type": "respond_prompt", "prompt_id": prompt_id, "answer": answer})
    )
    answered.add(prompt_id)


def _drive_job_demo(
    ws: Any, *, number_answer: float
) -> tuple[dict[str, Any], set[str]]:
    """WS イベントを受信駆動で処理し、終端 job_status とその間に観測した type 集合を返す.

    prompt は confirm=True / number=number_answer で応答する。job_status の
    pending_prompt 経由でも応答できるよう二重化し、prompt_id で重複応答を防ぐ。
    """
    answered: set[str] = set()
    seen_types: set[str] = set()
    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        seen_types.add(event["type"])
        if event["type"] == "prompt":
            _respond_prompt(ws, event["prompt"], answered, number_answer)
        elif event["type"] == "job_status":
            job = event["job"]
            pending = job.get("pending_prompt")
            if pending is not None:
                _respond_prompt(ws, pending, answered, number_answer)
            if job["status"] in _TERMINAL:
                return job, seen_types


def _read_mjpeg(base_url: str, path: str, boundary_count: int = 2) -> bytes:
    """MJPEG ストリームを boundary_count 個の boundary 行まで読んで切断する."""
    with httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT) as client:
        with client.stream("GET", path) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == (
                "multipart/x-mixed-replace; boundary=frame"
            )
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if data.count(b"--frame") >= boundary_count:
                    break
    return data


class TestHttpRoutes:
    """実サーバーへの基本的な HTTP 経路."""

    def test_root_page_is_served(self, live_server: LiveServer):
        # / は既定タブへ 307 リダイレクトする。ブラウザ同様に追従する
        response = httpx.get(
            f"{live_server.base_url}/", timeout=_HTTP_TIMEOUT, follow_redirects=True
        )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_state_reports_selected_machine(self, live_server: LiveServer):
        response = httpx.get(f"{live_server.base_url}/api/state", timeout=_HTTP_TIMEOUT)

        assert response.status_code == 200
        assert response.json()["machine"] == "kurousagi"

    def test_machines_lists_fixtures(self, live_server: LiveServer):
        response = httpx.get(
            f"{live_server.base_url}/api/machines", timeout=_HTTP_TIMEOUT
        )

        body = response.json()
        assert body["selected"] == "kurousagi"
        assert {"kurousagi", "test-fixture"} <= set(body["machines"])


class TestPreviewOverRealHttp:
    """Fake カメラのプレビューを実 HTTP で取得する（snapshot / 無限 stream）."""

    def test_snapshot_returns_decodable_jpeg(self, live_server: LiveServer):
        response = httpx.get(
            f"{live_server.base_url}/api/preview/snapshot", timeout=_HTTP_TIMEOUT
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert decode_jpeg(response.content) is not None

    def test_stream_yields_decodable_mjpeg_frames(self, live_server: LiveServer):
        data = _read_mjpeg(
            live_server.base_url, "/api/preview/stream?overlay=crosshair"
        )

        assert b"Content-Type: image/jpeg" in data
        frame = decode_jpeg(jpeg_payload(data.split(b"--frame")[1]))
        assert frame is not None


class TestJobLifecycleOverWebSocket:
    """WS /api/ws のイベント往復とジョブ → 設定反映の通し検証."""

    def test_job_demo_completes_and_applies(self, live_server: LiveServer):
        with connect(f"{live_server.ws_url}/api/ws") as ws:
            response = httpx.post(
                f"{live_server.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 2, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201

            job, seen_types = _drive_job_demo(ws, number_answer=77.0)

        assert job["status"] == "succeeded"
        assert job["apply_available"] is True
        assert "77" in job["result"]["summary"]
        # ジョブの全イベント種別が実ネットワーク経由で届いている
        assert {"job_status", "log", "progress", "prompt", "prompt_resolved"} <= (
            seen_types
        )

        # 直近 SUCCEEDED ジョブの計測値を設定へ反映する（apply 往復）
        apply = httpx.post(
            f"{live_server.base_url}/api/jobs/last/apply", timeout=_HTTP_TIMEOUT
        )
        assert apply.status_code == 200
        assert apply.json()["applied"] == {"paste_dispenser.pad_align.canny_low": 77.0}

        # 隔離した tmp の machine.toml に書かれている（実機設定は汚していない）
        machine_toml = (
            live_server.settings.configs_root / "kurousagi" / "machine.toml"
        ).read_text()
        assert "canny_low = 77" in machine_toml


class TestPromptDialogOverBrowser:
    """実ブラウザ上の prompt modal 表示。"""

    def test_confirm_dialog_uses_custom_button_labels(
        self, live_server: LiveServer, browser_page
    ):
        shutil.copy(
            COPPER_PCB_FIXTURE,
            live_server.settings.pcb_browse_root / "led_blinker.kicad_pcb",
        )
        select = httpx.put(
            f"{live_server.base_url}/api/pcb-file",
            json={"path": "led_blinker.kicad_pcb"},
            timeout=_HTTP_TIMEOUT,
        )
        assert select.status_code == 200, select.text

        browser_page.goto(
            f"{live_server.base_url}/pasting/height_plane",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#job-console").wait_for(state="visible", timeout=10_000)

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/height_plane", timeout=_HTTP_TIMEOUT
        )
        assert start.status_code == 201, start.text

        dialog = browser_page.locator("#jc-prompt")
        dialog.wait_for(state="visible", timeout=30_000)
        ok_button = browser_page.locator("#jc-prompt-ok")
        no_button = browser_page.locator("#jc-prompt-no")
        ok_button.wait_for(state="visible", timeout=10_000)
        no_button.wait_for(state="visible", timeout=10_000)

        assert ok_button.inner_text() == "続行"
        assert no_button.inner_text() == "中止"

        no_button.click()
        deadline = time.monotonic() + 10.0
        while True:
            current = httpx.get(
                f"{live_server.base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT
            ).json()["job"]
            if current is not None and current["status"] == "aborted":
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"height_plane が aborted にならない: {current}")
            time.sleep(0.05)


class TestSettingsOverBrowser:
    """設定画面の実ブラウザ操作."""

    def test_probe_shift_two_fields_autosave(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        x_input = browser_page.locator(
            'input[data-pair-key="probe.shift"][data-pair-index="0"]'
        )
        y_input = browser_page.locator(
            'input[data-pair-key="probe.shift"][data-pair-index="1"]'
        )
        x_input.wait_for(state="visible", timeout=10_000)
        y_input.wait_for(state="visible", timeout=10_000)

        assert x_input.input_value() == "-0.5"
        assert y_input.input_value() == "0.0"

        x_input.fill("0.25")
        y_input.fill("-0.75")

        deadline = time.monotonic() + 5.0
        while True:
            response = httpx.get(
                f"{live_server.base_url}/api/settings/machine",
                timeout=_HTTP_TIMEOUT,
            )
            fields = {field["key"]: field for field in response.json()["fields"]}
            if fields["probe.shift"]["value"] == [0.25, -0.75]:
                break
            if time.monotonic() > deadline:
                raise AssertionError("probe.shift が保存されない")
            time.sleep(0.05)

    def test_setting_label_does_not_focus_input(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        label = browser_page.locator(".settings-label").nth(0)
        label.wait_for(state="visible", timeout=10_000)

        label.click()

        active_tag = browser_page.evaluate("document.activeElement?.tagName")
        assert active_tag != "INPUT"


class TestPadConfigOverRealHttp:
    """Pad-config API を実 HTTP で叩く（PCB 選択 → GET → PATCH → 永続化）."""

    def test_pad_config_get_patch_roundtrip(self, live_server: LiveServer):
        # led_blinker を pcb_browse_root へ置いて選択する
        shutil.copy(
            COPPER_PCB_FIXTURE,
            live_server.settings.pcb_browse_root / "led_blinker.kicad_pcb",
        )
        select = httpx.put(
            f"{live_server.base_url}/api/pcb-file",
            json={"path": "led_blinker.kicad_pcb"},
            timeout=_HTTP_TIMEOUT,
        )
        assert select.status_code == 200, select.text

        # GET: 実 PCB から outline / pads / 階層ツリー / defaults を返す
        config = httpx.get(
            f"{live_server.base_url}/api/pasting/pad-config", timeout=_HTTP_TIMEOUT
        ).json()
        assert config["pcb_file"].endswith("led_blinker.kicad_pcb")
        assert config["tree"]["id"] == "L0"
        assert config["pads"]
        first = config["pads"][0]
        assert first["enabled"] is True  # 既定は全 pad 有効

        # PATCH pads: 1 pad を無効化 → affected_pads に反映
        patch = httpx.patch(
            f"{live_server.base_url}/api/pasting/pad-config/pads",
            json={"ids": [first["id"]], "enabled": False},
            timeout=_HTTP_TIMEOUT,
        )
        assert patch.status_code == 200
        affected = {p["id"]: p for p in patch.json()["affected_pads"]}
        assert affected[first["id"]]["enabled"] is False

        # 再 GET: 無効が基板ごと設定として永続化されている
        reread = httpx.get(
            f"{live_server.base_url}/api/pasting/pad-config", timeout=_HTTP_TIMEOUT
        ).json()
        repad = next(p for p in reread["pads"] if p["id"] == first["id"])
        assert repad["enabled"] is False
