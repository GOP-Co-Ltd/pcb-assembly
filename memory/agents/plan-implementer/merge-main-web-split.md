# merge origin/main into chore/20260730/web-service-units（2 プロセス分離 × 通知音）

`git merge origin/main`（merge-base `c8ec48c` / MERGE_HEAD `b317915`、main 側 26 commit）で
発生した 25 件の衝突の解決記録。commit は orchestrator が行う（この agent は `git add` まで）。

衝突の構図は 2 つだけ:

- **このブランチ**: 単一 `src/webui/` を `src/web/api/`（backend WebAPI・port 8081）と
  `src/web/ui/`（UI frontend・port 8080。テンプレートと static は全てこちら）に分離。
  env prefix を `PCBASM_WEBUI_*` → `PCBASM_API_*` に改名。MR6 で操作権リースを追加
- **main**: `src/webui/` に通知音（audio）機能を追加。`align_component_groups` →
  `align_regions` の改名、air pump の bool オプション削除、銅箔前処理の canny 設定追加

## 衝突ごとの解決

### A. 通知音機能の配置（rename 検出で自動配置されたもの）

| 衝突 | main の意図 | このブランチの意図 | 両立させ方 |
|---|---|---|---|
| `src/web/api/routers/audio.py` (UA) | `src/webui/routers/audio.py` を新設 | router は backend | **音は Pi の ALSA で鳴る = backend の仕事**。import を `webui.dependencies` → `web.api.dependencies` に直して backend に置いた |
| `src/web/ui/static/js/audio.js` (UA) | 通知音ページの JS を新設 | static は frontend | frontend の配信資産としてそのまま採用（`api()` funnel を使うので機体 prefix はそのまま効く） |
| `src/web/ui/templates/dev/audio.html` (UA) | 通知音ページを新設 | テンプレートは frontend | frontend に配置。MR6 のゲート印を追加（下記 D） |
| `tests/web/api/routers/test_audio.py` (UA) | router の仕様テスト | テストは `tests/web/api/` | import を `web.api.*` に直し、docstring に「2 プロセス分離後も router は backend」「MR6 のゲート」を追記 |
| `src/web/ui/static/audio/*.wav` (UD) | `src/pcbasm/hal/sounds/` へ移動したので移動元を削除 | frontend の static に置いていた | **削除**（main の移動が正。`src/pcbasm/hal/sounds/{success,failure}.wav` が実体） |

### B. `src/webui/` / `tests/webui/` として復活した 5 ファイル（modify/delete）

main が変更、このブランチが削除・書き換えたため git がツリーに main 版を残したもの。
**内容を新しい置き場所へ手で移植し、`src/webui/` と `tests/webui/` は `git rm` で完全に削除**した。

| 残った main 版 | 移植先 | 移植内容 |
|---|---|---|
| `src/webui/app.py` | `src/web/api/app.py` | `create_app(*, audio_player=None)`（None なら `AlsaAudioPlayer`）、`app.state.audio_player`、`JobManager(..., audio_player=...)`、lifespan の `audio_player.close()`（**ジョブ join の後**＝実行中ジョブの完了音を捨てない）、`audio.router` の登録 |
| `src/webui/dependencies.py` | `src/web/api/dependencies.py` | `get_audio_player` / `AudioPlayerDep` |
| `src/webui/routers/common.py` | **`src/web/ui/layout.py`** | main の 1 行 `SECTION_LABELS["audio"] = "通知音"`。指示は `web/api/routers/common.py` だったが、このブランチでは `SECTION_LABELS` が frontend の `layout.py` に移設済み（設定ページの階層表示は表示知識なので frontend が持つ）。**実在する定義箇所へ入れた** |
| `src/webui/routers/pages.py` | **`src/web/ui/layout.py`** | `TABS["dev"] += ("audio",)` / `FEATURE_LABELS["audio"]` / `FEATURE_TEMPLATES[("dev","audio")]`。frontend の表示知識テーブルなので `web.ui.pages` ではなく `layout.py`。page ハンドラ自体は変更不要（汎用 feature ルートが `FEATURE_TEMPLATES` を引くだけ = backend JSON をテンプレへ渡す薄いラッパーのまま） |
| `tests/webui/test_app.py` | `tests/web/api/test_app.py` | `test_lifespan_closes_injected_audio_player_once`（注入プレイヤーを 1 回だけ close する契約）を移植 |

### C. 意味的判断を伴った衝突

- **`src/web/api/jobs/manager.py`** — main は `audio_player` 引数と完了音再生を追加、
  このブランチは `_publish` → public `publish`（`control_changed` 配信のため）。
  衝突は `__init__` の docstring 1 箇所だけで、**両方の Args を並べた**。
  `publish` / `_play_completion_sound` の実体はどちらも残っている
- **`src/web/api/jobs/{pasting,posctrl}.py`** — 衝突は import 行のみ。main の改名
  `align_component_groups` → `align_regions` を採り、モジュールパスはこのブランチの
  `web.api.*` にした（`board_ops.py` 自体は自動マージ済みで `align_regions` を持つ）
- **`src/web/api/config_store.py`** — main は `[audio]` の validator 追加（自動マージ済み）と
  `SettingValueType` から `"bool"` を削除（air pump オプション廃止）。このブランチは型
  エイリアスを `web.api.models` へ移して再 export。**エイリアスの定義は models 側を維持**し、
  そこから `"bool"` を落として main の意図を反映した（`MACHINE_FIELDS` に bool 項目は無く、
  `_coerce` にも `case "bool"` が無いので stale なリテラルだった）。`MachineSettingValue` の
  `bool` は残す（JSON の true/false を 400 にするため）という main のコメントも models 側へ移した。
  `write_text_atomic` / ロック機構には触っていない
- **`src/web/ui/static/js/job_console.js`（自動マージ）** — main の Web Audio 62 行削除と
  このブランチの操作権処理追加が両立していることを確認（`audioContext` /
  `playCompletionSound` / `.wav` が 0 件、`control_changed` 中継は残存）
- **`src/web/ui/templates/{base,settings}.html`（自動マージ）** — main の audio 属性 /
  bool checkbox 削除と、このブランチの操作権バー・`job_console.js` 読み込みが両立
- **`tests/helpers.py`** — main は `_alsa_audio_available` / `skip_if_no_alsa_audio` /
  `FAKE_AUDIO_DEVICES` / `FakeAudioPlayer` を追加、このブランチは
  `_skip_if_hardware_unavailable` → `_skip_unless_available` に改名し mDNS ヘルパを追加。
  **改名後の名前 1 つに寄せて main の追加を全部載せた**（docstring は「カメラ / ALSA
  接続・mDNS 可否」に拡張）
- **`tests/web/api/conftest.py` / `jobs/conftest.py` / `jobs/test_manager.py` /
  `jobs/test_board_ops.py` / `test_preview.py`** — 衝突は import / 引数の並びのみ。
  `webui.*` → `web.api.*` にしたうえで main の追加（`FakeAudioPlayer`、`Audio`、
  `AudioPlaybackError`、`TESTING_DATA_DIR`、`audio_player=` 引数）を全部載せた。
  `test_board_ops.py` は main が削除した `pad_align_abort_message` の import を落とした
  （使用箇所は main 側で既に消えている）
- **`tests/web/ui/test_pages.py`** — docstring の追記合戦。main の
  「webui-audio-output 計画書」節を MR2 節の**前**に置いて両方残した
- **`tests/e2e/conftest.py`** — `create_app(e2e_settings, audio_player=FakeAudioPlayer())`
  を採用（実 `aplay` で実スピーカーが鳴るのを防ぐ）。docstring はこのブランチの
  live_server 説明に main の注入理由を足した
- **`tests/e2e/test_browser_ui.py`** — (1) import 衝突は両方載せ、(2) 完了通知テストは
  main の「ブラウザは wav を取得しない」版を採用し、ページ遷移先だけ `live_server` →
  `live_ui` に直した（ページを描くのは frontend）
- **`README.md`** — main の「### 通知音」節を、このブランチの WebUI 章（PCB ファイル節の
  後・systemd 節の前）に挿入。「backend 機で鳴る」「テスト再生は操作権を要する」を追記。
  main の「環境変数で動作を切り替えられる（全量は `src/webui/settings.py`）:」1 行は**落とした**
  （このブランチの「### 環境変数」節が backend / frontend 別に `PCBASM_API_*` /
  `PCBASM_UI_*` を列挙しており、旧 prefix の重複記述になる）。main の
  「サービス実行ユーザーから ALSA PCM を再生できるか確認」は systemd 節に残した

### D. 操作権ゲート（MR6 規約）を新エンドポイントへ適用

- `GET /api/audio/settings` → **開放**（全 GET は開放）。`UNGATED_REQUESTS` に追加
- `POST /api/audio/test` → **`ControlDep` を追加**（機体のスピーカーが実際に鳴るので
  `machine-control` と同じ扱い。閲覧者が鳴らせてはいけない）。`GATED_REQUESTS` に追加。
  認可は `Depends` で行う（ハンドラ本体で `claim` すると `klipper_errors_to_502()` が
  423 を 502 に化かす）
- `dev/audio.html` に印を追加: `#audio-settings-form`（設定フォーム）と
  `.audio-test-actions`（テスト再生ボタン 2 個をまとめた div）。`tests/web/ui/test_control_ui.py`
  の `_GATED_ELEMENTS` に登録した（このテストは「登録外のテンプレートは何もゲートしない」を
  掃引するので、登録しないと落ちる）
- `emergency-stop` / `jobs/current/abort` / WS `abort` / `POST /api/control/takeover` は
  未変更。`#estop` / `#jc-abort` に `data-requires-control` は付けていない

## 計画外の判断ログ

1. **`src/web/api/models.py` の `SettingValueType` から `"bool"` を削除**（衝突ファイルでは
   ない）。main が `src/webui/config_store.py` 側で消したリテラルの移設先がここだったため。
   放置しても型エラーにはならないが、main の air pump オプション廃止が半端に残る
2. **`SECTION_LABELS` / `TABS` などの追記先を `src/web/ui/layout.py` にした**（指示の
   `src/web/api/routers/common.py` ではない）。このブランチが表示知識を frontend へ
   移設済みで、backend 側に該当テーブルが存在しないため
3. **`tests/web/ui/test_layout.py::test_fetch_is_confined_to_the_funnel_and_the_completion_sound`
   を改名・期待値変更**（`{"app.js", "job_console.js"}` → `{"app.js"}`）。main が
   ブラウザ側の wav fetch を撤去したので、`job_console.js` に `fetch(` が無くなり
   このテストが落ちた。テスト名の「and_the_completion_sound」も意味を失ったので
   `test_fetch_is_confined_to_the_funnel` にした（仕様変更に伴う既存テスト更新）
4. **main が追加した `TestCopperDetectionPageOverRealHttp` を
   `tests/e2e/test_api_e2e.py` から `tests/e2e/test_browser_ui.py` へ移した**
   （`TestCopperDetectionOverBrowser`）。main はページを `live_server.base_url`
   （= backend 直）で開いていたが、このブランチでは backend はページを配信しない
   （`#canny-low` が見つからず 30 秒タイムアウトで失敗した）。`live_ui` 経由に直し、
   `#canny-save` が `data-requires-control` なので `_acquire_control` を足した。
   両ファイルの docstring が「実ブラウザ操作は test_browser_ui.py」と分担を宣言している
   ので、in-place 修正ではなくそちらへ移した。合わせて `test_api_e2e.py` で未使用に
   なった `from playwright.sync_api import expect` を落とした
5. **`tests/e2e/test_browser_ui.py::TestAudioPageOverBrowser` の 2 テストを
   `live_ui` + `_acquire_control` に直した**（同じ理由。main は `live_server` でページを
   開いていた。テスト再生は今 `ControlDep` を通るため操作権が必要）

## main 側で落としたもの・縮退させたもの

- **落としたもの: README の「環境変数で動作を切り替えられる（全量は
  `src/webui/settings.py`）:」1 行**（上記 C 参照。このブランチの「### 環境変数」節が
  backend / frontend 別に完全な一覧を持つので、旧 prefix・旧モジュールを指す重複記述）
- **機能の縮退はゼロ。** 通知音の HAL・音源 wav・router・JS・テンプレート・
  設定フィールド・ジョブ完了時の Pi 再生・ブラウザ Web Audio の撤去・ALSA テストは
  すべて保持している
- **`POST /api/audio/test` に操作権ゲートを足したのは「縮退」ではなく MR6 規約の適用**
  だが、main 単体の挙動（誰でも鳴らせる）からは変わっている。閲覧者はテスト再生
  できない

## 確認事項（orchestrator の裁定が要るかもしれない点）

1. **`POST /api/audio/test` のゲート** — 指示どおり `ControlDep` を付けた。「機体の
   スピーカーが鳴るだけで装置は動かないので開放でよい」という判断もあり得る
   （どちらでも安全側。現状は閲覧者は鳴らせない）
2. **判断ログ 4（e2e テストのファイル間移動）** — 「衝突解決」の範囲を少し超えて
   テストを別ファイルへ移した。in-place 修正（`test_api_e2e.py` に browser テストを
   1 件だけ残す）でも通る

## 検証結果

- `make format`: pass（2 回連続で全 hook Passed）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（2299 passed / 128 deselected）
- `make test-e2e`: pass（90 passed / 2337 deselected）
- `git diff --name-only --diff-filter=U`: 空
- 衝突マーカー・`</content>`: 0 件
- `src/webui/` `tests/webui/` の残存: 0 件

実機テスト（`make test` / `@mark_hardware`）は未実行。**通知音は実スピーカーが鳴るため
`tests/pcbasm/hal/test_audio.py` の `@skip_if_no_alsa_audio` 付き実機テストと
`/dev/audio` のテスト再生はユーザー確認が必要。**
