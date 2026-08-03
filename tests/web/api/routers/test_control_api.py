"""操作権リースの HTTP / WS 境界の仕様テスト（MR6 実装契約 §2 / §3 / §6 / §10）.

契約:

- `POST /api/control/{acquire,release,takeover,name}` は
  `{"control": {...}, "you": {"key": ...}}` を返す（`GET /api/state` の同名フィールドと
  同じ形）。`/api/state` は `control`（未保持でも `held: false` の形）と `you` を足す
- セッション同定はヘッダ `X-Pcbasm-Session` → cookie `pcbasm_session` → `anonymous`、
  表示名はヘッダ `X-Pcbasm-Client-Name`（quote 済み）→ cookie `pcbasm_name` →
  `名前未設定 (<key>)`。壊れた入力は例外にせずフォールバックする
- 変更系は非保持者に **423 Locked**（body に `holder`）。`emergency-stop` /
  `jobs/current/abort` / WS `abort` / `takeover` / 全 GET は**絶対にゲートしない**
- 拒否は `Depends`（ハンドラ本体の外）で起きるので、`klipper_errors_to_502()` に
  巻き込まれて 502 に化けない
- WS は認可拒否で**切断しない**（`{"type": "error"}` を返して接続を維持する）

2 クライアントの区別はリクエストごとのヘッダで行う（同一 app に 2 つ目の
`TestClient` を被せると lifespan が二重に走り、`bind_loop` の指す loop がずれる）。

ゲート網羅の掃引では「ゲートが無ければ 423 以外になる」入力を選び、ゲートが外れても
装置を触らない / ジョブが走り出さないものにしてある（未登録ジョブ名・空 values 等）。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from tests.helpers import before_deadline
from tests.web.api.jobs.conftest import register_synthetic
from web.api.app import create_app
from web.api.control import ClientIdentity
from web.api.jobs.context import JobContext, PromptSpec
from web.api.settings import Settings

ALICE = {"X-Pcbasm-Session": "alice-session", "X-Pcbasm-Client-Name": quote("田中")}
BOB = {"X-Pcbasm-Session": "bob-session", "X-Pcbasm-Client-Name": quote("鈴木")}

ALICE_KEY = ClientIdentity("alice-session", "田中").key
BOB_KEY = ClientIdentity("bob-session", "鈴木").key

FREE_CONTROL = {"key": None, "display_name": None, "held": False, "connections": 0}

# WS イベントの待ち時間上限（ハングをテスト失敗に変えるための締切）
_WS_DEADLINE = 15.0

# `ControlLease` の既定 idle_timeout を確実に超える経過時間（fake clock で進める量）
_PAST_IDLE_TIMEOUT = 601.0

# ゲート対象（非保持者は 423）。ゲートが外れたときの応答は 4xx/5xx だが 423 ではない
GATED_REQUESTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/api/jobs/no_such_job", {"params": {}}),
    ("post", "/api/jobs/no_such_job/param-defaults", {"values": {}}),
    ("put", "/api/jobs/current/params", {"values": {}}),
    ("post", "/api/jobs/last/apply", None),
    ("post", "/api/jobs/last/discard", None),
    ("put", "/api/settings/machine", {"values": {}}),
    ("post", "/api/machine-control", {"action": "relax"}),
    ("post", "/api/firmware-restart", None),
    ("put", "/api/pcb-file", {"path": "boards/sample.kicad_pcb"}),
    ("post", "/api/pcb-file/upload", None),
    ("post", "/api/pasting/nozzle-cap/record", None),
    ("patch", "/api/pasting/pad-config/node", {"node": "L1", "values": {}}),
    ("patch", "/api/pasting/pad-config/pads", {"ids": [], "enabled": True}),
    ("patch", "/api/pasting/pad-config/initial-purge", {"initial_purge_ul": 1.0}),
    ("post", "/api/pasting/pad-config/import", {"document": {}}),
]

# 誰でも可（安全機能・読み取り専用計算・閲覧）
UNGATED_REQUESTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/api/emergency-stop", None),
    ("post", "/api/jobs/current/abort", None),
    ("post", "/api/control/takeover", None),
    ("post", "/api/pasting/pad-config/route", {"layer": "F.Cu"}),
    ("post", "/api/pasting/pad-config/fill-path", {"layer": "F.Cu"}),
    ("get", "/api/state", None),
    ("get", "/api/jobs", None),
    ("get", "/api/jobs/current", None),
    ("get", "/api/settings/machine", None),
    ("get", "/api/pasting/pad-config", None),
]


def _request(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> int:
    """指定 identity で 1 リクエスト送り、ステータスコードを返す."""
    if path.endswith("/upload"):
        # multipart 必須のエンドポイント（body 検証より前にゲートが効くかを見る）
        response = client.post(
            path, files={"file": ("x.kicad_pcb", b"()")}, headers=headers
        )
    else:
        response = client.request(method.upper(), path, json=body, headers=headers)
    return response.status_code


def _control(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    """`GET /api/state` の control フィールド."""
    response = client.get("/api/state", headers=headers)
    assert response.status_code == 200
    return response.json()["control"]


def _receive_type(
    ws: WebSocketTestSession, wanted: str, *, limit: int = 500
) -> dict[str, Any]:
    """指定 type の WS イベントが届くまで受信する（他 type は読み飛ばす）."""

    def receive() -> dict[str, Any]:
        for _ in range(limit):
            message = ws.receive_json()
            if message["type"] == wanted:
                return message
        pytest.fail(f"{limit} 件以内に type={wanted} が届きませんでした")

    # `receive_json` は届くまで無期限に待つ。イベントを配らない退行（購読登録漏れ
    # など）を「終わらないテスト」ではなく失敗にするため締切をテスト側に持たせる
    return before_deadline(
        receive, what=f"type={wanted} の WS イベント", deadline=_WS_DEADLINE
    )


def _wait_status(client: TestClient, status: str, *, timeout: float = 30.0) -> None:
    """GET /api/jobs/current をポーリングして指定ステータスを待つ（締切はテスト側）."""
    deadline = time.monotonic() + timeout
    job: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/current").json()["job"]
        if job is not None and job["status"] == status:
            return
        time.sleep(0.02)
    pytest.fail(f"{timeout}s 以内に status={status} になりませんでした: {job}")


def _register_prompting(app: FastAPI, name: str = "control_prompt") -> None:
    """Confirm prompt を 1 回出して終わる合成ジョブを登録する（装置は触らない）."""

    def run(ctx: JobContext) -> None:
        ctx.prompt(PromptSpec(kind="confirm", message="続行しますか"))

    register_synthetic(app.state.catalog, run, name=name, hidden=True)


def _register_commanded(app: FastAPI, name: str) -> None:
    """WS command を 1 件受け取るまで待つ合成ジョブを登録する（装置は触らない）."""

    def run(ctx: JobContext) -> None:
        ctx.next_command(timeout=None)

    register_synthetic(
        app.state.catalog, run, name=name, hidden=True, accepts_commands=True
    )


class _FakeClock:
    """テストが明示的に進める単調時計（`create_app(clock=...)` へ注入する）."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestControlEndpoints:
    """POST /api/control/{acquire,release,takeover,name} のワイヤ契約."""

    def test_acquire_returns_control_and_you(self, client: TestClient):
        response = client.post("/api/control/acquire", headers=ALICE)

        assert response.status_code == 200
        assert response.json() == {
            "control": {
                "key": ALICE_KEY,
                "display_name": "田中",
                "held": True,
                "connections": 0,
            },
            "you": {"key": ALICE_KEY},
        }

    def test_release_frees_the_lease(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/control/release", headers=ALICE)

        assert response.status_code == 200
        assert response.json()["control"] == FREE_CONTROL
        assert _control(client, BOB) == FREE_CONTROL

    def test_release_by_viewer_does_not_free_the_lease(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/control/release", headers=BOB)

        assert response.status_code == 200
        assert response.json()["control"]["key"] == ALICE_KEY
        assert response.json()["you"] == {"key": BOB_KEY}

    def test_acquire_by_second_client_returns_423_with_holder(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/control/acquire", headers=BOB)

        assert response.status_code == 423
        assert response.json() == {
            "detail": "操作権は〈田中〉が保持しています",
            "holder": {
                "key": ALICE_KEY,
                "display_name": "田中",
                "held": True,
                "connections": 0,
            },
        }

    def test_takeover_wins_against_the_holder(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/control/takeover", headers=BOB)

        assert response.status_code == 200
        assert response.json()["control"]["key"] == BOB_KEY
        assert response.json()["control"]["display_name"] == "鈴木"
        # 奪われた側は変更系を実行できなくなる
        assert client.post("/api/firmware-restart", headers=ALICE).status_code == 423

    def test_name_updates_the_display_name_of_the_holder(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200
        renamed = dict(ALICE) | {"X-Pcbasm-Client-Name": quote("田中太郎")}

        response = client.post("/api/control/name", headers=renamed)

        assert response.status_code == 200
        assert response.json()["control"]["display_name"] == "田中太郎"
        assert _control(client, BOB)["display_name"] == "田中太郎"

    def test_name_by_viewer_does_not_steal_the_lease(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/control/name", headers=BOB)

        assert response.status_code == 200
        assert response.json()["control"]["key"] == ALICE_KEY


class TestStateControlFields:
    """GET /api/state の control / you（frontend が「自分が保持者か」を決める材料）."""

    def test_free_lease_is_reported_as_held_false(self, client: TestClient):
        data = client.get("/api/state", headers=ALICE).json()

        assert data["control"] == FREE_CONTROL
        assert data["you"] == {"key": ALICE_KEY}

    def test_holder_is_visible_to_the_viewer(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        data = client.get("/api/state", headers=BOB).json()

        assert data["control"]["key"] == ALICE_KEY
        assert data["control"]["display_name"] == "田中"
        assert data["you"] == {"key": BOB_KEY}
        # 保持者判定は key の比較で行う（サーバは接続ごとに payload を作り分けない）
        assert data["control"]["key"] != data["you"]["key"]

    def test_pcb_selection_response_reports_the_caller_as_holder(
        self, client: TestClient
    ):
        response = client.put(
            "/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}, headers=ALICE
        )

        assert response.status_code == 200
        assert response.json()["control"]["key"] == ALICE_KEY
        assert response.json()["you"] == {"key": ALICE_KEY}


class TestIdentityResolution:
    """セッション / 表示名の解決順とフォールバック（契約 §2）."""

    def test_request_without_headers_claims_as_anonymous(self, client: TestClient):
        response = client.post("/api/control/acquire")

        assert response.status_code == 200
        anonymous = ClientIdentity("anonymous", "").key
        assert response.json()["control"]["key"] == anonymous
        assert response.json()["control"]["display_name"] == f"名前未設定 ({anonymous})"

    def test_quoted_japanese_name_is_restored(self, client: TestClient):
        response = client.post(
            "/api/control/acquire",
            headers={"X-Pcbasm-Session": "s1", "X-Pcbasm-Client-Name": quote("田中")},
        )

        assert response.json()["control"]["display_name"] == "田中"

    def test_cookies_are_used_when_headers_are_absent(self, client: TestClient):
        cookie = f"pcbasm_session=cookie-session; pcbasm_name={quote('佐藤')}"

        response = client.post("/api/control/acquire", headers={"Cookie": cookie})

        assert response.json()["control"] == {
            "key": ClientIdentity("cookie-session", "佐藤").key,
            "display_name": "佐藤",
            "held": True,
            "connections": 0,
        }

    def test_header_wins_over_cookie(self, client: TestClient):
        response = client.post(
            "/api/control/acquire",
            headers={
                "X-Pcbasm-Session": "header-session",
                "X-Pcbasm-Client-Name": quote("ヘッダ名"),
                "Cookie": "pcbasm_session=cookie-session; pcbasm_name=cookie-name",
            },
        )

        assert response.json()["control"]["key"] == (
            ClientIdentity("header-session", "").key
        )
        assert response.json()["control"]["display_name"] == "ヘッダ名"

    @pytest.mark.parametrize(
        "raw_name",
        [
            pytest.param("田中".encode(), id="raw-utf8-bytes"),
            pytest.param(b"%E7%94", id="truncated-percent-encoding"),
            pytest.param(b"   ", id="blank"),
        ],
    )
    def test_undecodable_name_falls_back_to_placeholder(
        self, client: TestClient, raw_name: bytes
    ):
        """壊れた表示名は 500 にせず既定名へ落とす（文字化けを全員に配らない）."""
        response = client.post(
            "/api/control/acquire",
            headers={"X-Pcbasm-Session": "s2", "X-Pcbasm-Client-Name": raw_name},
        )

        assert response.status_code == 200
        expected = ClientIdentity("s2", "").key
        assert response.json()["control"]["display_name"] == f"名前未設定 ({expected})"


class TestGateCoverage:
    """変更系のゲートと、絶対にゲートしないものの掃引."""

    @pytest.mark.parametrize(("method", "path", "body"), GATED_REQUESTS)
    def test_viewer_is_denied_with_423(
        self, client: TestClient, method: str, path: str, body: dict[str, Any] | None
    ):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        status = _request(client, method, path, body, BOB)

        assert status == 423

    @pytest.mark.parametrize(("method", "path", "body"), UNGATED_REQUESTS)
    def test_viewer_is_never_locked_out(
        self, client: TestClient, method: str, path: str, body: dict[str, Any] | None
    ):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        status = _request(client, method, path, body, BOB)

        assert status != 423

    def test_holder_passes_the_gate(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        # ゲートを通ると Klipper 不達（テスト config の port 7126）まで到達する
        assert client.post("/api/firmware-restart", headers=ALICE).status_code == 502

    def test_free_lease_is_claimed_by_the_first_writer(self, client: TestClient):
        """誰も保持していなければ変更系の呼び出し自体が操作権を取る."""
        assert (
            client.post("/api/machine-control", json={"action": "relax"}).status_code
            == 502
        )

        anonymous = ClientIdentity("anonymous", "").key
        assert _control(client, ALICE)["key"] == anonymous

    def test_denied_machine_control_is_423_not_502(self, client: TestClient):
        """認可は `Depends` で行う（ハンドラ本体で claim すると 502 に化ける）.

        `post_machine_control` は本体を `klipper_errors_to_502()` で包む。
        `ControlDeniedError` は `RuntimeError` 派生なので、本体の中で claim すると
        リース拒否が「Klipper 通信エラー 502」になり、frontend が 423 を検出できない。
        """
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post(
            "/api/machine-control", json={"action": "relax"}, headers=BOB
        )

        assert response.status_code == 423
        assert response.json()["holder"]["display_name"] == "田中"

    def test_emergency_stop_works_for_a_viewer(self, client: TestClient):
        """緊急停止は安全機能なので閲覧者でも通る（Klipper 不達の 502 まで到達）."""
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        response = client.post("/api/emergency-stop", headers=BOB)

        assert response.status_code == 502

    def test_viewer_can_abort_a_job_started_by_the_holder(
        self, client: TestClient, app: FastAPI
    ):
        _register_prompting(app, "control_abortable")
        assert (
            client.post(
                "/api/jobs/control_abortable", json={}, headers=ALICE
            ).status_code
            == 201
        )
        _wait_status(client, "waiting_input")

        response = client.post("/api/jobs/current/abort", headers=BOB)

        assert response.status_code == 200
        _wait_status(client, "aborted")


class TestWebSocketControl:
    """WS の在線・認可・control_changed 通知."""

    def test_connection_counts_as_presence(self, client: TestClient):
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        with client.websocket_connect("/api/ws", headers=ALICE):
            assert _control(client, ALICE)["connections"] == 1

        # 切断は finally で必ず登録解除される（猶予中なのでリースは保持のまま）
        control = _control(client, ALICE)
        assert control["connections"] == 0
        assert control["held"] is True

    def test_control_changed_is_broadcast_to_every_subscriber(self, client: TestClient):
        with (
            client.websocket_connect("/api/ws", headers=ALICE) as alice_ws,
            client.websocket_connect("/api/ws", headers=BOB) as bob_ws,
        ):
            assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

            for ws in (alice_ws, bob_ws):
                message = _receive_type(ws, "control_changed")
                # 全員へ同一 payload（自分かどうかは you.key と比べて各自が判定する）
                assert message["control"]["key"] == ALICE_KEY
                assert message["control"]["display_name"] == "田中"
                assert message["control"]["held"] is True

    def test_viewer_respond_prompt_yields_error_without_closing(
        self, client: TestClient, app: FastAPI
    ):
        _register_prompting(app, "control_ws_prompt")
        with client.websocket_connect("/api/ws", headers=BOB) as bob_ws:
            assert (
                client.post(
                    "/api/jobs/control_ws_prompt", json={}, headers=ALICE
                ).status_code
                == 201
            )
            prompt = _receive_type(bob_ws, "prompt")["prompt"]

            bob_ws.send_json(
                {"type": "respond_prompt", "prompt_id": prompt["id"], "answer": True}
            )

            error = _receive_type(bob_ws, "error")
            assert "田中" in error["detail"]
            # 接続は維持される（切れていれば次の送受信で例外になる）
            bob_ws.send_json({"type": "no_such_type"})
            assert "no_such_type" in _receive_type(bob_ws, "error")["detail"]
            # 権限が無い間はジョブも進まない
            assert client.get("/api/jobs/current").json()["job"]["status"] == (
                "waiting_input"
            )

    def test_viewer_command_yields_error_without_closing(
        self, client: TestClient, app: FastAPI
    ):
        """`command` も `respond_prompt` と同じくゲートする.

        `command` は実行中ジョブへの Record / Quit や任意 G-code の入口なので、
        ここが開くと閲覧者が保持者のジョブへ割り込める（frontend の `inert` は
        UI だけの防御で、WS へ直接送れば迂回できる）。
        """
        _register_commanded(app, "control_ws_command")
        with client.websocket_connect("/api/ws", headers=BOB) as bob_ws:
            assert (
                client.post(
                    "/api/jobs/control_ws_command", json={}, headers=ALICE
                ).status_code
                == 201
            )
            _wait_status(client, "running")

            bob_ws.send_json({"type": "command", "command": {"type": "record"}})

            error = _receive_type(bob_ws, "error")
            assert "田中" in error["detail"]
            # 接続は維持される（切れていれば次の送受信で例外になる）
            bob_ws.send_json({"type": "no_such_type"})
            assert "no_such_type" in _receive_type(bob_ws, "error")["detail"]
            # コマンドはジョブへ届いていない（届けば next_command が返って終わる）
            assert client.get("/api/jobs/current").json()["job"]["status"] == "running"

    def test_holder_command_reaches_the_running_job(
        self, client: TestClient, app: FastAPI
    ):
        """ゲートは保持者を止めない（コマンドがジョブへ届いて完了する）."""
        _register_commanded(app, "control_ws_command_holder")
        with client.websocket_connect("/api/ws", headers=ALICE) as alice_ws:
            assert (
                client.post(
                    "/api/jobs/control_ws_command_holder", json={}, headers=ALICE
                ).status_code
                == 201
            )
            _wait_status(client, "running")

            alice_ws.send_json({"type": "command", "command": {"type": "record"}})

            _wait_status(client, "succeeded")

    def test_viewer_abort_over_ws_is_allowed(self, client: TestClient, app: FastAPI):
        _register_prompting(app, "control_ws_abort")
        with client.websocket_connect("/api/ws", headers=BOB) as bob_ws:
            assert (
                client.post(
                    "/api/jobs/control_ws_abort", json={}, headers=ALICE
                ).status_code
                == 201
            )
            _receive_type(bob_ws, "prompt")

            bob_ws.send_json({"type": "abort"})

            _wait_status(client, "aborted")

    def test_release_lets_the_next_client_answer_the_pending_prompt(
        self, client: TestClient, app: FastAPI
    ):
        """詰み回避の直接検証（保持者が帰っても他人が引き継いで進められる）."""
        _register_prompting(app, "control_ws_handover")
        with client.websocket_connect("/api/ws", headers=BOB) as bob_ws:
            assert (
                client.post(
                    "/api/jobs/control_ws_handover", json={}, headers=ALICE
                ).status_code
                == 201
            )
            prompt = _receive_type(bob_ws, "prompt")["prompt"]
            assert client.post("/api/control/release", headers=ALICE).status_code == 200
            assert client.post("/api/control/acquire", headers=BOB).status_code == 200

            bob_ws.send_json(
                {"type": "respond_prompt", "prompt_id": prompt["id"], "answer": True}
            )

            _wait_status(client, "succeeded")


class TestIdleExpiryIsGatedByTheMachineLock:
    """`create_app` の `busy=` 配線（§1 の裁定「ジョブ中は無操作失効しない」）.

    リースの純ロジックは `tests/web/api/test_control.py` が注入した fake busy で
    見ているので、ここは**装置排他ロックが `busy` として繋がっているか**だけを見る
    （`lambda: True` / `lambda: False` に固定すると、どちらか一方の向きが崩れる）。
    実時間の 600s は待てないので clock を注入する。
    """

    def test_lease_survives_idle_timeout_while_the_machine_is_locked(
        self, webui_settings: Settings
    ):
        clock = _FakeClock()
        app = create_app(webui_settings, clock=clock)
        with TestClient(app) as client:
            assert client.post("/api/control/acquire", headers=ALICE).status_code == 200
            # WS 在線で切断猶予の失効を止め、無操作失効だけを観測対象にする
            with client.websocket_connect("/api/ws", headers=ALICE):
                with app.state.appstate.machine_lock("test-job"):
                    clock.advance(_PAST_IDLE_TIMEOUT)

                    assert _control(client, ALICE)["held"] is True

                # ロックを離せば同じ無操作時間で失効する（= busy が繋がっている）
                assert _control(client, ALICE)["held"] is False


class TestControlDoesNotBlockJobs:
    """ジョブとリースの独立（契約 §4）."""

    def test_takeover_does_not_touch_the_running_job(
        self, client: TestClient, app: FastAPI
    ):
        _register_prompting(app, "control_takeover_job")
        assert (
            client.post(
                "/api/jobs/control_takeover_job", json={}, headers=ALICE
            ).status_code
            == 201
        )
        _wait_status(client, "waiting_input")

        assert client.post("/api/control/takeover", headers=BOB).status_code == 200

        assert client.get("/api/jobs/current").json()["job"]["status"] == (
            "waiting_input"
        )
