# MR6 統合（トラック A + B の合流）

トラック A（backend）と B（frontend）が同一ツリーで並列に実装したものを、初めて
ツリー全体で検証した記録。A / B の設計判断は残し、**合流してはじめて壊れるもの**だけを
直した。

## A / B の境界検証（契約 §10）

キー名は全て一致していた（直した箇所は無い）。実物で突き合わせた対応:

| 契約 §10 | A（backend） | B（frontend） |
| --- | --- | --- |
| `X-Pcbasm-Session` / `X-Pcbasm-Client-Name` | `identity.SESSION_HEADER` / `NAME_HEADER` | `proxy._SESSION_HEADER` / `_NAME_HEADER` |
| `pcbasm_session` / `pcbasm_name` | `identity.SESSION_COOKIE` / `NAME_COOKIE` | `proxy.SESSION_COOKIE` / `NAME_COOKIE`（`pages` が発行、`control.js` が name を書く） |
| `/api/state` の `control` / `you` | `models.ControlInfo` / `ClientInfo`（既定値付き） | `control.js applySnapshot` |
| 423 body の `holder` | `app.py control_denied_handler` | `control.js onDenied` |
| `control_changed` の payload | `control_api.control_changed_event` | `job_console.js` → `control.applyControl` |
| `body.dataset.control` | — | `"held" / "viewer" / "free" / "unknown"`、初期値 `viewer` |

ヘッダ / cookie の実経路は `tests/e2e/test_multi_user_browser.py` が通しで踏む
（ブラウザ 2 context → proxy → backend で 423 と `control_changed` を観測する）。

### 契約 §10 と実装が食い違ったまま残した 1 点（要裁定）

WS の認可拒否イベントの key は **`detail`**（契約 §10 は `message`）。A / B 双方が
独立に `detail` を選び、双方が理由を報告している:

- 既存の error 経路（`routers/jobs.py` の他の error publish）が `detail`
- `job_console.js` の `case "error"` が `event.detail` を読む
- 契約 §3 が「既存経路でクライアント改修ゼロで toast が出る」と要求している

A と B は互いに一致しているのでワイヤは繋がっている。`message` へ寄せると既存の
error イベントと 2 系統になり §3 を破るため、**`detail` を正として残した**。契約側の
記述を直すか否かは orchestrator の裁定。

## 合流して初めて壊れたもの（直した）

### 1. 既存ブラウザ E2E 22 件が落ちていた（fail-closed の副作用）

`data-requires-control` に `inert` が付くのは `state !== "held"` のときなので、
**誰も操作権を取っていない状態（`free`）でも塞がる**。既存のブラウザテストは操作権を
取らないため、変更系の click / fill が全て届かなくなっていた。

- `tests/e2e/conftest.py` に `acquire_control(page)` を追加し、既存の 13 箇所
  （`test_browser_ui.py` の各 goto 直後と `test_paste_solder_browser.py` の
  `_open_paste_solder`）から呼ぶようにした。
- `acquire_control` は `free` なら取得、`viewer` なら**奪取**する。テストの下準備が
  backend を直叩きしており（`select_led_blinker` は `PUT /api/pcb-file` = ゲート付き）、
  セッションヘッダが無いため `anonymous` がリースを握るため。
- 押すボタンを決める前に `control.refresh()` を挟む。SSR の初期値も `viewer` で
  「サーバがそう言っている」のと区別できないため（ここを省くとボタンの表示が
  切り替わる瞬間に click が競合する）。
- 逆向きも起きる: ブラウザが保持者になると、テストが backend を直叩きする
  ジョブ開始が 423 になる。`session_headers(page)` を追加し、browser context の
  `pcbasm_session` cookie を `X-Pcbasm-Session` に載せて同一クライアントとして通す
  （3 箇所）。

### 2. `hidden` の付いたボタンがレイアウトに残り、隣の要素のクリックを奪っていた

`app.css` の `button { display: inline-flex }` が UA の `[hidden] { display: none }` に
勝つため、操作権バーの 3 ボタンが同時に配置され、`.control-lease` の枠から溢れて
`#machine-select` / `#pcb-chip` の上に重なっていた（Playwright が
"intercepts pointer events" で click 不能）。`button[hidden] { display: none }` を追加。

**副作用（意図的）**: 同じ理由で `#jc-prompt-no`（`noButton.hidden = !(isConfirm ||
cancelable)`）も従来は隠れていなかった。この規則で正しく隠れるようになる。MR6 以前からの
表示バグで、既存テストは「表示されるべきケース」だけを見ていたので検知できていなかった。

### 3. プロンプトの `showModal()` が閲覧者を詰ませていた（安全性）

`::backdrop` が `#estop` と `#jc-abort` を覆うため、応答権を持たない閲覧者は
「応答もできず・中止もできず・緊急停止もできない」状態になっていた。契約 §4 の
「abort は誰でも可なので deadlock にならない」が実際には成立していなかった
（保持者も応答待ちの間は緊急停止を押せない）。

`dialog.show()`（非モーダル）へ変更し、中央寄せと重なり順を `.jc-prompt[open]` の
CSS に持たせた。非モーダルは自動でフォーカスが移らないので、Enter の暗黙送信を保つため
`#jc-prompt-input`（無ければ `#jc-prompt-ok`）へ明示的に `focus()` する
（`test_enter_key_submits_ok_instead_of_cancel` が固定している挙動）。

### 4. ヘッダが 1280px で溢れていた

操作権バーの分だけ `.global-controls` の余白が無くなり、`.control-holder` が 0px まで
潰れて保持者が読めなかった。CSS で 1 行に収め、`min-width` で保持者表示を守り、
ボタンのラベルを「操作権を取得」→「取得」等へ短くした（`title` に元の文言が残る。
ラベル文字列はどのテストも固定していない）。

## 共有基盤（両トラックの shared_infra_requests）

- `JobManager.publish(event)` を public 化し（旧 `_publish` を改名）、`control_api` の
  `ControlEventHub`（45 行）と `routers/jobs.py` の `hub.register` / `unregister` を削除。
  購読キューの二重管理が無くなり、`app.state.control_events` も不要になった
  （`create_app` 以外で app を組む場合に必要な state は `app.state.control` だけ）。
- `tests/helpers.py` に `before_deadline(call, *, what, deadline)` を追加し、
  `tests/e2e/test_proxy_e2e.py` と `tests/web/api/routers/test_control_api.py` の
  重複していた同形ヘルパを置き換えた。
- `app.css` に `[inert]`（`opacity` + `cursor`）、`.control-lease` 一式、
  `.jc-prompt-hint` を追加（B の依頼）。

## 新設テスト

`tests/e2e/test_multi_user_browser.py`（6 件、`browser_pages` の 2 context）。
アサートは (a) `body[data-control]`、(b) 対象の `inert` 属性、(c) サーバ側効果の 3 点のみ。
`is_enabled()` は `inert` を見ないので使っていない。

- 保持者と閲覧者が**再読込なしで**分岐する（WS `control_changed`）
- サーバ応答前は塞ぐ（`GET /api/state` を route で宙吊りにして固定）
- 閲覧者の実マウス click は HTTP を発生させず、迂回しても backend が 423
- **閲覧者から緊急停止が効く**（`_klipper_action` は Klipper 送信より先に abort を
  立てるので、Klipper 不通でも実行中ジョブが aborted になる = 観測可能）
- **閲覧者から WS abort が効く**
- 閲覧者が奪取でき、元の保持者が再読込なしで閲覧者へ落ちる

## mutation（契約 §7）

影コピー（`PYTHONPATH=<copy>`）で全て検出を確認し、壊した状態は残していない。

| | mutation | 検出 |
| --- | --- | --- |
| a | firmware-restart の `ControlDep` を外す | `test_viewer_is_denied_with_423[post-/api/firmware-restart]`, `test_takeover_wins_against_the_holder` |
| b | WS の except から `ControlDeniedError` を外す | `test_viewer_respond_prompt_yields_error_without_closing`（WS 切断） |
| c | emergency-stop に `ControlDep` を付ける | `test_emergency_stop_works_for_a_viewer`, `test_viewer_is_never_locked_out[post-/api/emergency-stop]` |
| d | `claim` を `Depends` から `klipper_errors_to_502` の内側へ移す | `test_denied_machine_control_is_423_not_502`（423 → 502） |
| e | `control.js` の初期値を `held` に | `test_gated_controls_are_inert_before_the_server_answers`（新設 e2e）, `test_control_js_starts_as_viewer` |

## 既知の制約・残課題

- **操作権が空いている（`free`）だけでも UI は塞がる。**「1 人で使うのに毎回『取得』を
  押す」運用になる。契約 §4 の「操作権が空いています。取得して応答してください」が
  この設計を前提にしているのでそのままにしたが、実機で使いにくければ「`free` は
  塞がない」に変える余地がある（`control.js` の `blocked` 1 行）。
- **操作者がタブを開いたまま 10 分放置（ジョブ非実行）すると操作権が空く**（契約 §1 の
  裁定）。MR 説明と実機確認チェックリストへの転記が必要。
- 非モーダル化でプロンプト表示中に背面の UI を押せるようになった（保持者のみ。
  装置排他ロックがあるのでジョブの二重起動は起きない）。実機で違和感が無いか確認したい。
- コミットはしていない（orchestrator の担当）。新規ファイルは `git add -N` 済み。

## 検証結果

- `make format`（`uv run pre-commit run -a`）: pass（2 回連続で差分なし）
- `make type`（`uv run pyright`）: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（2105 passed, 120 deselected）
- `make test-e2e`（`-m "e2e and not hardware"`）: pass（84 passed。合流前は 22 failed）
- `grep -rn '</content>' src tests`: 0 件
- 実機テスト（`make test` / `make run`）は実行していない

______________________________________________________________________

# レビュー指摘対応（orchestrator 裁定: 採用 7 / 却下 2 / 承認 1）

`code-reviewer` の指摘に対する orchestrator の裁定を受けての追加実装。**採用 7 件はすべて
「実装は正しいがテストが無い / 弱い」型の指摘**で、6 件はテスト追加、3 件は実装も直した
（N1 / N2 / N3）。既存の設計判断（上記）は変えていない。

## M1（must-fix）WS `command` の操作権ゲートが無検証

`routers/jobs.py` の `case "command":` の `lease.claim(identity)` を消しても全件緑だった
（`respond_prompt` 側だけがテストされていた）。この 1 行が唯一のサーバ側防御で、退行すると
閲覧者が実行中ジョブへ Record/Quit や machine コマンドを送れる（frontend の `inert` は
UI だけの防御で、WS へ直接送れば迂回できる）。

`respond_prompt` のテストと同形で 2 件追加（`TestWebSocketControl`）:

- `test_viewer_command_yields_error_without_closing` — 非保持者の `command` が
  `{"type": "error"}` を返し**接続が切れない**（続けて別メッセージを送れる）。コマンドが
  ジョブへ届いていないことを status で確認する
- `test_holder_command_reaches_the_running_job` — 保持者の `command` は通ってジョブが完了する

`accepts_commands=True` の合成ジョブ（`ctx.next_command(timeout=None)` で待つだけ。装置は
触らない）を `_register_commanded` で登録している。

## S1 423 → `onDenied` が無検証 / S2 ロード時 `refresh()` が無検証

既存の 2 件はソース文字列のピンで、`onDenied` を fail-open（`state = "held"`）に壊しても
検出できなかった。既存 e2e の 423 検証は httpx で backend を直叩きしており、**ブラウザの
`api()` 経路を通らない**ため `onDenied` を踏んでいなかった。

`tests/e2e/test_multi_user_browser.py` に 2 件追加。どちらも
**WS を開かないブラウザ**（`_NO_WEBSOCKET_SCRIPT` を `add_init_script` で注入）を使う。
`job_console.js` が WS の `open` で `control.refresh()` を呼ぶため、これを黙らせないと
更新源 (a)（ロード時の `GET /api/state`）と (c)（423 の `onDenied`）を単独で観測できない
（WS 経由の取り直しが両方をマスクする）。

- `test_load_fetches_the_lease_state_without_the_websocket` — 空きリースが `free` として
  届くこと。SSR の初期値は `viewer` なので `free` が「ロード後にサーバへ問い合わせた」証跡に
  なる。下準備の直叩きが握った anonymous のリースは `_release_control` で返させる
- `test_denied_write_drops_the_page_to_viewer` — ページが `held` のまま奪われた状態
  （WS が黙っているので `control_changed` が届かない）で `window.webui.api()` を叩き、
  423 → `onDenied` で `data-control` が `viewer` へ落ち `inert` が戻ることを見る。
  fail-open 方向（`held` のまま）と「`onDenied` を呼ばない」の両方を検出する

S2 の単体テストは名前を `test_control_js_reads_the_state_endpoint` へ改め、
docstring に「宛先のピンであり、実挙動は上記 e2e が持つ」と書いた（旧名
`test_state_is_fetched_from_the_backend_on_load` は検証内容より強いことを主張していた）。

## S3 `busy=` の配線が無検証

`app.py` の `busy=lambda: state.busy_owner is not None` を `lambda: True` / `lambda: False`
どちらに固定しても全件緑だった。`test_control.py` は注入された fake busy しか見ておらず、
**装置排他ロックが `busy` として繋がっているか**は誰も見ていなかった。

`create_app(settings, *, clock=...)` に時計の注入口を足した（`web.ui.app` の
`transport_factory` / `Settings.hostname` と同じ「テスト用注入口」の前例に倣う。失効までの
既定は 600s で実時間では待てない）。`TestIdleExpiryIsGatedByTheMachineLock` で
fake clock を 601s 進め、

- `state.machine_lock(...)` を保持している間は失効しない（→ `lambda: False` を検出）
- ロックを離すと同じ無操作時間で失効する（→ `lambda: True` を検出）

WS を 1 本張って在線にしてある（張らないと切断猶予 30s 側で失効し、無操作失効の観測に
ならない）。

## N1 プロキシの `unquote` が backend の防御を無効化していた（実装も修正）

`proxy.py` の `quote(unquote(display_name))` が `unquote` の既定 `errors="replace"` で
壊れた cookie を U+FFFD へ「修復」し、**正当な percent-encoding として** backend へ渡して
いた。backend の「復元できない値は既定名へ落とす」防御（`identity._decode_name` の
`errors="strict"` / `isascii()`）が実質到達不能で、文字化け 1 文字が表示名として
`control_changed` で全クライアントへ配られていた（cookie `pcbasm_name=%E3%81` →
ヘッダ `%EF%BF%BD` → 表示名 `"�"`）。

`_reencoded_name()` に切り出して `errors="strict"`、失敗したら**ヘッダを付けない**
（backend のフォールバックに委ねる）。回帰テストは
`test_undecodable_display_name_cookie_is_not_forwarded`。

## N2 / N4 非モーダル化で失われた高さクランプ（実装も修正）

`showModal()` → `show()` で UA の `dialog:modal { max-height: calc(100% - 6px - 2em);
overflow: auto }` が効かなくなり、`.jc-prompt[open]` は `height: fit-content` しか
持っていなかった。長い本文で `#jc-prompt-ok` が画面外に出ると、`position: fixed` なので
ページスクロールでも届かず、ダイアログ自身もスクロールしない = **応答不能なプロンプト**に
なる（実 prompt 本文は最長 ~110 字なので実データでは到達しない）。`.jc-prompt[open]` へ
`max-height` と `overflow: auto` を追加。

同時に N4（`.jc-prompt[open]` の CSS 自体が無検証。潰しても e2e 全件緑だった）を塞ぐため、
`test_long_prompt_keeps_the_ok_button_reachable`（`TestPromptDialogOverBrowser`）を追加。
長い本文を入れて `show()` し、ダイアログ内スクロール後に

- ダイアログ自身が viewport に収まる（`max-height`）
- OK が viewport 内に入る（`overflow`）

を実測する。`max-height` だけ / `overflow` だけを消しても落ちることを確認した
（Chromium では `overflow: visible` だと `max-height` のクランプも効かず、
ダイアログ高さが 1222px になる = 2 行が組で意味を持つ）。

## N3 案内ページの死んだ「取得」ボタン（実装も修正）

`message.html`（ピッカー / 503 / 未登録 404）は `base.html` を継承するので操作権バーを
描くが、`{% block scripts %}` を持たないため WS が無く、`data-machine-base` が空なので
`control.js` の `refresh()` / 取得 POST がプロキシ外へ飛んで 404 になる（押すと必ず
404 トーストが出る死んだボタン）。`base.html` の include を `{% if base %}` で囲んだ
（マシンが決まっていないページに操作権の概念は無い）。回帰テストは
`test_machine_independent_page_has_no_control_bar`。

`control.js` 自体は案内ページでも読み込まれたままにしている（`refresh()` の 404 は
`catch` して `unknown` に落ちるだけでトーストを出さない。読み込みを分岐させると
「全ページで読む」配線テストと二重管理になる）。

## 却下 2 件（裁定を受容した記録）

- **保持者判定を `ControlLease.is_holder()` へ移す案** — 却下。`control.py` は凍結対象で、
  `info.key == identity.key` は公開キー 2 つの同値比較であってドメインルールの複製ではない。
  凍結を解いて再レビューするコストに見合わない。**実装しなかった。**
- **実装ノートを契約 §7.3 の `mr6-control-lease.md` へ統一する案** — 却下。トラック別
  （backend / frontend / integration / control-core）の 4 本の方が読みやすい。契約の指定名を
  実態に合わせる側に直す。**ファイル名は変えていない。**

## 承認 3 ファイル（巻き戻していない）

`src/web/api/jobs/manager.py`（`_publish` → public `publish`）/ `src/web/ui/static/app.css` /
`tests/helpers.py`。いずれも上記「共有基盤」「合流して初めて壊れたもの」の記録どおりの
経緯で、orchestrator が承認済み。

## mutation（レビュー対応分）

影コピー（`cp -r src /tmp/mut/src` + `PYTHONPATH=/tmp/mut/src`）で 8 通り当て、すべて
**新設テストが**落ちることを確認した。壊した状態は残していない（`diff -r` で影コピーと
リポジトリが一致することを確認済み）。

| mutation | 落ちたテスト |
| --- | --- |
| `case "command":` の `lease.claim` を削除 | `test_viewer_command_yields_error_without_closing`（error が届かず 15s 締切で fail） |
| `control.js` の `onDenied` を `state = "held"` に | `test_denied_write_drops_the_page_to_viewer` |
| `control.js` 末尾の `refresh();` を削除 | `test_load_fetches_the_lease_state_without_the_websocket` |
| `app.py` の `busy=` を `lambda: True` に | `test_lease_survives_idle_timeout_while_the_machine_is_locked`（ロック解放後も失効しない） |
| `app.py` の `busy=` を `lambda: False` に | 同（ロック保持中に失効する） |
| proxy の `unquote` を `errors="replace"` に | `test_undecodable_display_name_cookie_is_not_forwarded` |
| `.jc-prompt[open]` の `max-height` / `overflow` を削除（両方 / 片方ずつの 3 通り） | `test_long_prompt_keeps_the_ok_button_reachable` |
| 案内ページで操作権バーを描くように戻す | `test_machine_independent_page_has_no_control_bar` |

## 検証結果（レビュー対応後）

- `make format`: pass（2 回連続で差分なし）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（2110 passed, 124 deselected）
- `make test-e2e`: pass（88 passed, 2146 deselected。対応前は 85）
- `grep -rn '</content>' src tests`: 0 件
- 実機テスト（`make test` / `make run`）は実行していない。コミットもしていない
