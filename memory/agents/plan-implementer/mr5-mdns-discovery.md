# MR5 — mDNS 広告 + 探索でマシン一覧を自動構成

実装契約: `/tmp/pcbasm-plan/mr5-brief.md`（計画書 `web-api-ui-split.md` 224-250 行の MR5 節が正典、
食い違いは契約書優先）。ブランチ `feat/20260730/mdns-discovery`。

## 循環 import の回避（契約 §3）

**採用: `resolve_machine_id` は `web/api/settings.py` に置き、`settings.py` が
`discovery.py` から `SERVICE_TYPE` を import する（契約の第一候補どおり）。** 循環しない:

- `web/api/discovery.py` → `web.api.models`（`API_VERSION`）のみ。`settings.py` を import しない
  （`ServiceAdvertiser` は `Settings` を受け取らず、必要な値を個別の引数で受ける）
- `web/api/settings.py` → `web.api.discovery`（定数のみ）
- `routers/common.py` → `web.api.settings`（`Settings` を既に import していたので import 追加なし）
- `web/ui/settings.py` / `web/ui/discovery.py` → `web.api.discovery`（共有ワイヤ定数）

副作用として `web.api.settings` の import が zeroconf を引き込む。宣言済み依存なので許容した。

`common.py:149` の `settings.hostname or socket.gethostname()` を `resolve_machine_id` 呼び出しに
置換し、不要になった `import socket` を落とした（自分の変更で生じた orphan）。

## 契約からの逸脱・追加した判断

1. **`build_service_info` を public な純関数として追加**（契約の API 一覧には無い）。
   `server=` の回帰と TXT ラウンドトリップをソケットなしで固定するため。private メソッドを
   直接テストしない方針との両立でこの形にした。`ServiceAdvertiser` は内部でこれを使う。
2. **`advertise_addresses` の注入値には `select_advertise_addresses` を掛けない**
   （`app.py:_build_advertiser`）。フィルタは実 IF 列挙（`local_ipv4_addresses()`）にだけ適用する。
   掛けると `advertise_addresses=("127.0.0.1",)` が空になり、「テストをループバックに閉じる」という
   この注入口の目的（契約 T3）が成立しない。計画書 232 行「ifaddr 列挙 → 除外」と整合。
3. **`update` は `async_update_service`** で TXT を差し替える（instance 名は登録時のものを使い回す）。
   `allow_name_change=True` で改名された広告を、新しい instance 名で二重登録しないため
   `build_service_info(instance=...)` を持たせた。
4. **`/api/machines` は `src/web/ui/machines_api.py` の別ルータ**にし、`app.py` で
   `include_router(pages.router)` より**前**に入れた（契約が許した 2 案のうち後者）。pages.py に足すと
   キャッチオールとの前後関係がファイル内の定義順に依存して読み取りにくい。
   登録順の回帰は `tests/web/ui/test_machines_api.py::TestListMachines::test_returns_json_not_a_tab_page`。
5. **`current` はクエリ `?current=<machine_id>` を受けてサーバが bool を返す**（契約の 2 案のうち
   「サーバが判定する形」）。JS は `select` の `data-machine-id` / `data-current-suffix` を読むだけ
   （`machine_selector.html` に 2 属性を追加）。
6. **`machine_selector.js` は `fetch` を直接呼ばず `window.webui.frontendJson()` を通す**。
   `tests/web/ui/test_layout.py::TestMachinePrefixFunnel::test_fetch_is_confined_to_the_funnel_and_the_completion_sound`
   が「`fetch(` は app.js と job_console.js だけ」を静的に固定しているため。既存テストを書き換えず
   invariant を保つ側を選び、`app.js` に prefix を付けない GET ヘルパ `frontendJson` を追加した
   （`/api/machines` は frontend 自身のエンドポイントなので machine prefix を付けてはいけない）。
7. **`PCBASM_API_HOSTNAME` は追加していない**。契約 D7 の文言に出てくるが、`hostname` は現状
   コンストラクタ注入のみで E2E も足りており、使われない env を増やさない方針を採った。
   D7 の要件（ID の導出を 1 箇所に）は `resolve_machine_id` で満たしている。
8. D1〜D8 のうち D1（`addresses` を足さない）/ D2 / D3 / D4 / D5 / D6 / D7 / D8 は契約どおり。
   D8 の「0 台の案内ページに自動リロードを入れない」も契約どおり入れていない。

## 実 LAN 汚染の防止（契約 T3）

- `tests/helpers.py` に `skip_if_no_mdns`（5353 の `SO_REUSEADDR` 共有 bind +
  `224.0.0.251` の join をプローブ、結果はモジュールレベルでキャッシュ）。既存 decorator の
  ファクトリ名を `_skip_if_camera_unavailable` → `_skip_unless_available` に改名して共用した
  （カメラ専用の名前で mDNS を通すと嘘になるため。呼び出し元は helpers.py 内の 2 箇所だけ）。
- `discovery_enabled=False` を渡した fixture: `tests/web/api/conftest.py` の `webui_settings` /
  `real_settings`、`tests/web/ui/conftest.py` の `backend_settings` / `ui_settings`、
  `tests/e2e/conftest.py` の `make_api_settings` / `make_ui_settings`、
  `tests/web/ui/test_pages.py` の `_frontend_only` ヘルパ（fixture 相当。テスト本文は無改変）。
- `tests/conftest.py` に autouse fixture `disable_mdns_by_default` を追加し、
  `PCBASM_API_DISCOVERY_ENABLED=0` / `PCBASM_UI_DISCOVERY_ENABLED=0` を全テストへ注入した。
  `Settings.from_env()` を実アプリに通すテスト（`tests/web/api/test_settings.py:129`,
  `tests/web/ui/test_pages.py` の `create_frontend_app()`）は fixture を経由しないので、
  これが無いと env 経路だけ広告・探索が生きる。
  **注意: `tests/web/api/test_settings.py` / `tests/web/ui/test_settings.py` の `ENV_VARS`
  （`clean_env` が delenv する一覧）に discovery の env を足してはいけない。** 足すと
  `clean_env` を使う「実アプリを from_env で起動する」テストで広告が復活する。
- 見張りテスト（fixture が False を返すことの assert）:
  `tests/web/api/test_discovery.py::TestDiscoveryIsolation`、
  `tests/web/ui/test_discovery.py::TestDiscoveryIsolation`、
  `tests/e2e/test_discovery_e2e.py::TestDiscoveryIsolation`。
- 実 zeroconf を使うのは `tests/e2e/test_discovery_e2e.py` だけ。サービス型は毎回
  `_pcbasmt<hex>._tcp.local.`、`interfaces=("127.0.0.1",)`、広告アドレスも `127.0.0.1`。
  assert は「期待した machine_id が現れる」のみで総件数を見ない。

## E2E の実行形（ハマりどころ）

`make test-e2e` は同一セッションで playwright の sync API を使い、**main thread の event loop を
回したままにする**。そのため main thread では `asyncio.run` も anyio マーカーも
「another loop is running」で失敗する（実測: 単体では通り、フルスイートで 3 件が RuntimeError）。
`test_discovery_e2e.py` はテスト本体を同期関数にし、async シナリオを `run()` が**専用スレッドの
新しい loop** で実行して例外を送り返す形にした。`skip_if_no_mdns`（同期ラッパー）を素直に被せられる
副産物もある。

## mutation 検出の実測（契約 §9.2、影コピー + PYTHONPATH）

| mutation | 検出したテスト |
| --- | --- |
| (a) `server=` を省略 | `tests/web/api/test_discovery.py::TestBuildServiceInfo::test_server_is_a_dedicated_name_not_the_avahi_hostname`（+ `test_instance_override_keeps_the_registered_name`） |
| (b) `169.254.` 除外を消す | `tests/web/api/test_discovery.py::TestSelectAdvertiseAddresses::test_excludes_link_local`（+ `test_all_excluded_returns_empty`） |
| (c) `api` 版不一致のスキップを消す | `tests/web/ui/test_discovery.py::TestEndpointFromServiceInfo::test_mismatched_api_version_is_dropped` |
| (d) マージを mDNS 優先へ反転 | `tests/web/ui/test_machines.py::TestSetDiscovered::test_static_host_port_and_name_win_for_the_same_machine_id`（+ `test_missing_name_and_machine_type_are_filled_from_mdns`） |
| (e) `/api/machines` を pages より後に登録 | `tests/web/ui/test_machines_api.py::TestListMachines::test_returns_json_not_a_tab_page`（同クラス 5 件） |

影コピーは scratchpad 配下に取り（`/tmp/mut-src` は権限で拒否された）、検証後に削除。
リポジトリには壊した状態を残していない（`git status` は意図した変更のみ）。

## 既知の制約・残課題

- **生存判定を入れていない**（契約どおり）。電源断の機体は最大 75 分一覧に残り、選ぶと 503 ページ。
- `MachineDiscovery` の `Removed` は候補集合の掃除のみ。`_resolve` が解決できなかった広告は
  WARNING を出して捨てる（次の `Updated` で再試行される）。
- `PUT /api/settings/machine` の広告更新は「`state.machine_name()` の前後比較」で判定するため、
  machine.toml が壊れて `machine_name()` が None を返す状態では更新が飛ばない（表示名が古いまま）。
  設定を直せば次の変更で追いつく。
- 実機確認は未実施（Claude は実機テストを実行しない）。計画書 250 行の
  `avahi-browse -rt _pcbasm._tcp` / `journalctl -u avahi-daemon | grep -iE "conflict|withdraw"` /
  `ping kurousagi.local` はユーザー側で確認が必要。

## 検証結果

- `make format`: pass（2 回目で no-op）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（1938 passed / 112 deselected）
- `make test-e2e`: pass（76 passed / 1974 deselected）

---

# MR5 レビュー指摘対応（採用 8 件 / 却下 3 件）

orchestrator 裁定にもとづく追加実装。上の初回実装ノートは残置（この節が差分）。

## 採用分の対応

### M1 — `make api-fake` / `ui-fake` が実機の machine_id を実 LAN に広告する

`Makefile` の `api-fake` に `PCBASM_API_DISCOVERY_ENABLED=0`、`ui-fake` に
`PCBASM_UI_DISCOVERY_ENABLED=0` を他の env と同じ行で追加した（`webui-fake` は
`api-fake` への依存エイリアスなので同じレシピを通る = 追加不要）。理由をレシピ直上に
コメントで残した（fake backend は camera / data_dir だけが fake で config_dir は実機のもの。
同じ machine_id で広告すると frontend の `_merge` が「先に発見した方」を残すため、
ドロップダウンの実機エントリが port 8099 の fake backend を指しうる）。

### M2 — 長い `machine_name` が backend を永久に起動不能にする

2 段構えで実装（`src/web/api/discovery.py`）:

1. `_clamp_txt_value(key, value)` を追加し、TXT に載せる `name` を **1 エントリ 255 bytes**
   （`key=value` の長さ前置が 1 バイトなので実質 `255 - len(key) - 1` = `name` では 250 bytes）
   に UTF-8 の文字境界で切る（`encode` → スライス → `decode(errors="ignore")`）。
   切ったときは INFO ログを 1 行出す。広告そのものは生かす（表示名だけが切れる）。
2. `start()` / `_republish()` の **両方で `_build_info()` を try の内側へ移し**、except に
   `ValueError` を追加した（`BadTypeInNameException` は `zeroconf.Error` のサブクラスだが、
   try の外にあったため漏れていた）。失敗しても WARNING のみで落とさない。

クランプ対象は自由入力の `name` だけ。`machine_type` など machine.toml 由来の他の値が
長すぎる場合は (2) が受けて「広告なしで続行」へ縮退する（アプリは落ちない）。

回帰テスト:

- `tests/web/api/test_discovery.py::TestLongDisplayName` — 300 bytes の名前で
  `build_service_info` が例外を出さず、TXT の `name` が 250 bytes 以下かつ有効な UTF-8
  （元の文字列の prefix）であること。
- 同 `::TestAdvertiserWithUnbuildableServiceInfo` — machine_id 70 文字（`BadTypeInNameException`）
  と TXT 値 300 bytes（`ValueError`）で `start()` が例外を投げず WARNING を出すこと。
  サービス型はランダム化し `interfaces=("203.0.113.9",)`。
- `tests/e2e/test_discovery_e2e.py::TestAdvertiseAndDiscover::test_update_with_a_very_long_name_still_publishes`
  — `_republish` 経路（`update(長い名前)`）が Task 例外を残さず、クランプ後の名前が
  探索側へ届くこと（期待値は公開 API `build_service_info` から導出し、クランプ規則を
  テストに複製しない）。

### M3 — env キルスイッチの回帰テスト

- `tests/web/api/test_settings.py::TestDiscoveryKillSwitch` /
  `tests/web/ui/test_settings.py::TestDiscoveryKillSwitch` — env 未設定 → `True`、
  `"0"` → `False`、`"0"` 以外（`"1"`）→ `True`。
  **`ENV_VARS`（`clean_env` の delenv 一覧）には足していない。** 足すと autouse fixture の
  `0` が消えて広告・探索が復活するため、その理由を `ENV_VARS` 直上のコメントに明記した
  （env を外すのは `TestDiscoveryKillSwitch` の 1 テストの中だけ）。
- より強い見張りとして、`from_env` 経路で実アプリを組むテストを両側に追加:
  `tests/web/api/test_discovery.py::TestDiscoveryIsolation::test_app_built_from_env_has_no_advertiser`
  と `tests/web/ui/test_discovery.py::TestDiscoveryIsolation::test_app_built_from_env_has_no_discovery`
  （`app.state.advertiser is None` / `app.state.discovery is None`）。

### S1 — `ServiceStateChange.Removed` の分岐が未検証（→ 実装バグを 1 件発見）

`tests/e2e/test_discovery_e2e.py::TestAdvertiseAndDiscover::test_unregistered_machine_disappears_from_the_list`
を追加（browsing を外側に、advertising を内側にして goodbye 後の消滅を `wait_for` で待つ。
assert は「期待した machine_id が消える」で、総件数は見ない）。

**この新テストで本番バグが露出したので `ServiceAdvertiser` を直した**（テストが実装の
バグを指摘しているケース）:

- `stop()`: `async_unregister_service` は **goodbye の送信タスクを返すだけ**で、await せずに
  `async_close()` するとタスクごとキャンセルされ goodbye が 1 パケットも出ていなかった。
  実測（`AsyncZeroconf` 2 本のプローブ）: await 無し → 探索側は `Removed` を受け取らない
  ／ await あり → 即座に消える。正常終了した機体が全 frontend のドロップダウンに
  最大 75 分残る挙動だったので `await (await zeroconf.async_unregister_service(info))` に修正。
- `start()` / `_republish()`: 同じ理由で announce / update の送信タスクも await するようにした。
  await しないと、飛び残った announce（3 パケット・約 0.45s）が直後の goodbye を**追い越して
  探索側で復活**する（テストが flaky になるだけでなく、実運用でも「止めた直後の機体が残る」）。
  代償は lifespan 起動が約 0.45s 伸びること。装置操作に影響しないので受容した。

### S2 — JS 経路（D8）が未検証

`tests/e2e/test_browser_ui.py::TestMachineSelectorRefresh` を 1 本追加（既存 `browser_page`
fixture を使用）。ローカル fixture `selector_ui` が `live_server` と到達不能な `ghost` の
2 台を静的登録した実 frontend を起こし、**registry も返す**（`set_discovered` で
「mDNS で表示名が届いた」状態をテストから作る = 実 zeroconf を触らない）。検証内容:

- ページロード直後に `/api/machines?current=<machine_id>` を **実際に fetch している**
  （`expect_request`。初回 `refresh()` を消すとここで落ちる）
- option のテキストがサーバの `label` と一致する（期待値は `GET /api/machines` の応答から取る。
  JS でラベルを組んでいたら不一致になる）
- `current` のマシンが選択される（登録順 2 番目にしてあるので、`selected` を落とすと
  先頭の ghost が選ばれて落ちる）
- `set_discovered` → `visibilitychange` で option がサーバの新しい label に組み替わる

### N1 — `SERVICE_TYPE` の値がピン

`tests/web/api/test_discovery.py::TestServiceTypeConstant` でリテラルを 1 行ピン
（実機確認手順 `avahi-browse -rt _pcbasm._tcp` と README が値に依存する）。
`tests/web/ui/test_discovery.py` のローカル再宣言は削除し、`web.api.discovery` から import
する形に直した。

### N2 — 「`machine_name` が変わったときだけ update」をピン

`tests/web/api/routers/test_settings_api.py::TestAdvertisementUpdate` を追加。
`RecordingAdvertiser`（実 `ServiceAdvertiser` を継承し `update` の呼び出しだけ記録。
ソケットは開かない）を `app.state.advertiser` に差して 3 ケース: 名前変更で 1 回呼ぶ /
他フィールドの保存では呼ばない / 同じ名前の再保存では呼ばない。
本物のサブクラスにしたのは、広告の再登録を観測する手段が `update` 呼び出しそのものしか
なく（結果はマルチキャストの先）、シグネチャは型で縛ったままにしたかったため。

### N3 — 実 zeroconf を触るテストのサービス型をランダム化

`random_service_type()` を `tests/helpers.py` へ移し（`skip_if_no_mdns` と同じ場所に集約）、
`tests/web/api/test_discovery.py::TestAdvertiserWithoutMulticast` と
`tests/web/ui/test_discovery.py::TestDiscoveryWithoutMulticast` の fixture に注入した。
`tests/e2e/test_discovery_e2e.py` のローカル定義は削除して helpers から import。
bind は ENODEV で必ず失敗するが、「万一 bind できたとき」に運用サービス型で漏らす
構造的リスクを消した。

## 却下 3 件（実装せず、受容したリスクとして記録）

1. **タスク参照保持（`set` + `add_done_callback`）のテスト** — GC タイミング依存の invariant で
   決定的なテストが書けない。実装は正しい（`ServiceAdvertiser._tasks` /
   `MachineDiscovery._tasks` に強参照を持ち done で discard）。**未検証のまま受容**する。
   壊すと「広告更新が飛ばない / 探索結果が不定に欠ける」形で表面化する。
2. **`SERVICE_TYPE` をワイヤ定数専用モジュール（`web/api/wire.py`）へ切り出す案** — 定数 1 つの
   ためにモジュールを増やす方が悪い。zeroconf は宣言済み依存で両プロセスが使う。現状維持
   （`web.ui` → `web.api.discovery` の import を継続）。
3. **machine.toml が壊れて `machine_name()` が None のとき update が飛ばない縮退** — 受容。
   壊れた machine.toml では既に 4 ページが 500 する既知の状態で、mDNS の表示名の古さは
   その中で最も軽い。設定を直せば次の変更で追いつく。

## mutation 検出の実測（影コピー `/tmp/fix-mut-src` + `PYTHONPATH`）

| mutation | 検出したテスト |
| --- | --- |
| `_build_info()` を try の外へ戻す | `TestAdvertiserWithUnbuildableServiceInfo`（2 件: machine_id 70 文字 / TXT 300 bytes） |
| `name` のクランプを外す | `TestLongDisplayName`（3 件）+ e2e `test_update_with_a_very_long_name_still_publishes` |
| except から `ValueError` を外す | `TestAdvertiserWithUnbuildableServiceInfo::test_too_long_txt_value_warns_instead_of_raising` |
| `!= "0"` を `True` 固定（api / ui 両側） | `TestDiscoveryKillSwitch::test_zero_disables_*`（両側）+ `TestDiscoveryIsolation::test_app_built_from_env_has_no_*`（両側） |
| `Removed` 分岐を削除 | e2e `test_unregistered_machine_disappears_from_the_list` |
| goodbye の await を外す（S1 の修正の逆） | 同上 |
| `machine_selector.js` の初回 `refresh()` を削除 | e2e `TestMachineSelectorRefresh`（`expect_request` が timeout） |
| `option.selected = machine.current` を削除 | 同上（`to_have_value` が不一致） |
| ラベルを JS で組む（`machine.label` を使わない） | 同上（`to_have_text` が不一致） |
| `SERVICE_TYPE` の値を変更 | `TestServiceTypeConstant::test_service_type_is_the_documented_literal` |
| 変化判定を無条件（`if advertiser is not None:`）に変更 | `TestAdvertisementUpdate`（2 件） |

影コピーは検証後に `src` の内容へ戻した（`diff -r -x __pycache__ src /tmp/fix-mut-src` が空。
`rm` は権限で拒否されたため上書きで復元）。リポジトリに壊した状態は残っていない。

## 検証結果（対応後）

- `make format`: pass（2 回実行）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass
- `make test-e2e`: pass
- 実機確認は未実施。M1 の効果（`make api-fake` が広告しない）と goodbye の実 LAN 挙動
  （`avahi-browse -rt _pcbasm._tcp` で停止後に消えること）はユーザー確認が必要。

---

# MR5 最終仕上げ（F1 / F2 / F3 / F4）

2 巡目レビューの指摘への対応。上の 2 節は残置（この節が差分）。

## F1 — 見張りテスト自身が実 LAN への漏れ口だった（should-fix）

`tests/web/api/test_discovery.py::TestDiscoveryIsolation::test_app_built_from_env_has_no_advertiser`
と `tests/web/ui/test_discovery.py::TestDiscoveryIsolation::test_app_built_from_env_has_no_discovery`
は assert を `with TestClient(app):` の**内側**に書いていた。lifespan が走るため、
**このテストが検出すべき回帰（env キルスイッチの破損）が起きたときに限り、実機の
machine_id を運用サービス型 `_pcbasm._tcp.local.` で全 IF に publish してから落ちる**。

`app.state.advertiser` / `app.state.discovery` は `create_app` が設定し、`_lifespan` は
start / stop するだけなので、**assert を `create_app` 直後（lifespan の外）へ出した**。
理由は両テストの docstring に 1 行残した（「起動するとこの見張りが守っている性質そのものを
破る」）。`tests/web/ui/test_discovery.py` では `TestClient` の import が orphan になったので
落とした（`tests/web/api` 側は他テストが使うので残置）。

検証（影コピー + tripwire、下表）: キルスイッチを `True` 固定すると 2 件が落ち、
`ServiceAdvertiser.start` / `MachineDiscovery.start` に仕込んだ tripwire は**発火しない**。
tripwire 自体が生きていることは、旧形（`TestClient` で lifespan を起動）を再現する
スクリプトで確認した（`LAN LEAK: ADVERTISER '_pcbasm._tcp.local.' ifaces=None` /
`LAN LEAK: DISCOVERY ...` の 2 行が出る = 旧形は本当に漏らしていた）。

## F2 — `start()` / `_republish()` の announce 待ちを巻き戻した（nit / 開発原則 3）

前段 S1 の修正で入れた「announce 送信タスクの await」（`await (await
async_register_service(...))` の形）を**元の実装に戻した**（登録自体の await は残し、
送信タスクは await しない）。S1 が要求したのは `stop()` の goodbye が飛ぶことだけで、
announce 待ちは範囲外・守るテストなし・flakiness も再現しない（このブランチで
goodbye テスト単独 5 連続 = 5/5 pass、`make test-e2e` も 79 件 pass）。lifespan の
余分なブロック（実測約 0.5s）だけが残っていた。

**`stop()` の goodbye の await は残す**（`test_unregistered_machine_disappears_from_the_list`
が守っている。外すと 20s の `wait_for` が timeout する = 実運用では正常終了した機体が
全 frontend のドロップダウンに最大 75 分残る）。規則は `stop()` の該当行に 1 行コメントで
残した:「送信完了を待つのは goodbye だけ。消えないことが最大 75 分の実害になる唯一の
経路であり、announce は待たなくても実害が無い（次のクエリに応答できる）」。

**受容した残存リスク**: shutdown が announce の飛び残り（3 パケット・約 0.45s）と競合すると、
停止直後の機体が探索側に一時的に残りうる（飛び残った announce が goodbye を追い越す）。
到達不能マシンを一覧に残すのは計画書の要件どおりの挙動（生存判定を入れない）で、
そのマシンを選ぶと 503 ページになる。次のクエリでも応答は返らないので、実害は
「一時的に選べる項目が残る」ことに限られる。

## F3 — クランプの INFO ログを caplog でピン

`_clamp_txt_value` の切り詰めログを消しても全スイートがグリーンだった。
`tests/web/api/test_discovery.py::TestLongDisplayName` に 2 本追加:

- `test_truncation_is_logged` — 300 bytes の名前で `web.api.discovery` の
  「切り詰め」レコードが出る
- `test_a_name_that_fits_is_not_logged` — 収まる名前では出ない（無条件ログ化を検出する）

判定は `caplog.records` を logger 名 + メッセージで絞る（`caplog.text` 全文一致にすると
他のログで通ってしまう）。

## F4 — `make api-fake` / `ui-fake` のキルスイッチに回帰テスト

`tests/test_makefile_fake_targets.py` を新設。Makefile を読んで 2 つの fake レシピ
（タブ始まりの行）を抜き、`PCBASM_API_DISCOVERY_ENABLED=0` /
`PCBASM_UI_DISCOVERY_ENABLED=0` が含まれることを parametrize で assert する。
置き場所は前例の `tests/test_claude_hooks.py::TestHardwareMarkerLayout`
（フック自身の前提をピンする）に倣ってトップレベル（`src/` に対応物が無い成果物）。
`webui-fake` は `api-fake` への依存エイリアスなので同じレシピを通る = 対象外。

## 対応しない（受容した gap）

`_republish()` の `_build_info()` を try の外へ戻す mutation は無検出のまま受容する。
この経路は `start()` 成功後にしか通らず、`name` はクランプ済み、`machine_id` と
`machine_type` は `start()` が同じ値で成功している。失敗しうるのは
`allow_name_change=True` による改名（`alpha` → `alpha-2`）で instance 名が 63 bytes を
超える corner case だけで、そのときも WARNING が出て広告は前の状態のまま残る（無害）。

## mutation 検出の実測（影コピー `<scratchpad>/co-mut-src` + `PYTHONPATH`）

| mutation | 検出したテスト / 観測 |
| --- | --- |
| F1: env キルスイッチを `True` 固定（api / ui 両側） | `TestDiscoveryIsolation::test_app_built_from_env_has_no_advertiser` / `...has_no_discovery` の 2 件が fail。**tripwire は無出力**（広告・探索を一切開始しない） |
| （tripwire の有効性確認）旧形どおり `TestClient` で lifespan を起動 | tripwire が 2 行発火（`LAN LEAK: ADVERTISER '_pcbasm._tcp.local.' ifaces=None` / `DISCOVERY ...`） |
| F3: クランプの `logger.info` を削除 | `TestLongDisplayName::test_truncation_is_logged` |
| F3 逆方向: 切り詰め不要でもログを出す | `TestLongDisplayName::test_a_name_that_fits_is_not_logged` |
| F4: Makefile から 2 つの env 行を削除 | `TestFakeTargetsDisableDiscovery::test_recipe_sets_the_kill_switch`（2 件） |
| F2: `stop()` の goodbye の await を外す | e2e `test_unregistered_machine_disappears_from_the_list`（20s の `wait_for` が timeout） |

影コピーは `rm -rf` が権限で拒否されるため、pristine な `src` から上書きして復元し
`diff -r -x __pycache__` が空であることを毎回確認した。F4 は Makefile が影コピーの
対象外なので実ファイルを一時的に壊し、退避したコピーから復元して `diff` で同一性を
確認した（復元後にテスト 2 件が pass することも確認済み）。

## 検証結果（最終）

- `make format`: pass（2 回実行。2 回目は no-op）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（1960 passed / 115 deselected）
- `make test-e2e`: pass（79 passed / 1996 deselected）
- `grep -rn '</content>' src tests`: 0 件
- コミットはしていない。実機確認（`avahi-browse -rt _pcbasm._tcp` での announce / goodbye、
  `make api-fake` が広告しないこと）はユーザー側で必要。
