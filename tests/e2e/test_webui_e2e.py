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
from playwright.sync_api import expect
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


def _wait_for_current_job(base_url: str, job_id: str) -> dict[str, Any]:
    """現在ジョブが終端するまで REST 経由で待つ."""
    deadline = time.monotonic() + 60.0
    while True:
        response = httpx.get(f"{base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT)
        assert response.status_code == 200
        job = response.json()["job"]
        if job is not None and job["id"] == job_id and job["status"] in _TERMINAL:
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"ジョブが終端しない: {job}")
        time.sleep(0.05)


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


class TestGenerateRectPcbOverRealHttp:
    """矩形 PCB 生成ジョブを実 HTTP 経由で実行する。"""

    def test_page_and_job_artifact_are_served(self, live_server: LiveServer):
        page = httpx.get(
            f"{live_server.base_url}/pasting/generate_rect_pcb",
            timeout=_HTTP_TIMEOUT,
        )
        assert page.status_code == 200
        assert "job-form" in page.text

        response = httpx.post(
            f"{live_server.base_url}/api/jobs/generate_rect_pcb",
            json={"params": {"width": 12.5, "height": 7.5}},
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 201, response.text

        job = _wait_for_current_job(live_server.base_url, response.json()["job"]["id"])
        assert job["status"] == "succeeded", job.get("error")
        artifacts = job["result"]["artifacts"]
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact["kind"] == "file"
        assert artifact["url"].endswith(".kicad_pcb")

        download = httpx.get(
            f"{live_server.base_url}{artifact['url']}", timeout=_HTTP_TIMEOUT
        )
        assert download.status_code == 200
        assert b"(kicad_pcb" in download.content


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


def _wait_first_prompt(ws: Any) -> dict[str, Any]:
    """最初の prompt イベントを受信して返す（WAITING_INPUT で停止した証跡）.

    job_status の pending_prompt 経由でも捕捉できるよう二重化する。
    """
    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        if event["type"] == "prompt":
            return event["prompt"]
        if event["type"] == "job_status":
            pending = event["job"].get("pending_prompt")
            if pending is not None:
                return pending


class TestRuntimeParamUpdateOverWebSocket:
    """PUT /api/jobs/current/params の実 HTTP + WS 通し検証（job_demo 題材）.

    実行中（prompt 待機中）の out-of-band 反映、固定/未知キー 400、非アクティブ 400 を 実 uvicorn
    で確認する。
    """

    def test_live_update_applies_while_waiting_prompt(self, live_server: LiveServer):
        with connect(f"{live_server.ws_url}/api/ws") as ws:
            response = httpx.post(
                f"{live_server.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201

            # 最初の prompt（confirm）= WAITING_INPUT で停止中。その間に PUT する
            prompt = _wait_first_prompt(ws)
            assert prompt["kind"] == "confirm"

            put = httpx.put(
                f"{live_server.base_url}/api/jobs/current/params",
                json={"values": {"live_value": 42.0}, "persist": False},
                timeout=_HTTP_TIMEOUT,
            )
            assert put.status_code == 200, put.text
            assert put.json()["params"] == {"live_value": 42.0}

            # out-of-band 適用が GET /jobs/current の summary へ即反映される
            current = httpx.get(
                f"{live_server.base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT
            ).json()["job"]
            assert current is not None
            assert current["params"]["live_value"] == 42.0

            # confirm → number に応答してジョブを終端させる
            answered: set[str] = set()
            _respond_prompt(ws, prompt, answered, number_answer=60.0)
            job, _ = _drive_job_demo(ws, number_answer=60.0)

        assert job["status"] == "succeeded"
        # ライブ反映した live_value が log/summary に出る経路の証跡
        assert job["params"]["live_value"] == 42.0

    def test_fixed_or_unknown_key_returns_400(self, live_server: LiveServer):
        with connect(f"{live_server.ws_url}/api/ws") as ws:
            response = httpx.post(
                f"{live_server.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201
            prompt = _wait_first_prompt(ws)

            # steps は runtime_editable=False（固定）→ 400
            fixed = httpx.put(
                f"{live_server.base_url}/api/jobs/current/params",
                json={"values": {"steps": 9}},
                timeout=_HTTP_TIMEOUT,
            )
            assert fixed.status_code == 400, fixed.text

            # 未知キー → 400
            unknown = httpx.put(
                f"{live_server.base_url}/api/jobs/current/params",
                json={"values": {"no_such_param": 1.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert unknown.status_code == 400, unknown.text

            answered: set[str] = set()
            _respond_prompt(ws, prompt, answered, number_answer=60.0)
            job, _ = _drive_job_demo(ws, number_answer=60.0)
        assert job["status"] == "succeeded"

    def test_update_without_active_job_returns_400(self, live_server: LiveServer):
        # ジョブを 1 度も起動していない状態で PUT → 400
        response = httpx.put(
            f"{live_server.base_url}/api/jobs/current/params",
            json={"values": {"live_value": 1.0}},
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 400, response.text

    def test_update_after_terminal_returns_400(self, live_server: LiveServer):
        with connect(f"{live_server.ws_url}/api/ws") as ws:
            response = httpx.post(
                f"{live_server.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201
            job, _ = _drive_job_demo(ws, number_answer=60.0)
        assert job["status"] == "succeeded"

        # 終端後（非アクティブ）の PUT → 400
        response = httpx.put(
            f"{live_server.base_url}/api/jobs/current/params",
            json={"values": {"live_value": 1.0}},
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 400, response.text


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

    def test_enter_key_submits_ok_instead_of_cancel(
        self, live_server: LiveServer, browser_page
    ):
        """Enter の暗黙送信は OK（続行）に落ちる.

        中止が DOM 先頭の submit ボタンだと Enter が中止を押した扱いになり、
        確認や質量入力のたびにジョブ/サブキャリブが勝手に中止されていた。 続行（True）ならセットアップへ進み、Klipper
        不通（port 7126）で failed になる。旧実装（中止が既定）だと即 aborted になっていた。
        """
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

        browser_page.locator("#jc-prompt").wait_for(state="visible", timeout=30_000)
        browser_page.keyboard.press("Enter")

        deadline = time.monotonic() + 120.0
        while True:
            current = httpx.get(
                f"{live_server.base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT
            ).json()["job"]
            if current is not None and current["status"] in _TERMINAL:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"height_plane が終端しない: {current}")
            time.sleep(0.1)
        assert current["status"] == "failed", current


class TestSettingsOverBrowser:
    """設定画面の実ブラウザ操作."""

    def test_probe_lift_height_autosave(self, live_server: LiveServer, browser_page):
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        field = browser_page.locator('input[name="probe.lift_height"]')
        field.wait_for(state="visible", timeout=10_000)

        field.fill("1.25")

        deadline = time.monotonic() + 5.0
        while True:
            response = httpx.get(
                f"{live_server.base_url}/api/settings/machine",
                timeout=_HTTP_TIMEOUT,
            )
            fields = {field["key"]: field for field in response.json()["fields"]}
            if fields["probe.lift_height"]["value"] == 1.25:
                break
            if time.monotonic() > deadline:
                raise AssertionError("probe.lift_height が保存されない")
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


class TestLoadingOverBrowser:
    """ペーストローディング画面の実ブラウザ操作.

    質量キャリブレーション表（初期 rotations_per_ul 等のブートストラップ用）と、 押出/吸引の操作パネル +
    パラメータ同期を持つ。既存値を線引きで補正する dispense_calibration とは用途が別なので併存する。
    """

    def _wait_machine_field(self, base_url: str, key: str, expected: float) -> None:
        """Machine 設定の 1 フィールドが期待値になるまで REST 経由で待つ."""
        deadline = time.monotonic() + 5.0
        while True:
            response = httpx.get(
                f"{base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
            )
            fields = {field["key"]: field for field in response.json()["fields"]}
            if fields[key]["value"] == expected:
                return
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{key} が {expected} に保存されない: {fields[key]}"
                )
            time.sleep(0.05)

    def test_loading_controls_sync_inputs_to_hidden_params(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.2")
        browser_page.locator("#lc-rotations").fill("5")
        browser_page.locator("#lc-rate").fill("0.5")
        browser_page.locator("#lc-accel").fill("0.5")

        # ローディング操作パネルの hidden へ各入力が同期される
        assert browser_page.locator("#param-amount").input_value() == "0.2"
        assert browser_page.locator("#param-rotations").input_value() == "5"
        assert browser_page.locator("#param-rate").input_value() == "0.5"
        assert browser_page.locator("#param-accel").input_value() == "0.5"

    def test_loading_inputs_persist_across_reload(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.33")
        browser_page.locator("#lc-rotations").fill("6.5")
        browser_page.locator("#lc-rate").fill("1.25")
        # 最後の入力が起こす debounce 即保存 POST を待ってからリロードする
        # （「実行」していないので、即保存が効いていなければ値は失われる）
        with browser_page.expect_response(
            lambda r: "/param-defaults" in r.url and r.request.method == "POST"
        ):
            browser_page.locator("#lc-accel").fill("2.5")

        browser_page.reload(wait_until="domcontentloaded")
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        expect(browser_page.locator("#lc-amount")).to_have_value("0.33")
        expect(browser_page.locator("#lc-rotations")).to_have_value("6.5")
        expect(browser_page.locator("#lc-rate")).to_have_value("1.25")
        expect(browser_page.locator("#lc-accel")).to_have_value("2.5")

    def test_mass_calibration_calculates_and_applies_dispense_values(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-mass-calibration").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.2")
        browser_page.locator("#lc-rotations").fill("5")
        browser_page.locator("#lc-rate").fill("0.5")
        browser_page.locator("#lc-accel").fill("0.5")
        browser_page.locator("#lc-mass-mg").fill("10")

        # ローディング操作パネルの hidden へ各入力が同期される
        assert browser_page.locator("#param-amount").input_value() == "0.2"
        assert browser_page.locator("#param-rotations").input_value() == "5"
        assert browser_page.locator("#param-rate").input_value() == "0.5"
        assert browser_page.locator("#param-accel").input_value() == "0.5"

        # 算出値は debounce GET で非同期に届くので Playwright の自動待機で待つ。
        # mass=10, rotations=5, density=3.78 → volume=2.645503, rpu=1.890000,
        # rate/accel = 0.5/1.89 = 0.264550（toFixed(6) 表示）
        expect(browser_page.locator("#lc-volume-ul")).to_have_text("2.645503")
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("1.890000")
        expect(browser_page.locator("#lc-dispense-rate")).to_have_text("0.264550")
        expect(browser_page.locator("#lc-dispense-accel")).to_have_text("0.264550")

        # 個別適用: rotations_per_ul のみ永続化 → 現在値 output が更新される
        browser_page.locator("#lc-apply-rotations-per-ul").click()
        self._wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 1.89
        )
        expect(browser_page.locator("#lc-current-rotations-per-ul")).to_have_text(
            "1.890000"
        )

        # 一括適用: 3 キーがまとめて永続化される
        # 保存値は Number(toFixed(6)) のトリム後（1.89, 0.26455, 0.26455）
        browser_page.locator("#lc-apply-all").click()
        self._wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 1.89
        )
        self._wait_machine_field(
            live_server.base_url, "paste_dispenser.max_dispense_rate", 0.26455
        )
        self._wait_machine_field(
            live_server.base_url, "paste_dispenser.dispense_accel", 0.26455
        )
        expect(browser_page.locator("#lc-current-dispense-rate")).to_have_text(
            "0.264550"
        )
        expect(browser_page.locator("#lc-current-dispense-accel")).to_have_text(
            "0.264550"
        )

        # 必須入力をクリア（mass=0）→ 4 出力が "-"、4 適用ボタンが全て無効化される
        browser_page.locator("#lc-mass-mg").fill("0")
        expect(browser_page.locator("#lc-volume-ul")).to_have_text("-")
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("-")
        expect(browser_page.locator("#lc-dispense-rate")).to_have_text("-")
        expect(browser_page.locator("#lc-dispense-accel")).to_have_text("-")
        expect(browser_page.locator("#lc-apply-rotations-per-ul")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-dispense-rate")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-dispense-accel")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-all")).to_be_disabled()


class TestDispenseCalibrationOverBrowser:
    """吐出量キャリブレーション統合ジョブ画面の実ブラウザ表示."""

    def test_menu_and_loading_controls_render(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/dispense_calibration",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#calibration-menu").wait_for(
            state="visible", timeout=10_000
        )

        # ①②③/全実行/終了 のメニューボタン（ジョブ未実行なので全て disabled）
        for button_id in (
            "#calib-rotations-per-ul",
            "#calib-max-dispense-rate",
            "#calib-max-fill-speed",
            "#calib-all",
            "#calib-finish",
        ):
            expect(browser_page.locator(button_id)).to_be_disabled()

        # プライム用 loading_controls はメニュー段階と ① 専用ローディング段階で有効化される設定
        panel = browser_page.locator("#loading-controls")
        panel.wait_for(state="visible", timeout=10_000)
        assert (
            panel.get_attribute("data-loading-stage")
            == "キャリブレーションメニュー,ローディング"
        )

        # 実行中変更可（runtime_editable）の入力には目印が付き、固定値には付かない
        assert (
            browser_page.locator("#param-line_length").get_attribute(
                "data-runtime-editable"
            )
            == "true"
        )
        assert (
            browser_page.locator("#param-board_width").get_attribute(
                "data-runtime-editable"
            )
            is None
        )

    def test_runtime_params_script_is_loaded(self, live_server: LiveServer):
        # 実行中パラメータ編集 JS がページに読み込まれている（薄ラッパー）
        page = httpx.get(
            f"{live_server.base_url}/pasting/dispense_calibration",
            timeout=_HTTP_TIMEOUT,
        )
        assert page.status_code == 200
        assert "js/dispense_runtime_params.js" in page.text


class TestProbeGuideOverRealHttp:
    """ロードセルプローブのガイドページと旧サーボジョブの撤去（計画書 「WebUI ガイドページ」「ユーザー決定事項 1・2」節）."""

    def test_probe_guide_page_is_served_with_calibration_steps(
        self, live_server: LiveServer
    ):
        response = httpx.get(
            f"{live_server.base_url}/pasting/probe_guide", timeout=_HTTP_TIMEOUT
        )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # Klipper コンソールでの較正手順と公式ドキュメントリンク
        assert "LOAD_CELL_CALIBRATE" in response.text
        assert "SAVE_CONFIG" in response.text
        assert "klipper3d.org" in response.text

    def test_probe_gnd_down_adjust_is_removed_from_pasting_tab(
        self, live_server: LiveServer
    ):
        tab = httpx.get(f"{live_server.base_url}/pasting", timeout=_HTTP_TIMEOUT)

        assert tab.status_code == 200
        assert "probe_gnd_down_adjust" not in tab.text
        page = httpx.get(
            f"{live_server.base_url}/pasting/probe_gnd_down_adjust",
            timeout=_HTTP_TIMEOUT,
        )
        assert page.status_code == 404


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


class TestAirPumpToggleOverRealHttp:
    """air_pump_enabled トグルを実 HTTP で PUT → GET → toml 反映まで検証."""

    def test_put_air_pump_enabled_persists_and_reflects(self, live_server: LiveServer):
        # ホワイトリストに air_pump_enabled が含まれる
        before = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        ).json()
        keys = {field["key"] for field in before["fields"]}
        assert "paste_dispenser.air_pump_enabled" in keys

        # false を PUT
        put = httpx.put(
            f"{live_server.base_url}/api/settings/machine",
            json={"values": {"paste_dispenser.air_pump_enabled": False}},
            timeout=_HTTP_TIMEOUT,
        )
        assert put.status_code == 200, put.text

        # GET で false が反映される
        after = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        ).json()
        fields = {field["key"]: field for field in after["fields"]}
        assert fields["paste_dispenser.air_pump_enabled"]["value"] is False

        # 隔離した tmp の machine.toml に書かれている（実機設定は汚していない）
        machine_toml = (
            live_server.settings.configs_root / "kurousagi" / "machine.toml"
        ).read_text()
        assert "air_pump_enabled = false" in machine_toml
