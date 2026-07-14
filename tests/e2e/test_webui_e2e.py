"""WebUI フルスタック E2E（実 uvicorn + 実 HTTP / WebSocket / MJPEG）.

`make test-e2e` で実行する。TestClient では検証しづらい以下を実ネットワーク経由で確認する:

- 無限 MJPEG ストリーム（TestClient は完全受信まで返らずハングする）
- WebSocket のイベント往復（ジョブ起動 → ログ/進捗/プロンプト → 完走 → 設定反映）

ジョブ通しの題材には hidden の ``job_demo``（uses_machine=False・実機不要）を使う。
log / progress / prompt / prompt_resolved / apply を一通り通すための検証用ジョブで、
prompt 2 回（confirm → number）に応答すると SUCCEEDED + apply ペイロードを返す。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, override

import httpx
from playwright.sync_api import expect
from websockets.sync.client import connect

from tests.e2e.conftest import (
    TERMINAL as _TERMINAL,
    LiveServer,
    drive_job_demo as _drive_job_demo,
    respond_prompt as _respond_prompt,
    select_led_blinker as _select_led_blinker,
    wait_first_prompt as _wait_first_prompt,
    wait_machine_field as _wait_machine_field,
)
from tests.helpers import wait_until
from tests.webui.conftest import decode_jpeg, jpeg_payload
from webui.routers.pasting_view import ResolvedSettings

_HTTP_TIMEOUT = 10.0


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


def _current_job(base_url: str) -> dict[str, Any] | None:
    """GET /api/jobs/current の job（無ければ None）を返す."""
    response = httpx.get(f"{base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200
    return response.json()["job"]


def _wait_for_current_job(base_url: str, job_id: str) -> dict[str, Any]:
    """現在ジョブが終端するまで REST 経由で待つ."""
    wait_until(
        lambda: (job := _current_job(base_url)) is not None
        and job["id"] == job_id
        and job["status"] in _TERMINAL,
        timeout=60.0,
        interval=0.05,
    )
    job = _current_job(base_url)
    assert job is not None
    return job


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

    def test_reference_point_page_explains_record_applies_immediately(
        self, live_server: LiveServer
    ):
        response = httpx.get(
            f"{live_server.base_url}/posctrl/reference_point_setup",
            timeout=_HTTP_TIMEOUT,
        )

        assert response.status_code == 200
        assert "Record を押すと現在位置を記録し、設定へ即時反映します" in response.text


class TestArtifactsOverRealHttp:
    """/artifacts mount の実 HTTP 配信（ジョブ実生成は jobs/test_pasting.py が担保）."""

    def test_file_under_webui_data_dir_is_served(self, live_server: LiveServer):
        artifact_dir = live_server.settings.webui_data_dir / "job-artifact-test"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "board.kicad_pcb").write_text(
            "(kicad_pcb (version 20240101))", encoding="utf-8"
        )

        download = httpx.get(
            f"{live_server.base_url}/artifacts/job-artifact-test/board.kicad_pcb",
            timeout=_HTTP_TIMEOUT,
        )

        assert download.status_code == 200
        assert b"(kicad_pcb" in download.content


class TestPreviewOverRealHttp:
    """Fake カメラのプレビューを実 HTTP で取得する（無限 stream / クライアント数）.

    TestClient は無限 MJPEG を完全受信まで返らずハングするため、実 uvicorn 経由の本クラスが stream
    配信・preview_clients の唯一の検証点。
    """

    def test_stream_returns_decodable_mjpeg_multipart(self, live_server: LiveServer):
        data = _read_mjpeg(
            live_server.base_url, "/api/preview/stream?overlay=crosshair"
        )

        assert b"Content-Type: image/jpeg" in data
        frame = decode_jpeg(jpeg_payload(data.split(b"--frame")[1]))
        assert frame is not None
        assert frame.shape == (720, 1280, 3)

    def test_copper_stream_accepts_canny_query(self, live_server: LiveServer):
        data = _read_mjpeg(
            live_server.base_url,
            "/api/preview/stream?overlay=copper&canny_low=50&canny_high=150",
        )

        assert decode_jpeg(jpeg_payload(data.split(b"--frame")[1])) is not None

    def test_state_reports_streaming_client_count(self, live_server: LiveServer):
        with httpx.Client(base_url=live_server.base_url, timeout=10.0) as client:
            assert client.get("/api/state").json()["preview_clients"] == 0

            with client.stream("GET", "/api/preview/stream") as response:
                next(response.iter_bytes())  # 配信開始を確実にする
                assert client.get("/api/state").json()["preview_clients"] == 1

            # 切断後のサーバー側クリーンアップは非同期に走るため短時間ポーリングする
            wait_until(
                lambda: client.get("/api/state").json()["preview_clients"] == 0,
                timeout=5.0,
                interval=0.05,
            )


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


class TestDispenseCalibrationPage:
    """吐出量キャリブレーション画面の HTTP 配信."""

    def test_runtime_params_script_is_loaded(self, live_server: LiveServer):
        # 実行中パラメータ編集 JS がページに読み込まれている（薄ラッパー）
        page = httpx.get(
            f"{live_server.base_url}/pasting/dispense_calibration",
            timeout=_HTTP_TIMEOUT,
        )
        assert page.status_code == 200
        assert "js/dispense_runtime_params.js" in page.text


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


class TestPadAlignMaxFailuresOverRealHttp:
    """pad_align.max_failures を実 HTTP で PUT → GET → toml 反映まで検証 （paste-align-
    max-failures 計画書）."""

    def test_put_max_failures_persists_and_reflects(self, live_server: LiveServer):
        # ホワイトリストに max_failures が含まれる
        before = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        ).json()
        keys = {field["key"] for field in before["fields"]}
        assert "paste_dispenser.pad_align.max_failures" in keys

        # 2 を PUT
        put = httpx.put(
            f"{live_server.base_url}/api/settings/machine",
            json={"values": {"paste_dispenser.pad_align.max_failures": 2}},
            timeout=_HTTP_TIMEOUT,
        )
        assert put.status_code == 200, put.text

        # GET で 2 が反映される
        after = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        ).json()
        fields = {field["key"]: field for field in after["fields"]}
        assert fields["paste_dispenser.pad_align.max_failures"]["value"] == 2

        # 隔離した tmp の machine.toml に書かれている（実機設定は汚していない）
        machine_toml = (
            live_server.settings.configs_root / "kurousagi" / "machine.toml"
        ).read_text()
        assert "max_failures = 2" in machine_toml


class TestNozzleCapOverRealHttp:
    """ノズルキャップ位置設定の実 HTTP 経路（nozzle-cap-parking 計画書「API 契約」節）."""

    def test_machine_settings_fields_include_nozzle_cap(self, live_server: LiveServer):
        response = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        )

        assert response.status_code == 200
        keys = {field["key"] for field in response.json()["fields"]}
        assert "nozzle_cap.x" in keys

    def test_nozzle_cap_page_renders_record_button(self, live_server: LiveServer):
        page = httpx.get(
            f"{live_server.base_url}/pasting/nozzle_cap", timeout=_HTTP_TIMEOUT
        )

        assert page.status_code == 200
        assert "記録" in page.text


class _PadTableHeaderCounter(HTMLParser):
    """Id="pad-table" の thead 内 <th> 個数を数える stdlib パーサ.

    BeautifulSoup 等の HTML パーサは依存に無いため、stdlib の html.parser を使う。 pad-
    table の thead はサーバーレンダリングの静的 HTML（tbody だけ JS が埋める）
    なので、ページ取得だけでヘッダ列数を数えられる。
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_pad_table = False
        self._in_thead = False
        self.th_count = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "table" and attr.get("id") == "pad-table":
            self._in_pad_table = True
        elif tag == "thead" and self._in_pad_table:
            self._in_thead = True
        elif tag == "th" and self._in_thead:
            self.th_count += 1

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "thead" and self._in_pad_table:
            self._in_thead = False
        elif tag == "table" and self._in_pad_table:
            self._in_pad_table = False


def _count_pad_table_header_columns(html: str) -> int:
    """HTML から id="pad-table" の thead 内 <th> 個数を返す."""
    counter = _PadTableHeaderCounter()
    counter.feed(html)
    return counter.th_count


class TestPadTableHeaderOverRealHttp:
    """はんだ塗布ページの pad-table ヘッダ列数がバックエンドモデルと構造整合する."""

    def test_pad_table_header_column_count_matches_resolved_settings(
        self, live_server: LiveServer
    ):
        # はんだ塗布ページの静的 HTML を実サーバーから取得する（PCB 未選択でも
        # thead は常にレンダリングされる）
        page = httpx.get(
            f"{live_server.base_url}/pasting/paste_solder", timeout=_HTTP_TIMEOUT
        )
        assert page.status_code == 200

        header_columns = _count_pad_table_header_columns(page.text)

        # テーブルは「ノード列 + 有効(enabled)列 + 各設定フィールド列」で構成される。
        # ResolvedSettings は enabled を含む解決済み設定の全フィールドを持つので、
        # 期待 <th> 数は ノード列(1) + len(ResolvedSettings.model_fields)。数値を
        # ハードコードせずモデルから導出することで、将来フィールドが増減したときの
        # ヘッダ更新漏れ（本バグと同種のヘッダ/ボディ列ずれ）を検出できる。
        expected_columns = 1 + len(ResolvedSettings.model_fields)
        assert header_columns == expected_columns
