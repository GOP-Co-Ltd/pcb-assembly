# MR6 トラック B（frontend: 操作権リースの UI とセッション同定）

契約: /tmp/pcbasm-plan/mr6-brief.md（§2 の frontend 側 / §5 / §9 の所有ファイル / §10 のワイヤ契約）。
backend（`src/web/api/**`）と `tests/e2e/` は一切触っていない。

## 実装した配線（要点）

- **cookie 発行は `pages.py` の `_html_page()` 1 箇所**。`render_message` / `settings_page` /
  `tab_page` / `feature_page` の 4 つの `TemplateResponse` をこの関数に通し、
  「HTML ページ応答だけがセッションを発行する」を構造として保証した。
  `SESSION_COOKIE`（= `pcbasm_session`）は **`web.ui.proxy` に定義**して pages が import する
  （cookie → backend ヘッダの翻訳規則を持つのが proxy 側なので、名前もそこに置いた）。
  `Path` は `set_cookie` の既定（`/`）のまま**渡していない**。未発行のときだけ発行する
  （毎回発行すると遷移ごとに別人になり操作権が自分から離れる）。
- **ヘッダ注入は `proxy._forward_headers()` 1 箇所**。シグネチャを
  `(headers, client_host, drop)` → `(connection: HTTPConnection, drop)` に変えた。
  `Request` と `WebSocket` が同じ基底クラスなので、http と WS ハンドシェイクの両方が
  同じ 1 関数を通り、`connection.cookies` から `X-Pcbasm-Session` /
  `X-Pcbasm-Client-Name` を組める（cookie を落とす処理の前に読む形になった）。
- `_DROP_REQUEST_HEADERS` に **`x-pcbasm-session` / `x-pcbasm-client-name` を追加**した。
  クライアントが自称したヘッダを素通しさせず、必ず cookie から組み直す（組み立て点を 1 つに保つ）。
- **`control.js`（新規）** が `body.dataset.control` を書き、`[data-requires-control]` に
  `toggleAttribute("inert", state !== "held")` を当てる。初期値 `INITIAL_STATE = "viewer"`、
  `held` 以外（`unknown` 含む）は全部塞ぐ = fail-closed。
- `app.js` は `err.status` / `err.data` を付け、423 のときだけ
  `window.webui.control?.onDenied(data)` を呼んでから throw する（既存 20 箇所は無改変）。
- `job_console.js` に `case "control_changed"`（→ `control.applyControl`）と、
  WS `open` 時の `control.refresh()` を足した。
- `settings.html` の scripts に `job_console.js` を追加（`/settings` に WS が無かった）。

## 計画外の判断ログ

1. **操作権バー（`partials/control_lease.html`）を新設した。** 契約 §5 は
   `data-requires-control` の対象しか列挙していないが、`POST /api/control/{acquire,release,
   takeover}` を叩く UI が無いと閲覧者が操作権を取れず機能が死ぬ。§4 の
   「操作権が空いています。**取得して**応答してください」も取得手段の存在を前提にしている。
   バー自身はゲートしない（閲覧者の唯一の入口）。
2. **表示名の入力欄をバーに置いた。** §10 が `pcbasm_name` を「JS から読み書きする表示名」と
   定義しているため、書き手が要る。`change` で cookie を書き、**保持者のときだけ**
   `POST /api/control/name` を投げる（閲覧者の名乗りは取得時にヘッダで届くので送らない）。
   → トラック A への確認事項（下記 IF）。
3. **表示名 cookie は `encodeURIComponent` で書き、proxy は `quote(unquote(value))` で
   正規化する。** cookie 値に生の日本語を入れるとヘッダ（latin-1）で壊れる。JS が既に
   quote 済みの値を書くので、素通しだと二重エンコードの危険があり、`unquote` を 1 回
   挟んで冪等にした（curl で生 ASCII 名を付けた場合も同じ経路で quote される）。
4. **プロンプトの案内文はダイアログ内の `#jc-prompt-hint` に置き、control.js が常に更新する。**
   要素がダイアログ内にあるので「プロンプトが保留中か」を control.js が知る必要がない
   （job_console.js との双方向依存を作らずに §4 の「holder が null なら取得を促す」を満たす）。
   本文（`#jc-prompt-message`）はゲートしない = 閲覧者にも見える。
5. **§5 のリストに無い操作もゲートした**: `#nc-record`（nozzle-cap/record）、
   `#ks-gcode-form`（machine-control の任意 G-code）、`.rps-actions`（WS command）、
   `#canny-save`（machine 設定の PUT）。いずれも backend 側でゲートされる変更系なので、
   開けておくと閲覧者が押して 423 トーストになるだけ。§5 の列挙は代表例と解釈した。
   逆に開放したもの: レイヤ切替・順路/塗布パス計算・`#pad-export-config`・pad ビューア・
   質量キャリブの計算入力（純計算）・`#mc-toggle` / `#mc-resize`（表示操作）・
   canny スライダー（プレビューのみ）・`#estop`・`#jc-abort`。
6. **`app.css` は触っていない**（所有ファイル外）。`[inert]` にブラウザ既定の視覚変化は
   無いので、現状は閲覧者に「押せない理由」が見えない。CSS 要求は報告の
   `shared_infra_requests` に出した。
7. **`app.py` は変更なし**（cookie は pages、スクリプト読み込みは base.html で足りた）。

## 他 implementer への IF 変更通知（トラック A 向け）

- **WS の error は `{"type": "error", "detail": "…"}`（`message` ではない）。**
  契約 §10 は `message` と書いているが、既存の `job_console.js` は `event.detail` を読み、
  既存の backend（`routers/jobs.py:346,351`）も `detail` で publish している。
  §3 の「クライアント改修ゼロで toast が出る」を満たすのは `detail` 側なので、
  **`detail` のまま**にした（`message` で送るとトーストが `undefined` になり、
  しかもテストが無い経路なので気付けない）。A 側も `detail` で揃えること。
- **`POST /api/control/name` を body 無しで呼ぶ。** 表示名は `X-Pcbasm-Client-Name`
  ヘッダから取る前提（§2 の解決順）。A が `{"name": ...}` の body を要求する実装なら
  frontend 側を 1 行変える必要があるので知らせてほしい。
- **プロキシ経由では自称ヘッダは通らない。** `/m/{id}/api/**` に
  `X-Pcbasm-Session` を直接付けても落として cookie から組み直す。frontend を通す
  テスト（e2e / トラック C）は **cookie で**セッションを分けること
  （`tests/web/api/` の `TestClient` は backend 直叩きなのでヘッダのままでよい）。
- `GET /api/state` は `control` を `held: false` の `LeaseInfo` ダンプで返す契約に依存
  （`null` だと frontend は `unknown` 表示になる）。`you.key` が無いと保持者判定ができず
  永久に `viewer`（fail-closed）になる。

## 既知の制約・残課題

- **WS ハンドシェイクのヘッダ注入は in-process では検証できない**（`ASGITransport` は
  websocket scope を扱えない）。実装は http と同じ `_forward_headers` を通るので、
  実 uvicorn の e2e（トラック C）で「WS の接続元が cookie のセッションとして数えられる」
  ことを押さえてほしい。
- **JS の実行を伴う検証は無い**（このリポジトリに JS テストランナーが無い）。
  `control.js` の状態切替は「ソースの契約点をピン + テンプレートの印の集合をピン +
  レンダリング済み HTML の属性」で押さえた。`body[data-control]` の遷移と実 `inert` の
  効果、`is_enabled()` が inert を見ないことへの対処は e2e（トラック C）の担当。
- `pcbasm_name` は frontend だけで持つ（cookie）。ブラウザを変えると名前は引き継がれない。

## 検証結果

自分の範囲だけを対象にした（ツリー全体のコマンドは並列作業中なので実行していない）。

- `uv run pre-commit run ruff / ruff-format / docformatter --files <owned files>`: pass（2 周して安定）
- `uv run pyright src/web/ui tests/web/ui`: 0 errors
- `uv run pytest tests/web/ui -m "not hardware"`: 359 passed（新規 59 件）
- 実ブラウザでの手元確認（コミットしない使い捨てスクリプト、`/usr/bin/chromium` +
  playwright）: `app.js` + `control.js` を読ませて JS エラー 0、状態遷移が
  `unknown`（state 取得失敗）→ `viewer`（他人が保持）→ `free`（423 で holder null）→
  `held`（`you.key` 一致）と動き、`#machine-control` の `inert` が held でだけ外れ、
  `#estop` には一度も付かないこと、表示名入力が
  `pcbasm_name=%E7%94%B0%E4%B8%AD` を書くことを確認した。
  この確認で「cookie 読み出しで例外が出ると inert が一切適用されない」順序バグを
  見つけたので、`render()` をイベント配線より前に移した。
- mutation（影コピー `/tmp/mr6b-mut` + `PYTHONPATH`）: 11 件すべて検出。
  `control.js` の初期値 → `"held"` / proxy のヘッダ注入削除 / cookie の `Path` を
  `/m/{id}` に狭める / `#estop` にゲートを付ける / `control_changed` case 削除 /
  settings の `job_console.js` 削除 / app.js の 423 フック削除 / `#machine-control` の
  ゲート外し / fail-closed 判定の緩和 / 再接続時 refresh 削除 / 空きリース案内文の削除。
  影コピーと `src` の差分は `__pycache__` のみ（壊した状態は残していない）。
