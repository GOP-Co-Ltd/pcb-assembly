"""`web.ui.machine_client` の仕様テスト.

計画書 docs/plans/web-api-ui-split.md「MR4」節が契約:

- `BackendGateway` は machine_id ごとに `httpx.AsyncClient` をキャッシュして
  keep-alive する。`transport_factory` を注入して**実物の backend app** を
  in-process で駆動する（`ASGITransport` は httpx 純正なので 3rd-party のモック
  ではない）
- `MachineClient` の取得が **1 本でも落ちたら `BackendUnavailable`**（欠損値の
  フォームを見て書き込み操作をされるのが最悪）
- **backend のレスポンスはキャッシュしない**（frontend は backend の WS を購読
  しないので無効化条件を書けない）

到達失敗は **実 ECONNREFUSED**（誰も listen していない 127.0.0.1:1）で確かめる。
上流の異常応答（5xx・契約外の JSON）は最小の実 ASGI アプリを上流に挿して作る
（レスポンスを組み立てるのは本物の FastAPI / starlette）。

`web.ui` は async なので、このファイルは anyio の pytest プラグイン
（`pytest.mark.anyio` + `anyio_backend`）で駆動する。リポジトリで唯一の async
テストなのは、他が `TestClient`（内部で同期化する）越しに検証しているため。
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import Response

from tests.web.ui.conftest import BACKEND_MACHINE_ID, BACKEND_MACHINE_NAME
from web.api.models import API_VERSION
from web.ui.machine_client import BackendGateway, BackendUnavailable, MachineClient
from web.ui.machines import MachineEndpoint

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def endpoint() -> MachineEndpoint:
    return MachineEndpoint(
        machine_id=BACKEND_MACHINE_ID,
        host="127.0.0.1",
        port=8081,
        name=BACKEND_MACHINE_NAME,
    )


@pytest.fixture
async def gateway(backend_app: FastAPI) -> AsyncIterator[BackendGateway]:
    """上流を in-process の実 backend app に差し替えた gateway."""
    async with aclosing(
        BackendGateway(
            connect_timeout=2.0,
            read_timeout=2.0,
            transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend_app),
        )
    ) as created:
        yield created


@pytest.fixture
def client(endpoint: MachineEndpoint, gateway: BackendGateway) -> MachineClient:
    return MachineClient(endpoint, gateway)


@pytest.fixture
def other_endpoint() -> MachineEndpoint:
    return MachineEndpoint(machine_id="other", host="127.0.0.1", port=8082)


class TestBackendGateway:
    """クライアントのキャッシュと後始末."""

    async def test_same_machine_reuses_one_client(
        self, endpoint: MachineEndpoint, gateway: BackendGateway
    ):
        assert gateway.client_for(endpoint) is gateway.client_for(endpoint)

    async def test_different_machines_get_different_clients(
        self,
        endpoint: MachineEndpoint,
        other_endpoint: MachineEndpoint,
        gateway: BackendGateway,
    ):
        assert gateway.client_for(endpoint) is not gateway.client_for(other_endpoint)

    async def test_client_targets_the_endpoint_base_url(
        self, endpoint: MachineEndpoint, gateway: BackendGateway
    ):
        assert str(gateway.client_for(endpoint).base_url) == endpoint.base_url

    async def test_timeouts_come_from_the_constructor(
        self, endpoint: MachineEndpoint, backend_app: FastAPI
    ):
        async with aclosing(
            BackendGateway(
                connect_timeout=1.5,
                read_timeout=3.5,
                transport_factory=lambda _e: httpx.ASGITransport(app=backend_app),
            )
        ) as gateway:
            timeout = gateway.client_for(endpoint).timeout

            assert (timeout.connect, timeout.read) == (1.5, 3.5)

    async def test_aclose_closes_every_client(
        self,
        endpoint: MachineEndpoint,
        other_endpoint: MachineEndpoint,
        gateway: BackendGateway,
    ):
        clients = [gateway.client_for(endpoint), gateway.client_for(other_endpoint)]

        await gateway.aclose()

        assert [http_client.is_closed for http_client in clients] == [True, True]

    async def test_aclose_drops_the_cache(
        self, endpoint: MachineEndpoint, gateway: BackendGateway
    ):
        """閉じたクライアントを配り続けると以降の取得が全部失敗する."""
        closed = gateway.client_for(endpoint)

        await gateway.aclose()

        assert gateway.client_for(endpoint) is not closed


class TestMachineClient:
    """実物の backend app からの取得（in-process）."""

    async def test_machine_info_reports_the_backend_identity(
        self, client: MachineClient
    ):
        info = await client.machine_info()

        assert info.machine_id == BACKEND_MACHINE_ID
        # 表示名は backend の machine.toml が正（frontend の登録名では上書きしない）。
        # data/testing/config は machine_name を持たないので machine_id にフォールバック
        assert info.machine_name == BACKEND_MACHINE_ID
        assert info.api_version == API_VERSION

    async def test_state_is_validated_into_the_contract_model(
        self, client: MachineClient
    ):
        state = await client.state()

        assert state.pcb_file is None
        assert state.busy is False

    async def test_machine_settings_returns_whitelisted_fields(
        self, client: MachineClient
    ):
        settings = await client.machine_settings()

        assert "machine_name" in {field.key for field in settings.fields}

    async def test_jobs_includes_hidden_definitions(self, client: MachineClient):
        """Hidden も返る（e2e が hidden ジョブを実行時登録する）."""
        catalog = await client.jobs()

        assert catalog.jobs
        assert any(job.hidden for job in catalog.jobs)

    async def test_responses_are_not_cached(
        self, client: MachineClient, backend_app: FastAPI
    ):
        """他の操作者による変更が次のページ描画で見える（キャッシュしない）."""
        before = await client.state()
        backend_app.state.appstate.select_pcb(Path("boards/sample.kicad_pcb"))

        after = await client.state()

        assert (before.pcb_file, after.pcb_file) == (None, "boards/sample.kicad_pcb")


class TestBackendUnavailable:
    """到達失敗と契約違反の応答（→ 503 ページ）."""

    async def test_unreachable_backend_raises_with_its_base_url(self):
        """実 ECONNREFUSED（誰も listen していないポート）."""
        endpoint = MachineEndpoint(machine_id="dead", host="127.0.0.1", port=1)

        async with aclosing(
            BackendGateway(connect_timeout=2.0, read_timeout=2.0)
        ) as gateway:
            with pytest.raises(BackendUnavailable) as raised:
                await MachineClient(endpoint, gateway).machine_info()

        assert "http://127.0.0.1:1" in str(raised.value)
        # 503 ページに「どのマシンが応答しないか」を出すため
        assert raised.value.endpoint is endpoint

    async def test_error_status_is_reported_as_unavailable(self):
        """5xx は本文の形が合っていても値として使わない.

        本文を契約どおりに組んだ 503 を素材にするのは、判定が**ステータス**で
        行われていることを観測するため（本文の検証だけに頼っていると、
        エラー本文がたまたま契約を満たす応答をそのまま画面に流してしまう）。
        """
        async with _broken_gateway(
            JSONResponse(_VALID_STATE_BODY, status_code=503)
        ) as gateway:
            with pytest.raises(BackendUnavailable):
                await MachineClient(_BROKEN, gateway).state()

    async def test_response_off_contract_is_reported_as_unavailable(self):
        """必須フィールドを欠く応答を通すと、テンプレートが描画中に落ちる."""
        async with _broken_gateway(JSONResponse({"busy": True})) as gateway:
            with pytest.raises(BackendUnavailable):
                await MachineClient(_BROKEN, gateway).state()

    async def test_non_json_body_is_reported_as_unavailable(self):
        """プロキシや別サービスが 200 で HTML を返しても値としては使えない."""
        async with _broken_gateway(HTMLResponse("<html>not me</html>")) as gateway:
            with pytest.raises(BackendUnavailable):
                await MachineClient(_BROKEN, gateway).machine_info()


_BROKEN = MachineEndpoint(machine_id="broken", host="127.0.0.1", port=8081)

# StateResponse を満たす本文（ステータスだけが異常な応答の素材）
_VALID_STATE_BODY = {
    "pcb_file": None,
    "busy": False,
    "busy_owner": None,
    "focus_z": None,
    "mainsail_url": None,
    "preview_clients": 0,
    "job": None,
    "nozzle_cap": None,
}


def _broken_gateway(response: Response) -> aclosing[BackendGateway]:
    """どの GET にも同じ応答を返す上流を挿した gateway."""
    app = FastAPI()

    @app.get("/{path:path}")
    async def any_path(path: str) -> Response:
        return response

    return aclosing(
        BackendGateway(
            connect_timeout=2.0,
            read_timeout=2.0,
            transport_factory=lambda _endpoint: httpx.ASGITransport(app=app),
        )
    )
