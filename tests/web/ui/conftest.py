"""UI frontend テスト共有フィクスチャ.

frontend（`web.ui`）を `fastapi.testclient.TestClient` で駆動し、上流には実物の
backend app（`web.api.create_app`）を httpx の `ASGITransport` で in-process に挿す。
`ASGITransport` は httpx 純正の transport なので 3rd-party のモックではない
（`transport_factory` の注入口は本番では実 TCP transport が入る）。

**in-process で書けるのは JSON プロキシと SSR ページだけ**:

- `ASGITransport` はレスポンスを最後までバッファしてから返すので、MJPEG
  （終端しない multipart）を読むと永久にハングする
- `ASGITransport` は websocket scope を扱えないので WS 中継も検証できない
- backend 側の lifespan も走らない（`JobManager.bind_loop` が呼ばれないため
  ジョブ実行と WS イベント配信は成立しない）

→ MJPEG / WS / ジョブ実行は `tests/e2e` で実 uvicorn（実ソケット経由）で検証する。

Settings は env ではなく直接構築して注入する（env はプロセスグローバルで leak する）。
`ui_settings.machines_file` は tmp_path の不在パスにして、リポジトリの
`config/machines.toml` をテストが拾わないようにする。
"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import copy_testing_config
from web.api.app import create_app as create_backend_app
from web.api.settings import Settings as ApiSettings
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings

# frontend の登録 id と backend の自己申告 id を一致させる（SSR ページが
# backend から取った machine_id と URL の machine_id を突き合わせられるように）
BACKEND_MACHINE_ID = "uitest"
BACKEND_MACHINE_NAME = "UI テスト機"


@pytest.fixture
def backend_pcb_root(tmp_path: Path) -> Path:
    """Backend 側の PCB ブラウザ root（アップロードの着地確認に使う）."""
    root = tmp_path / "backend" / "pcb"
    (root / "boards").mkdir(parents=True)
    (root / "boards" / "sample.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    return root


@pytest.fixture
def backend_settings(tmp_path: Path, backend_pcb_root: Path) -> ApiSettings:
    """上流 backend の Settings（`data/testing/config` を tmp_path へコピーして使う）."""
    data_dir = tmp_path / "backend" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return ApiSettings(
        config_dir=copy_testing_config(tmp_path / "backend"),
        data_dir=data_dir,
        pcb_browse_root=backend_pcb_root,
        # 公開範囲はセキュリティ境界なので既定から暗黙に広げない
        pcb_browse_allowed=(backend_pcb_root,),
        pcb_browse_start=backend_pcb_root,
        pcb_upload_dir=backend_pcb_root / "uploads",
        mainsail_url="http://mainsail.invalid",
        hostname=BACKEND_MACHINE_ID,
        # 実 LAN へ mDNS を撒かない
        discovery_enabled=False,
    )


@pytest.fixture
def backend_app(backend_settings: ApiSettings) -> Iterator[FastAPI]:
    """実物の backend app。lifespan は走らないので後始末だけ明示的に行う."""
    app = create_backend_app(backend_settings)
    yield app
    app.state.preview.request_shutdown()
    app.state.jobs.shutdown()
    app.state.appstate.close()


@pytest.fixture
def ui_settings(tmp_path: Path) -> Settings:
    """静的登録 1 台の frontend Settings（machines_file は不在パス）."""
    return Settings(
        machines=(
            MachineEndpoint(
                machine_id=BACKEND_MACHINE_ID,
                host="127.0.0.1",
                port=8081,
                name=BACKEND_MACHINE_NAME,
            ),
        ),
        machines_file=tmp_path / "machines.toml",
        # 実 LAN を探索しない（探索は tests/web/ui/test_discovery.py と e2e が見る）
        discovery_enabled=False,
    )


@pytest.fixture
def frontend_app(ui_settings: Settings, backend_app: FastAPI) -> FastAPI:
    """上流を in-process backend に差し替えた frontend app."""
    # 遅延 import: conftest 自体は `web.ui.app` が無い時点でも import できること
    # （settings / machines のテストは app に依存しない）
    from web.ui.app import create_app

    return create_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend_app),
    )


@pytest.fixture
def frontend_client(frontend_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(frontend_app) as test_client:
        yield test_client
