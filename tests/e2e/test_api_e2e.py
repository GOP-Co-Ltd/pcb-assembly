"""Backend WebAPI のフルスタック E2E（実 uvicorn + 実 HTTP / WebSocket / MJPEG）.

`make test-e2e` で実行する。TestClient では検証しづらい以下を実ネットワーク経由で確認する:

- 無限 MJPEG ストリーム（TestClient は完全受信まで返らずハングする）
- WebSocket のイベント往復（ジョブ起動 → ログ/進捗/プロンプト → 完走 → 設定反映）

API は `live_server`（backend 直）へ、SSR ページは `live_ui`（frontend 経由）へ投げる。
プロキシ経路そのものの検証は tests/e2e/test_proxy_e2e.py が担当する。

ジョブ通しの題材には hidden の ``job_demo``（uses_machine=False・実機不要）を使う。
log / progress / prompt / prompt_resolved / apply を一通り通すための検証用ジョブで、
prompt 2 回（confirm → number）に応答すると SUCCEEDED + apply ペイロードを返す。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, override

import httpx
from websockets.sync.client import connect

from tests.e2e.conftest import (
    TERMINAL as _TERMINAL,
    LiveServer,
    LiveUi,
    drive_job_demo as _drive_job_demo,
    respond_prompt as _respond_prompt,
    select_led_blinker as _select_led_blinker,
    wait_first_prompt as _wait_first_prompt,
    wait_machine_field as _wait_machine_field,
)
from tests.helpers import wait_until
from tests.web.api.conftest import decode_jpeg, jpeg_payload
from web.api.routers.pasting_view import ResolvedSettings

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
        machine_toml = (live_server.settings.config_dir / "machine.toml").read_text()
        assert "canny_low = 77" in machine_toml


class TestRuntimeParamUpdateOverWebSocket:
    """PUT /api/jobs/current/params の実 HTTP + WS 通し検証（job_demo 題材）.

    実行中（prompt 待機中）の out-of-band 反映は実プロセスでしか成立しない。 キー種別ごとの 400 は
    tests/web/api/routers/test_jobs.py が担当する。
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


class TestCameraCropSettingsOverRealHttp:
    """Camera.crop.* の実 HTTP 経路（webui-camera-calib 計画書「テスト観点」e2e 項）.

    クロップ編集 UI は settings ページの汎用フォームへ統一されたが、
    API 契約（rebuild しない・crosshair オーバーレイのフレーム毎反映）は不変。
    どの経路から PUT されても crop 変更が MJPEG ストリームを切断せず
    次フレームへ反映されることが本質のため、通し確認は API 直叩きで行う。

    - crop PUT 中も同一 MJPEG ストリームが生存する（再接続なしで反映の通し確認）
    - PUT → GET → 隔離 tmp の machine.toml へ反映される
    """

    def test_crop_put_keeps_mjpeg_stream_open_and_reflects_in_toml(
        self, live_server: LiveServer
    ):
        with httpx.Client(
            base_url=live_server.base_url, timeout=_HTTP_TIMEOUT
        ) as client:
            with client.stream(
                "GET", "/api/preview/stream?overlay=crosshair"
            ) as response:
                assert response.status_code == 200
                chunks = response.iter_bytes()
                data = next(chunks)
                while data.count(b"--frame") < 1:
                    data += next(chunks)

                put = httpx.put(
                    f"{live_server.base_url}/api/settings/machine",
                    json={
                        "values": {
                            "camera.crop.width": 300,
                            "camera.crop.height": 300,
                        }
                    },
                    timeout=_HTTP_TIMEOUT,
                )
                assert put.status_code == 200, put.text

                # 同一レスポンスから追加フレームが取得できる = crop PUT で切断されない
                more = next(chunks)
                while more.count(b"--frame") < 1:
                    more += next(chunks)

        after = httpx.get(
            f"{live_server.base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT
        ).json()
        fields = {field["key"]: field for field in after["fields"]}
        assert fields["camera.crop.width"]["value"] == 300
        assert fields["camera.crop.height"]["value"] == 300

        # 隔離した tmp の machine.toml に書かれている（実機設定は汚していない）
        machine_toml = (live_server.settings.config_dir / "machine.toml").read_text()
        assert "width = 300" in machine_toml
        assert "height = 300" in machine_toml


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
    """はんだ塗布ページの pad-table 列構成がバックエンドモデルと構造整合する.

    設定フィールドの列見出しは静的 HTML には無く、pad-config の ``fields`` から JS が描く。
    ヘッダ/ボディの列ずれは「静的列（ノード・有効）+ fields」と ``ResolvedSettings``
    のフィールド数が一致することで検出する。
    """

    def test_static_header_has_only_node_and_enabled_columns(self, live_ui: LiveUi):
        page = httpx.get(
            f"{live_ui.base_url}/pasting/paste_solder", timeout=_HTTP_TIMEOUT
        )
        assert page.status_code == 200

        # 設定フィールドの見出しはサーバ定義（fields）から描くため静的 HTML には 2 列だけ
        assert _count_pad_table_header_columns(page.text) == 2

    def test_fields_plus_enabled_match_resolved_settings(
        self, live_server: LiveServer, live_ui: LiveUi
    ):
        _select_led_blinker(live_server)
        config = httpx.get(
            f"{live_ui.base_url}/api/pasting/pad-config", timeout=_HTTP_TIMEOUT
        )
        assert config.status_code == 200, config.text

        fields = config.json()["fields"]
        # ResolvedSettings = enabled + 各設定フィールド。数値をハードコードせずモデルから
        # 導出することで、フィールド増減時のヘッダ/ボディ列ずれを検出する
        assert len(fields) + 1 == len(ResolvedSettings.model_fields)
        assert {field["name"] for field in fields} == set(
            ResolvedSettings.model_fields
        ) - {"enabled"}
