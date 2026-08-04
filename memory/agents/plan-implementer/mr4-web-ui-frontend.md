
## トラック 0（基盤: settings / machines / conftest）

### 計画外の判断ログ

- `machines.toml` の `port` 省略を許し、既定を `web.ui.machines.DEFAULT_BACKEND_PORT = 8081`
  にした。契約書の `load_machines_file(path)` シグネチャを変えないため引数では渡さず、
  `Settings.default_backend_port` の既定値もこの定数を参照する（8081 の重複定義を作らない）。
- `machines.toml` の不正な記述（`machine_id` / `host` 欠落・空文字、`port` が非整数、
  `name` / `machine_type` が非文字列、`machine` が配列でない）は `ValueError` で起動時に落とす。
  運用者が手で書く境界の入力なので、黙って 1 台落ちるより気付けるほうがよい。
  ファイル不在だけは空 tuple（単独起動の要件）。
- `MachineRegistry` は重複 `machine_id` を弾かない（`list()` に両方残り、`resolve()` は
  後勝ち）。マージ規則は MR5 の担当なので先取りしない。
- `tests/web/ui/conftest.py` は `tests/web/api/conftest.py` のフィクスチャを import せず、
  必要な backend Settings を自前で組んだ（非トップレベル conftest の `pytest_plugins` は
  pytest 8 で使えず、conftest 間 import は依存が見えなくなるため）。
  契約書の 4 フィクスチャに加えて `backend_settings` / `backend_pcb_root` を公開している
  （アップロード着地の確認に `backend_settings.pcb_upload_dir` を使える）。
  backend の machine_id は `hostname="uitest"`（`BACKEND_MACHINE_ID`）で固定、frontend の
  静的登録 1 台も同じ id。`ui_settings.machines_file` は tmp_path の不在パス
  （リポジトリの `config/machines.toml` をテストが拾わないため）。
- `backend_app` フィクスチャは `ASGITransport` が lifespan を走らせないため、teardown で
  `preview.request_shutdown()` / `jobs.shutdown()` / `appstate.close()` を明示的に呼ぶ。
  ジョブ実行・WS は in-process では成立しない（conftest docstring に明記）。

### 検証結果（トラック 0 の所有ファイルのみ。全体検証は orchestrator）

- `uv run pre-commit run --files <所有ファイル>`: pass
- `uv run pyright <所有ファイル>`: `tests/web/ui/conftest.py:101` の
  `Import "web.ui.app" could not be resolved` のみ（トラック C が `app.py` を作れば解消。
  遅延 import なので conftest の import 自体は通る）。他はエラーなし
- `uv run pytest tests/web/ui/test_settings.py tests/web/ui/test_machines.py -m "not hardware"`:
  41 passed
- mutation 17 件（label の書式 3 種 / base_url の port / `list()` のソート /
  `resolve()` の None 返し / ファイル不在防御の除去 / port 既定の除去 / 型検証の除去 3 種 /
  `machine` 非配列検証の除去 / `from_env` の env 無視 / port 既定 8081 化 /
  proxy_read_timeout 短縮 / `config_dir` フィールド追加 / `PCBASM_API_PORT` 読み）を当て、
  **全件が対応テストで落ちた**。`machine` 非配列の検証は当初 `machine = "alpha"` では
  要素側の防御で吸収されて生き残ったため、テスト素材を `machine = 5`（非 iterable）に
  変更して kill した

## トラック B（backend クライアント + 表示知識 + 静的資産の移設）

### IF 変更（orchestrator が 3 件すべて採用。契約書冒頭「★ 確定した IF 変更」に反映済み）

- `web.ui.layout` の名前を public 化した: `JOB_TEMPLATES` /
  `PASTE_AUTO_THRESHOLD_KEYS` / `LOADING_ROTATION_PARAMS` /
  `DISPENSE_CALIBRATION_PARAM_GROUPS` / `grouped_fields`（契約書は `_` 付き）。
  `layout.py` は `pages.py` に import されるためだけに存在するモジュールなので、
  中身は全部そのモジュールの公開 API。pyright の `reportPrivateUsage = "warning"` が
  クロスモジュールの `_` 名アクセスを警告するため、放置するとトラック C の
  `pages.py` が全部踏む。
- `MachineSettingsResponse` を `web.api.routers.settings_api` から `web.api.models` へ
  移した。契約書「モデルは `web.api.models` から import する」を満たすため
  （`settings_api` から import すると frontend が fastapi router と pcbasm config を
  引き込む）。MR2 の `StateResponse` / `SettingsField` 移設と同じパターン。参照は
  同ファイル内だけだったので既存 import は壊れない。
- `BackendGateway(read_timeout=...)` には `Settings.ssr_timeout` を渡す前提にした。
  契約書のトラック A がプロキシ側でリクエスト毎に `read` を上書きする
  （`/preview/stream` は `None`、他は `proxy_read_timeout`）と定めているため、
  gateway の既定値は SSR 用にするのが唯一整合する解釈。gateway は 1 個のまま。

### 計画外の判断ログ

- `machine_selector.html` の `<option value>` はテンプレートで組んだ（JS では組まない）。
  契約書は「`option.value = /m/{id}/{current_suffix}`」だが、サーバが組めば
  `machine_selector.js` は `location.assign(select.value)` の 3 行で済み、
  skill webui-thin-wrapper の「表示文字列と URL はサーバ側」に沿う。
- `base.html` に `{% include "partials/machine_selector.html" %}` と
  `machine_selector.js` の `<script>` を足した（契約書の「10 箇所」に含まれないが、
  include されない partial は dead code なので不可避）。CSS は追加していない
  （`app.css` の汎用 `select` ルールで topbar の他要素と揃う）。
- `BackendUnavailable` は `endpoint` だけ public にした。503 ページに「どのマシンが
  応答しないか」を出すのに必要。`cause` は属性に持たずメッセージへ畳み込み、連鎖は
  `raise … from` に任せる（`__cause__` で辿れるものを二重に持たない）。
- `BackendGateway` のキャッシュキーは `machine_id` のみ（契約書どおり）。MR4 では
  エンドポイントが起動時に読んだ静的登録だけで host/port が不変なので、キャッシュの
  無効化規則は書かない（起こり得ないシナリオ）。mDNS で host が変わりうる MR5 では
  必要になる旨を docstring に明記した。
- `_fetch` は 4xx/5xx・JSON 不正・スキーマ不一致をすべて `BackendUnavailable` に
  畳む。「契約を満たさない応答」と「到達できない」を UI 上で区別する意味がない
  （どちらも欠損フォームを描いてはいけない）。
- このファイル群は async なので、リポジトリで初めて anyio の pytest プラグイン
  （`pytest.mark.anyio` + `anyio_backend` フィクスチャ）を使う。anyio は
  `pytest11` entry point で自動ロードされ、marker もプラグインが登録するので
  `--strict-markers` と両立する。新規依存の追加は無し。

### 所有権表外の付随変更（orchestrator が承認済み）

- `src/web/api/app.py`: `/static` マウント・`_static_asset_url` / `_NoCacheStaticFiles` /
  `_STATIC_DIR` / `_STATIC_CACHE_CONTROL` に加え、
  `app.state.templates.env.globals["static_asset"] = ...` の行も削除した（削除した
  関数を参照するため不可避）。`app.state.templates = Jinja2Templates(...)` と pages
  ルータ登録はトラック C のために残してある。
- `tests/web/api/test_app.py`: `test_static_css_is_served` を
  `test_static_assets_are_not_served_by_backend`（404）に置き換えた。
- `src/web/api/models.py` / `routers/settings_api.py`: 上記 `MachineSettingsResponse`
  の移設。

### トラック C への申し送り

- `static_asset` の実装（`_static_asset_url` / `_NoCacheStaticFiles` / `/static`
  マウント）は `src/web/ui/app.py` に必要。`base.html` が `static_asset()` を 5 箇所で
  呼ぶため、無いと全ページが Jinja の undefined で落ちる（orchestrator が契約書に
  追記済み）。
- テンプレートは `base` / `machine_id` / `machines` / `current_suffix` を context に
  要求する。`base` は Jinja の undefined が空文字に落ちるので、マシン非依存ページ
  （0 台の案内・ピッカー）では未設定でも壊れない。

### 検証結果（トラック B の所有ファイルのみ。全体検証は orchestrator）

- `uv run pre-commit run --files <所有ファイル>`: pass
- `uv run pyright <所有ファイル>`: 0 errors, 0 warnings
- `uv run pytest tests/web/ui/test_layout.py tests/web/ui/test_machine_client.py
  tests/web/api/test_app.py tests/web/api/test_models.py -m "not hardware"`: 61 passed
- `grep -rn '</content>' src tests`: 空
- `/api` literal は 31 本すべてが `app.js` の `api()` を通る（実測）。`fetch(` の
  直接呼び出しは 2 箇所だけで、`app.js` の funnel と `job_console.js` の完了音
  （`/static` は prefix しない）。WS URL 1 本と成果物 URL 1 本は `withBase` を明示。
  この 4 性質は `TestMachinePrefixFunnel` が静的にピンする（JS にテストランナーが
  無いため）
- **mutation 35 件を当て、全件が対応テストで落ちた（生き残り 0）。**
  内訳: machine_client 15 件（raise_for_status 除去 / キャッシュ無効化 / base_url 無視 /
  timeout 無視 / aclose の clear・close 除去 / ValueError 除去 2 種 / endpoint 保持と
  メッセージ / レスポンスキャッシュ導入 / 取得先 URL の誤り 4 種）、layout 12 件
  （TAB_PHASES・TAB_LABELS の欠落 / テンプレート名の typo / SECTION_LABELS の乖離 2 種 /
  `rsplit`→`split` 2 種 / 定義順の破壊 / 未知セクションのフォールバック / slug 重複 /
  未宣言 feature / 未割り当て JOB_TEMPLATES）、テンプレ・JS 7 件（selector 読み込み除去 /
  存在しない資産参照 / funnel の prefix 除去 2 種 / WS URL / 成果物 URL /
  funnel を通らない直接 fetch の追加）、api/app.py 1 件（`/static` マウントの復活）。
  リポジトリは影を作らず `cp` 相当のバックアップ + `try/finally` で毎回復元し、
  最後に `git status` と全テスト再実行で無変更を確認した
- **`test_error_status_is_reported_as_unavailable` は当初 vacuous だった。**
  異常応答の素材を `{"detail": "boom"}` にしていたため、`raise_for_status()` を外しても
  本文側の `ValidationError` が拾って `BackendUnavailable` になり mutation が生き残る。
  素材を「StateResponse を満たす本文 + 503」に変えて、判定が**ステータス**で
  行われていることを観測する形にした
- 実機テスト（`make test` / `@mark_hardware`）は未実行。ブラウザでの MJPEG / WS /
  完了音 / 成果物リンクの確認はユーザー

## トラック A（リバースプロキシ: src/web/ui/proxy.py）

### IF 変更通知（トラック C の app.py に影響）

- **`ProxyApp.__init__` に `read_timeout: float` を必須 kwarg で追加した**。
  契約書のシグネチャ（`registry, gateway, *, target_prefix`）＋ 1 引数。
  app.py 側は
  `ProxyApp(registry, gateway, target_prefix="/api", read_timeout=settings.proxy_read_timeout)`
  で構築する（`/artifacts` も同じ）。
  理由: トラック B の `BackendGateway` は `read_timeout` を **SSR 用の既定値**として
  `httpx.AsyncClient` に持たせる設計（docstring に「プロキシは用途ごとに上書きする」と
  明記）。app.py が gateway に渡すのは `ssr_timeout=2.0` になるため、プロキシが client
  既定をそのまま使うと `POST /api/machine-control`（M400 待ちで最大 60s）が 2 秒で 504
  になる。gateway 側を 120s にして SSR で都度上書きする案は、`MachineClient._fetch` が
  `client.get()` を素で呼ぶ設計のため SSR が 120s 待つことになり却下。
  既定値は付けない（渡し忘れが静かに通るより、pyright が統合時に指摘するほうがよい）。
- write timeout も gateway 既定（SSR 用）ではなく `read_timeout` を使う。上流へ PCB
  ファイルのボディを流す時間なので 2 秒では足りない。

### 計画外の判断ログ

- **`starlette.routing.get_route_path` を使わず `_route_path` に移植した。**
  starlette 1.3.0 は再エクスポートしておらず pyright の `reportPrivateImportUsage` で
  エラーになる（`src/` に `pyright: ignore` は 1 件も無いので増やさない方針。
  `starlette._utils` からの import も private モジュールなので採らない）。
  orchestrator の裁定に従い `starlette._utils.get_route_path` の **4 分岐をそのまま**
  移植した（当初は 2 分岐に圧縮していた。`root_path` に前方一致するがセグメント境界で
  ない場合に完全パスを返す分岐が欠けていた。`Mount` の path_regex が境界に `/` を
  要求するため到達しないが、docstring が「移植」と言うなら等価でなければ誤読を招く）。
- **ボディは「ある時だけ」上流に渡す**（`_has_body` = `content-length` か
  `transfer-encoding` がある）。計画書どおり無条件に `request.stream()` を渡すと httpx が
  GET にも `Transfer-Encoding: chunked` を付け、body なし GET を chunked で受けない上流に
  当たると壊れる。クライアントの `content-length` はそのまま転送するので上流も chunked に
  ならない（bytes をそのまま流すため長さは一致する）。
- リクエストの `connection` は落とすが、httpx が自前の既定ヘッダとして
  `connection: keep-alive` を必ず付け直す。よって「クライアントの `connection: close` が
  上流に漏れない」という形でテストしている（完全な不在は検証できない）。
- 非ストリーム応答も `stream=True` + `_UpstreamResponse` の 1 経路で扱う。JSON /
  artifacts / Range 206 / MJPEG を分岐させず、上流 close の保証も 1 箇所に集約できる。
  `aiter_raw()` を使い content-encoding を解かないので content-length /
  content-encoding をそのまま転送でき、multipart も再解析されない。
- `CancelledError` は上流を close したあと **re-raise する**
  （`web/api/routers/preview.py` の同期版は握り潰しているが、キャンセルを飲むと
  サーバー側のシャットダウン処理が進まないため踏襲しない）。
- WS の close も `finally` で `anyio.CancelScope(shield=True)` 越しに行う（MJPEG と同じ
  理由。上流 WS を閉じ残すと backend 側の購読が残る）。上流の close code は
  1005/1006/None を 1011 に丸め、クライアントが既に切断していれば close を送らない
  （`WebSocketState.CONNECTED` を確認）。
- 未知 machine_id の WS は accept せず 1011 で close（HTTP は 404 JSON）。

### テストの切り分け（計画書との差分）

- `tests/web/ui/test_proxy.py` は **`web.ui.app.create_app` を使わず** Starlette +
  `Mount("/m/{machine_id}/api" / "/artifacts")` を自前で組んで ProxyApp 単体を検証する
  （トラック C の app.py が未着でも自己検証できるようにするため）。
  → 計画書の「`/m/x/api/state` が pages ルータに食われない（登録順の回帰）」は
  **app.py の仕様なのでトラック C の `tests/web/ui/test_pages.py` 側に置く必要がある**。
  `/m/dead/posctrl` が 503 HTML も同様に pages 側。
- 上流は 2 種類。実 backend app（実 API 契約の確認: machine-info / pcb-file PUT /
  実 .kicad_pcb の multipart アップロード / 409 `{detail, owner}` / artifacts bytes）と、
  ヘッダ加工検証用のエコー ASGI 上流。`date` / `server` / 多値 `set-cookie` は uvicorn が
  付けるヘッダで in-process の実 backend では観測できないため、上流から送って
  「落としている」ことを確かめる（in-process で言えるのは「上流の分が 0 本」まで。
  「1 本ずつ」は uvicorn が 1 本足す e2e 側の主張）。
- PATCH の透過はエコー上流で検証した。実 backend の PATCH は
  `/api/pasting/pad-config/*` しかなく、実 pad config の選択が前提で
  プロキシの検証には過剰なため。
- read timeout（504）は `tests/web/ui/test_proxy_unreachable.py` で
  **accept だけして何も返さない実ソケット**に対して検証する（`MockTransport` 不使用）。
  502 は `127.0.0.1:1` への実 ECONNREFUSED。
- MJPEG / WS の in-process テストは書かない（`ASGITransport` はバッファし websocket
  scope も扱えないので永久ハングする）。上流 close 漏れによるカメラリーク、WS 中継、
  close code、`proxy=None` はトラック D の `tests/e2e/test_proxy_e2e.py` が 2 プロセスで
  押さえる。

### 依存の指摘（pyproject は未所有なので触っていない）

- `src/web/ui/proxy.py` は `websockets` を直接 import するが、現在 `websockets` は
  `uvicorn[standard]` 経由の間接依存。`pyproject.toml` の dependencies に明示追加するか
  orchestrator の判断を仰ぐ。

### 検証結果（所有ファイルのみ。全体検証は orchestrator）

- `uv run pre-commit run --files src/web/ui/proxy.py tests/web/ui/test_proxy.py
  tests/web/ui/test_proxy_unreachable.py`: pass
  （`te` ヘッダ名は codespell が誤検出するため `# codespell:ignore te` を付けた）
- `uv run pyright <所有 3 ファイル>`: 0 errors
- `uv run pytest tests/web/ui/test_proxy.py tests/web/ui/test_proxy_unreachable.py
  -m "not hardware"`: 38 passed。`tests/web/ui/` 全体（トラック 0 / B と合わせて）125 passed
- **mutation 19 件を当て、全件が対応テストで落ちた（生き残り 0）。**
  内訳: `date`/`server` を残す / レスポンス hop-by-hop を残す / 多値ヘッダを dict で潰す /
  上流ヘッダを転送しない / 上流 status を無視 / `query_string` を付け直さない /
  `target_prefix` を付けない / `cookie` を残す / リクエスト hop-by-hop を残す /
  `host` を残す / `x-forwarded-for` を足さない / 同ヘッダを連鎖せず上書き /
  ボディを常に stream で渡す（GET が chunked になる）/ ボディを渡さない /
  未知 machine_id を 1 台目へ流す / ConnectError→504 / ReadTimeout→502 /
  read timeout を全経路で外す / `_route_path` が mount prefix を残す。
  リポジトリは `try/finally` で毎回復元し、最後に全テスト再実行と `git status` で
  無変更を確認した
- **`pytest.mark.timeout` は `TestClient` の待ちを中断できない**（read timeout を外す
  mutation で 90s 経っても中断されなかった。pytest-timeout 2.4.0 は導入済み）。
  → 504 のテストは締め切りをテスト側に持たせた（daemon スレッドで GET し
  `join(15s)` で返らなければ `pytest.fail`）。**ハングは「落ちない」と同じなので、
  以後この種のテストで marker に頼らない。** `_client()` は `with` を使わない
  （応答が返らないとき `TestClient.__exit__` が in-flight を待って共倒れになる）
- **in-process では書けない 2 経路を実 uvicorn の smoke で確認した**
  （scratchpad の使い捨てスクリプト。commit していない。上流も自作の最小 ASGI アプリで、
  実機・カメラは一切関与しない）:
  - WS: 双方向中継 / `?job=paste` の query 転送 / `x-forwarded-for` 付与 /
    `cookie` 不到達 / 上流の close 4001 と reason がクライアントへ到達 /
    クライアント切断が上流に伝わる（上流の `finally` が走る）/ 未知 machine_id と
    不達 backend は **accept されず handshake 拒否（HTTP 403）** = job_console.js の
    バックオフ再接続が効く形
  - MJPEG: 終端しない multipart を frontend 経由で 512B 読んで close すると、
    上流の参照カウントが 1 → **0** に戻る（= 上流 `httpx.Response` を閉じている。
    カメラリーク回帰の本体）
- 実機テスト（`make test` / `@mark_hardware`）は未実行

### トラック D への申し送り（e2e で踏む罠）

- 「2 クライアント同時 → 片方 close で 1」を **1 つの `httpx.AsyncClient` から 2 本
  stream して確認しないこと**。同一クライアントだと片方の close で上流が両方
  終わる（frontend を通さず上流へ直接つないだ対照実験でも同じ挙動なので、
  プロキシではなく httpx / uvicorn 側の性質）。クライアントを 2 つ作るか、
  ブラウザ 2 タブで確認する。

______________________________________________________________________

## トラック B（backend クライアント + 表示知識 + 静的資産の移設）

### 計画外の判断ログ

- **`web.ui.layout` の名前を public にした**（契約書は `_JOB_TEMPLATES` /
  `_PASTE_AUTO_THRESHOLD_KEYS` / `_LOADING_ROTATION_PARAMS` /
  `_DISPENSE_CALIBRATION_PARAM_GROUPS` / `_grouped_fields` と `_` 付きで列挙）。
  `layout.py` は `pages.py` に import されるためだけに存在するモジュールで、
  pyright の `reportPrivateUsage = "warning"`（pyproject）がクロスモジュールの
  `_` 名アクセスを警告する = トラック C の `pages.py` が全部踏む。
  → `JOB_TEMPLATES` / `PASTE_AUTO_THRESHOLD_KEYS` / `LOADING_ROTATION_PARAMS` /
  `DISPENSE_CALIBRATION_PARAM_GROUPS` / `grouped_fields`。**IF 変更として
  orchestrator に通知済み。**
- **`MachineSettingsResponse` を `web.api.routers.settings_api` から
  `web.api.models` へ移した**（`settings_api` は models から import する形に変更。
  参照は同ファイル内だけだったので既存 import は無改変）。契約書「モデルは
  `web.api.models` から import する（pydantic のみ import の contract モジュール）」を
  満たすため。`settings_api` から import すると frontend が fastapi router と
  pcbasm config を引き込み、`tests/web/api/test_models.py` が守っている境界の意味が
  失われる。MR2 の `StateResponse` / `SettingsField` 移設と同じパターン。
  **所有権表に無いファイル 2 件（`models.py` / `settings_api.py`）を触ったので通知済み。**
- **`BackendGateway.read_timeout` は `Settings.ssr_timeout` を渡す想定**にした
  （docstring に明記）。契約書のトラック A が「タイムアウトは `/preview/stream` の
  ときだけ `read=None`、他は `proxy_read_timeout`」とプロキシ側でリクエスト毎に
  上書きすると定めているため、gateway 1 個のままで SSR の既定を持たせられる。
  gateway に `proxy_read_timeout`（120s）を持たせると SSR が 120s 待つ。
- **`BackendGateway` のキャッシュ更新規則は入れていない**（`machine_id` キーのみ）。
  MR4 のエンドポイントは起動時に読む静的登録だけで host/port が不変なので、
  現時点では到達不能なシナリオ。MR5（mDNS で host/port が変わる）で必要になる旨を
  クラス docstring に残した。
- `BackendUnavailable` は `endpoint` だけを public 属性にした（503 ページが
  「どのマシンが応答しないか」を出すため）。`cause` はメッセージに畳み込み、連鎖は
  `raise … from` に任せて属性としては持たない。
- `machine_selector.html` の `option.value` と表示 `label` は**テンプレート側で
  サーバが組む**（契約書は「`option.value = /m/{id}/{current_suffix}`」とだけ指定）。
  `machine_selector.js` は `location.assign(select.value)` の 1 行だけになる
  （skill webui-thin-wrapper）。
- **`base.html` に 2 行追加した**（契約書の「10 箇所」に含まれない）:
  `{% include "partials/machine_selector.html" %}` と
  `<script src="{{ static_asset('js/machine_selector.js') }}">`。
  include しないと新規パーシャルと JS が dead code になる。
- **`src/web/api/app.py` の `app.state.templates.env.globals["static_asset"] = …`
  行も削除した**（削除対象の `_static_asset_url` を参照するため不可避）。
  `app.state.templates = Jinja2Templates(...)` と pages ルータ登録はトラック C の
  ために残してある = 統合前の api 側は「テンプレートディレクトリが無いのに
  Jinja2Templates を構築する」状態（`FileSystemLoader` は構築時に存在検証しないので
  import も create_app も通る。backend の `test_pages.py` はトラック C が移設する）。
- **`tests/web/api/test_app.py` の `test_static_css_is_served` を
  `test_static_assets_are_not_served_by_backend`（404）に置き換えた**。
  `/static` マウント削除の直接の帰結で、所有権表に無いが他トラックの担当でもない。
- **`machine_control.js` のポーリング制御を関数に切り出した**（`startPolling` /
  `stopPolling` / `updatePolling`）。契約書は「`visibilitychange` で停止（3 行）」だが、
  既存の折りたたみ状態による停止/再開と条件が二重になるため、
  「折りたたみ中 **または** 背景タブなら止める」を 1 箇所に集約した。
  `applyCollapsed` に `document.hidden` を渡す形だと、背景タブに回った時点で
  サイドバーの CSS クラスとトグル記号まで折りたたみ側に書き換わってしまう。
- **リポジトリ初の async テスト**を追加した（`tests/web/ui/test_machine_client.py`）。
  `MachineClient` は async 専用で `TestClient` 越しに叩けないため。anyio の
  pytest プラグインは `pytest11` エントリポイントで自動ロードされるので依存追加は
  不要（`pytest.mark.anyio` + `anyio_backend` フィクスチャ、`--strict-markers` も通る）。
  gateway の後始末は `contextlib.aclosing` に載せた（`aclose()` が protocol に合う）。
- **JS / テンプレの prefix funnel を静的にピンした**
  （`test_layout.py::TestMachinePrefixFunnel`）。JS にテストランナーが無く、
  「prefix 付与点は `app.js` の `withBase()` 1 箇所」（計画書の設計判断 #4）を
  守る仕組みが他に無いため: 直接 `fetch` は app.js（funnel）と job_console.js
  （完了音の `/static`）にしか無い / funnel が `document.body.dataset.machineBase`
  を読んで `fetch(withBase(url))` する / WS URL と `artifact.url` が各 1 箇所で
  `withBase` を通る。

### JS / テンプレの編集（契約書の 10 箇所）

`/api` literal は非コメント行で 31 本（うち WS URL 1 本は `withBase` を明示）。
`grep` で確認したとおり **31 本すべてが `app.js` の `api()` を通る**。
直接 `fetch(` は 2 箇所のみ（`api()` 内の funnel と完了音の `/static`）。

「変更しない」リストは全件そのまま: `settings.html:72` / `paste_solder.html:11` の
`data-endpoint`、`preview_pane.html:2` の `data-stream-url`（bare）、
`job_console.js` の完了音 `/static`、`app.js` の `window.location.reload()` 2 箇所、
`machine_control.js` の localStorage 2 キー。

### 検証結果（トラック B の所有ファイル + 付随変更のみ。全体検証は orchestrator）

- `uv run pre-commit run --files <所有ファイル + api の 3 ファイル>`: pass
- `uv run pyright <同上>`: 0 errors, 0 warnings
- `uv run pytest tests/web/ui tests/web/api/test_app.py tests/web/api/test_models.py
  tests/web/api/routers/test_settings_api.py -m "not hardware"`: 164 passed
  （うち自分の追加分 46 件）
- **mutation 30 件を影コピー（`/tmp/claude-1000/mr4b/src` + `PYTHONPATH`）に当て、
  全件が対応テストで落ちた。リポジトリの `src/` は一度も書き換えていない。**
  内訳: machine_client 13 件（`raise_for_status` 除去 / 検証を `model_construct` 化 /
  例外変換の範囲を `RuntimeError` に狭める / `endpoint` 属性の除去 / メッセージから
  base_url を落とす / キャッシュ無効化 / キャッシュキーが machine_id を無視（読み・書き）/
  `aclose` がキャッシュを残す / `aclose` が閉じない / connect と read の入れ替え /
  base_url の無視 / レスポンスのキャッシュ追加）、layout 9 件（`machine_name` ラベル削除 /
  `section_of` を先頭セグメントに / `grouped_fields` のソート / ラベルフォールバック除去 /
  テンプレート名のタイポ / TAB_PHASES・TAB_LABELS の欠落 / 未割当テンプレートの
  JOB_TEMPLATES 追加 / 未宣言 feature の FEATURE_TEMPLATES 追加）、
  テンプレ・JS 8 件（static_asset のタイポ / machine_selector の JS 未読込 /
  パーシャル未 include / funnel が prefix しない / `machineBase` を読まない /
  WS URL 未 prefix / artifact URL 未 prefix / 他 JS からの直接 `fetch`）。
  最初の測定で 2 件が生き残ったが、どちらも **mutation 側の欠陥**だった:
  `base_url` をテストの endpoint と同値（`127.0.0.1:8081`）に置換していたため no-op、
  キャッシュ追加を「読み」と「書き」の 2 件に分けたため単体では no-op。
  素材を直して再測定し kill を確認した。
- `grep -rn '</content>' src tests`: 空

### 未了 / 他トラック待ち

- `web.ui.app`（トラック C）が無いため `/static` マウント・`Jinja2Templates`・
  例外ハンドラ（`BackendUnavailable` → 503 + `Retry-After: 5`）は未配線。
  `machine_selector.html` は `machines` / `machine_id` / `current_suffix` を
  トラック C の `_base_context` から受ける（未定義でも Jinja は空文字で描画する）。
- MJPEG / WS / 完了音 / 成果物リンクの実挙動はトラック D の e2e（2 プロセス）で検証。
  in-process（`ASGITransport`）では検証できない。

### トラック A 追記: mutation 検証と後から入れた修正

- `_route_path` は `starlette._utils.get_route_path` の全分岐（`root_path` 無し /
  前方一致しない / `path == root_path` / セグメント境界）を移植した形に直した。
  境界でない前方一致には Mount の path_regex の性質上到達しないことをコメントで明示。
- `tests/web/ui/test_proxy_unreachable.py` は `pytest.mark.timeout` ではなく
  **テスト側の締め切り（別スレッド + `Thread.join(15s)`）** でハングを失敗に変える。
  `pytest.mark.timeout` は `TestClient` のブロッキング待ちを中断できず、read timeout を
  外す mutation を当てるとスイート全体が止まってしまったため。`TestClient` を `with` で
  使わないのも同じ理由（`__exit__` が in-flight のリクエストを待つ）。
- **mutation 19 件を当て、全件が対応テストで落ちた**（生き残りなし）:
  date/server を落とさない / レスポンス hop-by-hop を落とさない / 多値ヘッダを dict で
  潰す / 上流ヘッダを転送しない / 上流 status を無視する / query_string を付け直さない /
  target_prefix を付けない / cookie を落とさない / リクエスト hop-by-hop を落とさない /
  host を落とさない / x-forwarded-for を足さない / x-forwarded-for を上書きする /
  ボディを常に stream で渡す（GET が chunked になる）/ ボディを渡さない /
  未知 machine_id を無視して 1 台目へ流す / ConnectError を 504 にする /
  ReadTimeout を 502 にする / read timeout を全経路で外す（MJPEG 判定の反転）/
  route_path が mount prefix を残す。
  スクリプトは `<scratchpad>/mut_trackA.py`、ログは `<scratchpad>/mut_trackA.log`。
  proxy.py は実行後に backup から復元済み（`git diff` で確認可）。
- カバーできていない振る舞い（意図的。トラック D の e2e が担当）:
  MJPEG の上流 close（`stream_response` finally / `CancelledError` の両経路）、
  WS の中継・accept 順・`proxy=None`・close code 丸め、`read=None` の実効性。

______________________________________________________________________

## トラック C（pages / app / __main__ + test_pages 移設）

### IF 変更通知（他トラック・orchestrator に影響）

- **`src/web/ui/templates/message.html` を新規作成した（トラック B 所有ディレクトリ）。**
  契約書はマシン非依存ページ（0 台の案内・複数台のピッカー）と例外ハンドラの
  503 / 404 を「`base.html` で描く」と定めているが、その本文テンプレートが存在せず、
  トラック C は `templates/` を所有しない。Python 側に HTML を埋め込むより
  1 ファイル追加が妥当と判断した（B が触らない新規ファイルなので同時編集の衝突は無い）。
  `base.html` を extends して `{% block content %}` に title / detail を出すだけ。
  chrome だけで描けるので backend が落ちていてもマシン切替ドロップダウンが出る。
- **`pages.render_message()` を public にした**（`app.py` の例外ハンドラが呼ぶ）。
  テンプレート名とコンテキストの組み立てを pages.py に閉じるため。
  `reportPrivateUsage = "warning"` 対策でもある（契約書 §1 と同じ理由）。
- 未 prefix の入口（`/`・`/{tab}[/{feature}]`・`/settings`）は **tab の妥当性を検証せず
  307 する**。検証は prefix 付きハンドラ 1 箇所に置く。副作用として
  `client.get("/api/machine-info")` が 307 → プロキシに落ちるため、移設した
  test_pages.py の mainsail テストが本文ほぼ無改変で通る。

### 計画外の判断ログ

- **machine 設定（`GET /api/settings/machine`）は必要なページだけ取る**
  （`_MACHINE_SETTINGS_FEATURES = {paste_solder, loading, copper_detection}` と
  settings ページ）。全ページで取ると、machine.toml が壊れただけで backend の
  `/api/settings/machine` が 500 → frontend が全画面 503 になり、直すための設定画面すら
  開けない。MR2 で backend に入れた「壊れていても描けるページは描く」防御を frontend でも
  保つ形。ROBUST_PAGE_URLS（15 URL）が 200、`_MACHINE_TOML_DEPENDENT_URLS`（4 URL）が
  503 の両方をテストで固定した。
- **`_machine_number()` は未設定・非数値を 0.0 にする。** テンプレートは
  `"%.3f"|format` / `| round | int` で埋めるため None を渡せない。frontend に
  machine.toml の既定値を持たせない（機体ごとの真実と 2 箇所でずれる）方針を取り、
  未設定は 0.0 表示 + 設定ページで埋めさせる。旧 pages.py は `state.machine()` の
  dataclass 既定値を描いていたのでここだけ振る舞いが変わる（該当キーが machine.toml に
  無い場合のみ）。
- **backend がジョブを申告していないジョブページは 503**（`_job_spec`）。
  frontend の `layout.TABS` は自前の知識なので、版ずれで backend からジョブが消えると
  `job_name` / `param_specs` が欠けたフォームを描いてしまう。空フォームの「実行」を
  押させるより 503 が安全。実 backend では `TestTabsCatalogConsistency` が両者の一致を
  ピンするため到達しないので、テストは `JobCatalog()`（空カタログ）を上流に挿して
  再現した（`_frontend_over(..., catalog=...)`）。
- `MachineRegistry` には `settings.machines` → `machines_file` の順で連結して渡す
  （重複 id のマージ規則は MR5 の担当なので先取りしない）。
- `__main__.py` は `uvicorn.Server` を素で使う（`_WebUIServer` は持ち込まない。
  frontend は `PreviewService` を持たない）。

### 所有権表外の付随変更

- `src/web/api/app.py`: pages ルータ登録の削除に伴い orphan になった
  `app.state.templates = Jinja2Templates(...)` / `Jinja2Templates` import /
  `_PACKAGE_DIR` / `from pathlib import Path` を削除（backend に templates/ は
  もう無い）。
- `src/web/api/dependencies.py`: 上記で唯一の利用者（pages.py）が消えた
  `get_templates()` と `Jinja2Templates` import を削除。
- `tests/web/api/routers/test_jobs.py`:
  `TestSaveParamDefaults::test_saves_persisted_values_for_next_form` の最後の 1 行
  （`client.get("/pasting/loading")` の SSR 検証）を `appstate.job_param_defaults` の
  検証に置換。backend はページを配信しなくなったため 404 になる。描画側は
  `tests/web/ui/test_pages.py::TestPastingJobPages::
  test_loading_page_renders_saved_loading_defaults` が `GET /api/jobs` 経由で見ている。

### test_pages.py 移設の本文変更（合格条件の報告）

61 件中 **本文を変えたのは 3 件**。他は fixture の差し替えのみ（`client` は
`frontend_client` のエイリアス fixture を定義して 55 件の本文を無改変で通した）。

1. `test_root_redirects_to_posctrl` — Location が `/posctrl` → `/m/{machine_id}/posctrl`。
   prefix が付かないと未 prefix の入口へ戻ってループするため、期待値を変えるしかない
   （契約書が想定していた 1 件）。
2. `test_file_browser_start_path_is_rendered` — backend Settings を差し替える
   `create_app(evolved)` を `_frontend_over(ui_settings, evolved)` に変更。MR4 でページを
   描くのは frontend なので、差し替えた backend を上流に挿さないとページが取れない。
3. `test_mainsail_link_resolves_from_machine_id_when_unset` — 同上（契約書が想定していた
   もう 1 件）。`/api/machine-info` の直叩きは 307 → プロキシ経由でそのまま通る。

これ以外の変更: import（`web.api.routers.pages.TABS` → `web.ui.layout.TABS`、
`Settings` → `ApiSettings` / `UiSettings` の別名化）、fixture 名
（`webui_settings` → `backend_settings` 4 箇所、`app` → `backend_app` 3 箇所）、
局所 fixture の追加（`client` / `config_dir` / `appstate` / `partial_nozzle_cap` /
`broken_machine_toml` — ui の conftest には無いため）、
`_MACHINE_TOML_DEPENDENT_URLS` のコメントを 500 → 503 に更新、docstring の MR4 追記。
**期待値を緩めた箇所は無い。**

### 追加したテスト（MR4 の新仕様）

`TestMachinePrefixedUrls`（未 prefix → 307 / 全リンクの prefix / `/m/x/api/state` と
`/m/x/artifacts/...` がプロキシに届く = 登録順の回帰 / ドロップダウンの遷移先 /
未知マシン 404）、`TestWithoutMachines`（machine.toml 無しで `create_app` 成功・案内・
`/settings` 200・セレクタ非表示）、`TestMultipleMachines`（ピッカー・遷移先の維持）、
`TestUnreachableBackendPage`（503 HTML + ドロップダウン + `Retry-After: 5`。上流は実
ソケット `127.0.0.1:1`）、`TestBackendContractMismatch`（空カタログ → 503）、
`TestBrokenMachineTomlPages::test_machine_toml_dependent_pages_return_503`。

### 検証結果（トラック C の所有ファイル + 付随変更）

- `uv run pre-commit run --files <所有ファイル>`: pass
- `uv run pyright <所有ファイル>`: 0 errors, 0 warnings
- `uv run pytest tests/web -m "not hardware" -q`: **880 passed**（ui 258 / api 622）。
  ランダム順で 3 回連続 pass
- `grep -rn '</content>' src tests`: 0 件
- `uv run python -c "from web.ui.app import create_app; create_app()"`: 成功（機体設定を
  読まない = `pcbasm.config` も `web.api.app` も import されない。ルート順は
  `/m/{machine_id}/api` → `/m/{machine_id}/artifacts` → `/static` → pages で、
  `/m/{machine_id}/settings` が `/m/{machine_id}/{tab}` より先）
- mutation 17 件を当てて対応テストが落ちることを確認（`<scratchpad>/m*.py`。
  実行後に backup から復元し `diff` で一致を確認済み）:
  登録順の反転 / 503 ハンドラ未登録 / `Retry-After` 削除 / 404 に未知 machine_id を渡す /
  `/static` マウント削除 / `base` prefix 削除 / 1 台のとき 307 しない / 0 台の案内文削除 /
  0 台で `machines[0]` を触る / `current_suffix` の固定 2 種 / `machines` を context から
  外す / machine 設定を取らない / 全ページで machine 設定を取る /
  `_machine_number` を 0 固定 / ジョブ未申告の 503 防御を外す。
  **1 件だけ mutation が生き残り作り直した**: `@app.exception_handler(BackendUnavailable)`
  を `RuntimeError` に変えても通る（`BackendUnavailable` は `RuntimeError` の派生なので
  等価な mutation だった）。`KeyError` に付け替えて落ちることを確認した。

### 申し送り（トラック D / orchestrator）

- `Makefile` の `ui` / `ui-dev` / `ui-fake` は `python -m web.ui`（`PCBASM_UI_*` env）で
  起動する。frontend は `--factory` でも動く（`create_app()` が env から読む）。
- e2e で frontend のページを取るときは `/m/{machine_id}/...` を使う。未 prefix でも
  1 台なら 307 で届くが、`follow_redirects=False` の検証では prefix 付きで書くこと。
- 0 台起動時の案内ページと複数台のピッカーは `message.html`
  （`data-testid="message-detail"`）。503 / 404 も同じテンプレート。

______________________________________________________________________

## トラック D（e2e + 運用ファイル）

### 計画外の判断ログ

- **`_serve(app) -> Iterator[int]` ではなく `start_app(app) -> RunningServer`** にした
  （`port` と `stop()` を持つ attrs frozen ハンドル）。「backend を落とすと frontend 経由の
  WS が閉じる」を検証するには **サーバーを途中で明示的に停止**する必要があり、
  contextmanager では起動順（backend の実ポートを知ってから frontend を作る）と
  停止順（backend だけ先に落とす）を同時に満たせない。fixture 側は
  `try: yield … finally: running.stop()` で同じ形になる。
- **`LiveUi.base_url` に `/m/{machine_id}` を含めた**（`origin` を別途公開）。
  既存 e2e の約 20 箇所は `f"{live_ui.base_url}/pasting/paste_solder"` の形で書き換わり、
  httpx（`follow_redirects=False` が既定）でもそのまま 200 が取れる。未 prefix の 307 や
  `/static` を触るテストだけ `origin` / `ws_origin` を使う。
- **`e2e_settings` に `hostname=E2E_MACHINE_ID`（"e2etest"）を注入**し、backend の
  自己申告 machine_id と frontend の URL prefix を一致させた（`socket.gethostname()`
  依存だと環境ごとに URL が変わり、`live_ui_two` の 2 台も区別できない）。
  config / data / pcb root は `tmp_path/api` 配下に移した（`live_ui_two` が
  `tmp_path/alpha` `tmp_path/bravo` を使うため、隔離ディレクトリを引数化した
  `make_api_settings(root, *, hostname)` に切り出した）。
- `browser_page` は **`browser_pages` ファクトリの上に再実装**した（1 枚返すだけ）。
  context の作成/破棄を 1 箇所に集約し、`pytest_collection_modifyitems` の判定は
  `_BROWSER_FIXTURES = {"browser_page", "browser_pages"}` の積で見る。
- `ui-fake` は `machines.toml` を recipe 内で生成する（`Settings.machines` は env で
  渡せない = マシン一覧は machines_file と mDNS が真実、という設計に従う）。
  生成先は `PCBASM_UI_FAKE_DIR`（既定 `/tmp/pcbasm-ui-fake`）。
- `api-dev` の `--port 8080` を **8081** に直した（`api` は Settings 既定を使うので
  自動追随するが、`api-dev` はハードコードしていた）。

### 所有権表外の付随変更

- `tests/web/api/test_settings.py`: `test_defaults_without_env` の
  `assert settings.port == 8080` を **8081** に更新した。`src/web/api/settings.py` の
  `port` 既定変更（トラック D 所有）の直接の帰結で、他トラックの担当でもない。
  期待値を緩めてはいない（8080 = UI という理由をコメントに残した）。
- `tests/e2e/test_paste_solder_browser.py` の
  `test_saved_override_file_can_be_imported`: **ページ内 `fetch` の宛先を
  `live_server.base_url` → `live_ui.base_url`** に変えた。ページが frontend
  オリジンで開くため backend 直は cross-origin になり、CORS を持たない backend では
  必ず `TypeError: Failed to fetch` になる（実測で落ちた）。ブラウザからの API 呼び出しは
  同一オリジン = プロキシ経由でしかできない、という MR4 の前提そのもの。

### 既存 e2e の付け替え（約 20 箇所）

- `tests/e2e/test_webui_e2e.py` → **`tests/e2e/test_api_e2e.py`（`git mv`）**。
  SSR ページ取得 6 箇所を `live_ui` に移し、API 直叩きは `live_server` のまま。
- `test_browser_ui.py`: `goto` 12 箇所とヘルパ `_start_completion_notice_job` を
  `live_ui` へ。`live_server` を使わなくなった 7 テストは引数から外した。
- `test_paste_solder_browser.py`: `_open_paste_solder(page, live_ui)` と
  ルート `goto` 1 箇所。pad-config の PATCH / route / fill-path は backend 直のまま。
- 結果として **ブラウザ E2E 31 件が全部プロキシ経由**になった（`empty-base`
  mutation で `TestPasteSolderBrowserRendering` / `TestSettingsOverBrowser` の 7 件が
  落ちることを実測 = 素通しになっていない証跡）。

### mutation 検証（影コピー + PYTHONPATH。リポジトリの `src/` は一度も書き換えていない）

`<scratchpad>/mutate_trackD.py` + `<scratchpad>/shadow-src`（`web/` のみ複製。
`pcbasm` は実リポジトリから解決される）。最後に `diff -r` でリポジトリと一致を確認済み。

| mutation | 落ちたテスト |
| --- | --- |
| `stream_response` を `CancelScope(shield=True)` で包む（クライアント切断のキャンセルを遮断） | `TestPreviewStreamReleasesCamera` 2 件（`preview_clients` が 1 / 2 のまま戻らない） |
| クライアント → 上流の WS 中継を止める | `TestJobOverProxiedWebSocket` 2 件 |
| 上流 status を無視して 200 固定 | `test_bytes_and_range_are_transparent`（206 → 200） |
| 未知 machine_id で `accept()` してしまう | `test_unknown_machine_id_is_rejected_before_accept` |
| 上流 WS の close をクライアントへ伝えない | `test_client_websocket_closes_when_backend_stops`（30s の自前締め切りで TimeoutError） |
| 未 prefix リダイレクトを 302 に | `test_single_machine_redirects_with_307` |
| `machines` を context から外す | `test_picker_lists_both_machines_with_prefixed_options` |
| `base` を空文字に | `test_each_machine_page_is_bound_to_its_own_prefix` + ブラウザ 7 件 |

- **生き残った mutation 1 件（重要な発見。orchestrator への報告事項）**:
  `_UpstreamResponse._close_upstream` を no-op にしても、さらに上流 `httpx.Response` を
  モジュール変数で握って GC も起きないようにしても、`preview_clients` は 0 に戻る。
  クライアント切断のキャンセルが `aiter_raw()` の yield 点へ throw され、httpcore の
  `PoolByteStream.__aiter__` が `except BaseException: await self.aclose()` で
  コネクションを閉じるため。**つまり明示 `aclose()` は多重防御であって、
  ブラックボックスからは観測できない**（キャンセル経路自体を潰す上記 shield mutation は
  ちゃんと検出できるので、e2e の検出力そのものは確認済み）。
  トラック A のファイルなので手は入れていない。
- テスト自身の欠陥を 1 件修正した: `next(response.iter_bytes())` で**イテレータを
  捨てる**と、GC の `GeneratorExit` が httpx の接続解放まで伝わって「まだ読むつもりの
  ストリーム」が切れる（2 クライアントの計数が 2 → 0 になり偽陰性）。
  `_started_stream()` でイテレータを返して呼び出し側が保持する形にした。
  トラック A の申し送り（同一クライアントから 2 本張らない）とは**別の罠**。

### 検証結果（全体）

- `make format`: pass
- `make type`: 0 errors, 0 warnings
- `make test-no-hardware`: **1881 passed**（MR3 は 1738。内訳は下記）
- `make test-e2e`: **62 passed**（うち browser 31。Playwright は導入済みで skip なし）
- `grep -rn '</content>' src tests`: 0 件
- 実機テスト（`make test` / `@mark_hardware`）は未実行

件数の内訳（1738 → 1881 = +143）:

- `tests/web/api` 737 → 618（−119）= `routers/test_pages.py` 119 件が
  `tests/web/ui/test_pages.py` へ移動（`test_app.py` の 1 件は 1:1 置換で件数不変）
- `tests/web/ui` 0 → 262（+262）= 移設 119 + 新規 143
  （test_pages +18 / test_layout 31 / test_proxy 36 / test_machines 23 /
  test_settings 18 / test_machine_client 15 / test_proxy_unreachable 2）
- e2e は `-m "not e2e"` で除外されるため件数に影響しない（62 = 既存 50 + 新規 11 + 1。
  新規 `test_proxy_e2e.py` 11 件、`test_api_e2e.py` は移設で件数不変）

### 申し送り（orchestrator）

- `CLAUDE.md:101`（`make api` の説明が「port 8080」）と `README.md:51`（「port 8080」）は
  backend 8081 / frontend 8080 にずれた。ドキュメント追随は計画書で **MR7** の担当なので
  触っていない。MR4 で直すなら 2 行。
- `uv add websockets` を実行済み（`pyproject.toml` + `uv.lock`。`>=16.0`）。
  インストール済みのものを宣言しただけなので挙動は変わらない。
- `webui` / `webui-dev` / `webui-fake` エイリアスは MR7 まで残置（実機の
  `pcbasm-webui.service` が `ExecStart=make webui` を参照）。

______________________________________________________________________

## 修正ラウンド（2 視点レビューの確定指摘 11 件。orchestrator 裁定後）

### M1: 未設定 machine.toml キーの `0.0` フォールバックを撤去した

**backend が実効値を返す形に変えた。選択したのは `/api/settings/machine` の
`SettingsField` に `resolved` を足す案**（`/api/machine-info` への追加・
`/api/settings/resolved` 新設は不採用）。理由:

- 実効値を要るページ（paste_solder / loading / copper_detection）は**すでにこの
  エンドポイントを取っている**ので、往復も新しいプロキシ経路も増えない
- 「ホワイトリストのキー → 値」を 1 箇所が持つので、生値（`value`）と実効値
  （`resolved`）が別 API に分かれて片方だけ増える事故が起きない。対象キーの
  ハードコード表を 2 本目として作らずに済む（`MACHINE_FIELDS` に自動追随する）
- 設定フォームは従来どおり `value`（未記載なら空欄 + placeholder「未設定」）を使い、
  現在値の表示だけが `resolved` を読む

実装:

- `SettingsField.resolved: MachineSettingValue | None` を**必須フィールド**で追加
  （既定 None にすると詰め忘れが静かに通る）。`machine_settings_fields(store, state)` が
  `AppState.machine()` から解決する。`GET /api/settings/machine` は `StateDep` を取る
- 解決は `_resolved_value(machine, key)` がドット区切りキーを `Machine` から辿る。
  `Path` → str、`tuple` → `list[float]`。**キー単位で broad except → None**
  （`[probe]` の必須キー欠落で `paste_dispenser` の実効値まで失わせない）。
  machine.toml が読めないときは全件 None（`AppState` の他 getter と同じ防御）
- frontend は `_MachineSettings`（`endpoint` + `fields`）に置き換え、`number(key)` が
  `resolved` を読む。**解決できない値は 0 で代替せず `BackendUnavailable` → 503**。
  0 を描くと、その 0 がスライダー/フォームの値として「設定に保存」で machine.toml へ
  書き戻される（旧実装は同じ状況で 500 だったので、劣化はしていない）
- 呼び出し元は 7 キー（copper_detection の canny_low / canny_high / blur_ksize、
  loading の solder_paste_density / rotations_per_ul / max_dispense_rate /
  dispense_accel）。`_machine_number` の呼び出し元はこれで全部
- 回帰テスト: `machine_toml_without_defaulted_keys` fixture が
  canny_low / canny_high / blur_ksize / solder_paste_density を machine.toml から削除し、
  ページが 100 / 200 / 5 / 3.780 を描き 0 を描かないことを固定。backend 側は
  `resolved` の既定値解決とセクション単位の独立性を `test_settings_api.py` で固定

### M2 / M3: 検査用の最小 ASGI 上流を e2e に導入した

`ui_over_inspection_upstream` fixture（実 uvicorn 2 サーバー）。上流は starlette の
`WebSocketRoute("/api/ws")`（受信ヘッダと query を JSON で返し 4001 + reason で閉じる）と
`Route("/api/slow")`（1s 待って 200）。backend WebAPI を上流にすると「cookie を落としたか /
x-forwarded-for を足したか / close code を中継したか」は応答に現れず素通りする。

- M2: cookie 不到達 / x-forwarded-for 付与 / query 到達 / 上流 close code + reason の
  中継を 4 テストで固定
- M3: `ssr_timeout=0.2` << 上流の遅延 1.0s << `proxy_read_timeout=10.0` で 200 が返る。
  待ちは `_before_deadline`（daemon スレッド + `join(15s)` → `pytest.fail`）で囲む

### M4: `Settings.default_backend_port` を配線した

`load_machines_file(path, *, default_port=DEFAULT_BACKEND_PORT)` にして `create_app` が
`settings.default_backend_port` を渡す。env → 実際の接続先までを
`TestDefaultBackendPort`（`PCBASM_UI_DEFAULT_BACKEND_PORT=19999` → 503 ページの本文に
`http://127.0.0.1:19999` が出る）で固定。module 定数の直参照に戻すと落ちる。

### S1〜S4 / nit

- S1: カメラリーク回帰の主張を「切断のキャンセルが上流ストリームへ伝播すること」に
  直した（`test_proxy_e2e.py` の module docstring とクラス docstring、
  `proxy.py` の `_UpstreamResponse` docstring）。**コードは変更していない**
- S2: backend の `SECTION_LABELS` / `section_of` を削除（唯一の参照は
  「backend コピーと一致」テスト 2 件だった）。`common.py` の docstring から
  `SettingsField` の再 export の記述も外した（`_fields` が使うので import は残る）。
  `machine_settings_fields` は残す。test_layout は既存の
  `test_every_machine_field_section_has_a_label` に寄せて 2 件削除
- S3: `/m/{machine_id}` 単体を **`/{tab}` より前**に登録して既定タブへ 307。
  当初 `/{tab}/{feature}` の後ろに置いたが、`/{tab}` の登録が先にあるため
  「より具体的なものを前」に統一した。`test_paste_solder_browser.py` の
  オリジン確保 `goto` は `/posctrl` に変更
- S4: `CLAUDE.md` の `make api` 行を port 8081、`README.md` は
  「backend WebAPI は port 8081（UI frontend は 8080）」に更新
- nit: test_pages.py の docstring を「本文 3 件 + 503 期待値の新規固定 1 件」に、
  「2 プロセス」表現を「実 uvicorn（実ソケット経由）」等へ統一（5 箇所）、
  `browser_pages` の docstring を「`browser_page` の実装を集約するファクトリ」に

### mutation 検証（M1〜M4。すべてリポジトリを復元済み）

| mutation | 落ちたテスト |
| --- | --- |
| `_MachineSettings.number` を `field.value` + 0.0 フォールバックに戻す | `TestUnsetMachineSettingsShowResolvedValues` 2 件 |
| `cookie` を落とさない / `x-forwarded-for` を足さない | `test_cookie_is_not_forwarded_to_the_backend` / `test_client_address_is_forwarded` |
| WS close を `close(1000, "")` 固定 / `query_string` を落とす | `test_upstream_close_code_and_reason_reach_the_client` / `test_query_string_reaches_the_upstream` |
| `_relay_timeout` が gateway 既定（`ssr_timeout`）を返す | `test_slow_upstream_response_is_relayed_instead_of_timing_out`（504） |
| `_endpoint_from_entry` が module 定数 `DEFAULT_BACKEND_PORT` を直参照 | `test_env_default_port_applies_to_entries_without_a_port` |

### 検証結果

- `make format` / `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: **1886 passed**（修正前 1881。+7 新規 − 2 削除）
- `make test-e2e`: **67 passed**（修正前 62。+5 = WS 4 + read timeout 1）
- `grep -rn '</content>' src tests`: 0 件。`git status --short` に使い捨てプローブ無し
- 実機テスト（`make test` / `@mark_hardware`）は未実行

______________________________________________________________________

## 検出力の穴の追補ラウンド（独立確認で生き残った mutation 2 件）

修正ラウンド後の独立確認で「テストが 1 件も落ちない mutation」が 2 件見つかったので、
そこだけをテストで塞いだ。**`src/` は変更していない**（実装は正しく、守る仕組みが
無かっただけ）。

### G1: `_MachineSettings.number()` の 503 分岐（`raise` → `return 0.0` が 265 件素通り）

- 素材: `[paste_dispenser]` の**必須キー（既定値なし）** `nozzle_diameter` を 1 行だけ
  落とした machine.toml（fixture `machine_toml_without_required_paste_dispenser_key`）。
  TOML としては読めるので backend の `/api/settings/machine` は 200 を返し、cattrs が
  `PasteDispenser` を組めないため `paste_dispenser.*` の `resolved` が全て None になる。
  既存の `machine_toml_without_defaulted_keys`（既定値**を持つ**キーを落とす）は
  attrs の既定値で解決されてしまうので、この分岐には届かなかった
- テスト `TestUnresolvableMachineSettingsAreNotFabricated` の 2 件:
  - `/m/{id}/api/settings/machine` が **200** + `canny_low.resolved is None`
    （+ 生値 `rotations_per_ul == 45.783133` は健在 / `probe.min_samples` の実効値も健在
    = セクション単位の独立性）。「backend が落ちているのではなく実効値が無いから 503」を
    上流の応答側から示すため
  - `/posctrl/copper_detection` が **503** で本文に `canny-low-value` を含まない
- mutation `raise BackendUnavailable` → `return 0.0`: `test_pages.py` 143 件中
  **新規の 1 件だけが落ちた**（`test_copper_detection_returns_503_instead_of_zero`、
  `assert 200 == 503`）。他 142 件は緑 = この分岐に検出力が無かったことの裏取り

### G2: `/preview/stream` だけ read 無制限（`_relay_timeout` → `return base` が MJPEG 3 件素通り）

- 既存の MJPEG 3 件はストリーム読み取りが `ssr_timeout=2.0` より短いため、read を
  gateway 既定へ戻す mutation を検出できていなかった（M3 の `/api/slow` 1 件だけが落ちる
  = 「read が短すぎない」ことしか守れていない）
- 検査用上流（`_inspection_upstream`）に `/api/preview/stream` を追加した。フレーム間を
  `_UPSTREAM_DELAY`（1.0s）空けて流し続ける終端しない multipart で、**実カメラは無関係**
- fixture を 2 本に分けた（起動処理は contextmanager `_ui_over_inspection_upstream` に
  括り出し、`proxy_read_timeout` だけを引数化）:
  `ui_over_inspection_upstream`（10.0s。既存 M2/M3 用）と
  `ui_with_short_read_timeout`（**0.5s**。G2 用）。
  `ssr_timeout 0.2 < proxy_read_timeout 0.5 < フレーム間隔 1.0` に置いたので、
  read を無制限にしない経路はどちらの値でも打ち切られる
- テスト `TestPreviewStreamIsExemptFromReadTimeout` の 2 件:
  - `/api/preview/stream` が 0.5s を超える間隔でも **2 フレーム目まで届く**
    （`--frame` を 2 個数えるまで読む。チャンク境界の分割・結合に依存しない形）
  - 対照: 同じ frontend の `/api/slow`（1.0s）は **504**（例外がこの 1 経路に限られる）
- mutation `_relay_timeout` → `return base`: preview テストが **0.24s で失敗**
  （`httpx.RemoteProtocolError: peer closed connection without sending complete message
  body`）。**ハングしない**ことを確認済み。同 mutation で既存の M3（`/api/slow` 200 期待）
  も落ちる
- 待ちは `_before_deadline`（daemon スレッド + `join(15s)`）で囲む。`pytest.fail` を
  スレッド内で投げても `_before_deadline` が主スレッドへ送り直す

### G3: `CLAUDE.md` の make ターゲット一覧

`make ui` / `ui-dev` / `ui-fake` を追記した（`api` 系の書き方に合わせ `ui` と `ui-dev` を
1 行に束ねたので **2 行**）。あわせて直後のエイリアス行の「上記へのエイリアス」を
「`api` 系へのエイリアス」に直した（間に ui の行が入って指示語が曖昧になるため。
2 プロセス構成の説明は MR7 の担当なので書いていない）。

### 検証結果

- `make format` / `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: **1888 passed**（前回 1886。+2）
- `make test-e2e`: **69 passed**（前回 67。+2）
- mutation 適用中に書き換えた `src/web/ui/pages.py` / `src/web/ui/proxy.py` は
  `cp` バックアップから復元済み（proxy は md5 一致を確認。どちらも未追跡ファイルなので
  `git diff` では確認できないため、復元後に該当テストの再実行で確認した）
- `grep -rn '</content>' src tests`: 0 件。`git status --short` の `??` は MR4 の新規
  ファイルのみ（使い捨てファイル無し）
- 実機テスト（`make test` / `@mark_hardware`）は未実行

### 追補ラウンドの再検証（別セッションからの独立確認）

上記 G1〜G3 はセッションを跨いだため、成果物の状態と検出力を新しい context から
やり直して確認した。**`src/` / `tests/` への追加変更は無い**（すでに正しい状態だった）。

- `src/web/ui/pages.py` の `number()` は `raise BackendUnavailable`、
  `proxy.py` の `_relay_timeout` は `read=None if route_path in
  _UNBOUNDED_STREAM_PATHS else self._read_timeout` のまま = 前回の mutation は復元済み
- `make format` / `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: **1888 passed**（105 deselected）
- `make test-e2e`: **69 passed**（1924 deselected）
- mutation 再測定（`cp` バックアップ → 適用 → 復元 → `md5sum -c` で一致確認 →
  該当スイート再実行で 143 / 18 passed）:
  - `number()` の `raise BackendUnavailable` → `return 0.0`:
    `tests/web/ui/test_pages.py` 143 件中 **1 件だけが落ちた**
    （`TestUnresolvableMachineSettingsAreNotFabricated::
    test_copper_detection_returns_503_instead_of_zero`、`assert 200 == 503`）。
    他 142 件は緑 = この分岐を守っているのは新規テストだけ、という前回の測定を再現
  - `_relay_timeout` → `return base`: `TestPreviewStreamIsExemptFromReadTimeout::
    test_stream_survives_gaps_longer_than_the_read_timeout` が **0.23s で失敗**
    （`httpx.RemoteProtocolError`）。**ハングしない**ことを再確認。同 mutation で
    M3 の `TestProxyReadTimeout` も落ちる。対照の
    `test_other_paths_are_still_cut_off_by_the_read_timeout` は mutation 下でも緑
    （`ssr_timeout` 0.2s でも `/api/slow` は 504 になるため。この 1 件は
    「例外が preview だけ」を示す対照であって `_relay_timeout` の kill 役ではない）
