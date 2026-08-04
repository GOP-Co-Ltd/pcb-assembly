# MR2 — backend の自己申告 API と `machine_name`（web-api-ui-split）

計画書: `docs/plans/web-api-ui-split.md`「MR2」節（`git show
origin/docs/20260730/web-api-ui-split:docs/plans/web-api-ui-split.md`）。

前回の実装エージェントが API 529 で中断したため、`src/` の項目 1〜3 は引き継ぎ時点で完成
していた。このセッションで足したのは項目 4（`pcb_browse_allowed`）と全テスト。

## 計画外の判断ログ

### 1. `pcb_browse_allowed` の判定を 2 段階（`_is_allowed` / `_leads_to_allowed`）にした

計画書は「許可サブツリーの祖先ディレクトリは列挙に出す」だけを指定していた。実装は
`routers/files.py` に 2 つの述語を置き、意味を分けた。

- `_is_allowed(p)` = `p` が許可サブツリーの内側。中身の列挙・PCB 選択・アップロードを許す
- `_leads_to_allowed(p)` = 許可サブツリー自身か、そこへ辿る途中の祖先。**列挙だけ**許す

祖先ディレクトリの列挙では、子のうち「許可サブツリーへ続くディレクトリ」だけを返す。
`/` の列挙に `media` `mnt` `home` は出るが `/etc` や `/` 直下のファイルは出ない。祖先を
「素通し」にすると `/` の全ファイルが見えてしまい、面を潰す目的を達しない。

### 2. アップロード先の検証は 400 で返した

`pcb_upload_dir` はユーザー入力ではなく設定値だが、計画書が「アップロード先で許可サブ
ツリーを検証する」と明記しているので実装した。保存（`mkdir` / `write_bytes`）より前に
検証するので、範囲外設定でディレクトリが作られることはない。

### 3. `_resolve_under_root` は分割せず、上に `_resolve_browsable` を重ねた

`GET /api/files` だけが「祖先も可」という緩い判定を必要とするため、root 判定
（traversal 防止）と公開範囲判定を別関数に保った。`PUT /api/pcb-file` は
`_resolve_browsable`（root + 許可サブツリー）を使う。

### 4. `Settings.from_env` に `pcb_browse_allowed` の env 上書きは追加しなかった

計画書に無く、`make webui-fake` も `PCBASM_WEBUI_PCB_ROOT` を設定しない（= 既定の 3 パス
がそのまま効く）ため、投機的実装を避けた。`PCBASM_WEBUI_PCB_ROOT` で root を独自の場所へ
向ける運用が出てきたら、そのとき明示的に足す。

### 5. 既存 `src/` の不備 1 件を直した

`webui/models.py` の `JobSpecInfo.loading_stages` の既定値が `""` で、
`JobDefinition.loading_stages` の既定値 `"ローディング"` と食い違っていた。`/api/jobs` は
常に値を埋めるので現状は無害だが、frontend が共有する contract モジュールで既定値が
ずれているのは罠なので `"ローディング"` に揃えた。

## 既存テストへの影響

### 書き換えた 1 本（計画書が意図的に変える契約）

`tests/webui/routers/test_pages.py::TestPages` の
`test_mainsail_link_follows_request_host_when_unset` →
`test_mainsail_link_resolves_from_machine_id_when_unset`。

削除対象の `request.url.hostname` フォールバック（`href="http://testserver"`）を assert
していた。新契約（`Settings.hostname` = `machine_id` から解決し、SSR も
`/api/machine-info` と同じ値を使う）を検証する形へ書き換え、テスト名も実態に合わせた。
期待値を緩めたのではなく、`http://testserver` が**出ないこと**も併せてピンしている。
**mainsail_url 解決変更で期待が変わった既存テストはこの 1 本だけ**（他は
`webui_settings.mainsail_url` を明示指定しているため無影響）。

### fixture に `pcb_browse_allowed` を明示追加（計画書の決定事項どおり）

- `tests/webui/conftest.py` の `webui_settings` → `(pcb_root,)`
- `tests/webui/conftest.py` の `real_settings` → `(PROJECT_ROOT,)`
- `tests/e2e/conftest.py` の `e2e_settings` → `(pcb_root,)`

既定値（リポジトリルート + `/media` + `/mnt`）は変えず、他フィールドからの暗黙導出も
していない（許可リストはセキュリティ境界なので自動で広がらせない）。

`test_files.py::TestPcbFileApi::test_browse_root_slash_allows_any_mounted_path` は
`test_browse_root_slash_allows_any_allowed_subtree_path` に改名した（`pcb_browse_root="/"`
のままでも許可サブツリーなら選べる、という新しい契約に docstring を合わせた）。既存の
assert（200 + 選択された相対パス）は変えていない。

## 追加したテスト（計画書「テスト」節の対応）

| 計画書の要求 | 場所 |
| --- | --- |
| 新規 2 エンドポイントの契約 | `tests/webui/routers/test_app_state.py::TestMachineInfoApi`、`tests/webui/routers/test_jobs.py::TestJobCatalogApi` |
| hidden ジョブが `/api/jobs` に出る | `TestJobCatalogApi::test_includes_hidden_jobs`（`register_synthetic(hidden=True)`） |
| 保存済み既定値が `params` に反映 | `TestJobCatalogApi::test_params_reflect_saved_defaults` / `test_params_ignore_type_mismatched_saved_values` |
| `nozzle_cap` 部分定義の回帰（MR2 の要） | `TestStateApi::test_partially_recorded_nozzle_cap_reports_null_without_error`、`TestNozzleCapPage::test_partially_recorded_cap_keeps_every_page_renderable`（`ALL_PAGE_URLS` = SSR 全 22 ページを parametrize）。素材は `tests/webui/conftest.py` の `partial_nozzle_cap` fixture |
| `machine_name` 保存でコメント保持 + トップレベル bare key | `tests/webui/test_config_store.py::test_write_machine_name_stays_a_top_level_bare_key`（`tomllib` 再パース）/ `test_write_machine_name_preserves_comments_and_other_lines` |
| `JobDefinition` 3 フィールドから SSR コンテキスト導出 | `tests/webui/routers/test_pages.py::TestJobDefinitionDrivenContext` |
| `pcb_browse_allowed` の境界 | `tests/webui/routers/test_files.py::TestPcbBrowseAllowed` |
| `models.py` が pydantic 以外を import しない | `tests/webui/test_models.py`（AST 検査） |

`Machine.machine_name` 自体は `tests/pcbasm/test_config.py::TestMachineName`。

`TestJobDefinitionDrivenContext` は「ハードコード辞書が消えた」ことを private 名の不在で
はなく**導出の一致**でピンしている: pasting の全ジョブ feature について
`("preview-pane" in html) is definition.provides_preview` と
`("loading-controls" in html) is (definition.loading_param is not None)`、および
`data-loading-stage` / `loading_default` が定義側の値と一致すること。辞書を復活させて
定義と食い違わせると落ちる。

## 既知の制約・残課題

- `/api/machine-info` と `/api/jobs` の e2e（計画書の「実機確認」= `curl`）は追加して
  いない。`make test-no-hardware` の検証ループに入らないテストを増やすのを避けた。
  実機確認はユーザー担当。
- `models.py` の AST 検査は `webui/models.py` のみが対象。MR3 で `web/api/models.py` に
  リネームされる際は import パスの追従が必要。
- `API_VERSION`（現在 `1`）は `webui/models.py` にある。MR5 の `discovery.py` はここから
  import して共有する（pydantic 以外に依存しない場所という計画書の条件を満たす）。
- `machine_name` は設定画面の「マシン」セクションに出るだけで、ヘッダなどへの表示は
  していない（計画書 MR2 の範囲外。frontend のマシン選択ドロップダウンは MR4/MR5）。

## 他 implementer への IF 変更通知

並列実装は無し。`Settings` に 2 フィールド（`pcb_browse_allowed` / `hostname`）が増えた
ので、`Settings(...)` を直接構築するコードを足す場合は tmp を root にするなら
`pcb_browse_allowed` を明示すること。

## 検証結果

- `make format`: pass
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（1724 passed, 87 deselected）
- 参考: `pytest -m "e2e and not browser and not hardware"` も pass（20 passed）
  — `tests/e2e/conftest.py` を触ったため確認した
- `grep -rn '</content>' src tests`: 空
- コミットは作っていない（分割は orchestrator）

______________________________________________________________________

## レビュー確定指摘の修正（2 巡目）

3 視点レビュー 33 件 → 反証 → orchestrator 裁定で採用された 13 件（M1〜M4 / S1〜S5 /
nit 4 件）を修正した。却下 20 件には触れていない。

### M1. `mainsail_url` のフォールバックを `.local` 付きに（ユーザー裁定）

`routers/common.py` に `_default_mainsail_url(machine_id)` を追加し、
`http://{machine_id}.local` を返す。`machine_id` が既にドットを含む場合（FQDN や
`.local` 付きの `Settings.hostname` 注入）は重ねない。SSR（`_base_context`）も
`/api/machine-info` も `build_machine_info` 1 本を通るので値は同一のまま。

### M2. `PCBASM_WEBUI_PCB_ROOT` に `pcb_browse_allowed` を追従させた

実装者ノート「4. `Settings.from_env` に `pcb_browse_allowed` の env 上書きは追加
しなかった」の判断を**撤回**する。env で root を差し替えると許可リストが既定の 3 パスの
ままで、ファイルブラウザが全パス 400 になっていた（実測）。`from_env` は
`PCBASM_WEBUI_PCB_ROOT` が与えられたとき、既定 3 パスに**その root を追加**する
（env 未設定時の既定値は不変）。docstring に理由込みで明記した。

### M3. prefix 兄弟ディレクトリを許可境界のテスト素材に追加

`tests/webui/conftest.py` の `pcb_root` fixture が `<pcb_root>-evil/secret.kicad_pcb`
を作る。列挙 400 と選択 400 を新規 2 本でピンし、既存の
`test_ancestor_listing_hides_entries_outside_allowed_subtree` の等値 assert
（`== {("pcb", "dir")}`）にも prefix 兄弟が入らないことの検出力が付いた。

### M4. `move_to_cap` を `AppState.nozzle_cap()` 経由に

`routers/machine_control.py` が生の `state.machine().nozzle_cap` を読んでいた。
`ClassValidationError` は `ExceptionGroup` 派生で `ValueError` ではないため
`except ValueError` を通り抜け、部分記録の `[nozzle_cap]` で 500 になっていた。

### S1. 全ページ parametrize を「壊れた machine.toml」へ転用（対象は 19 ページ）

`broken_machine_toml` fixture（終端されていない文字列を追記）を新設し、
`TestBrokenMachineTomlPages` に付け替えた。nozzle_cap の回帰は実際に読む 3 経路
（`/api/state` / `/pasting/nozzle_cap` / `move_to_cap`）に絞った。
`state.py` の `nozzle_cap()` docstring の事実誤認（「全ページの SSR に載る」）も直した。

**計画（裁定）との差分 1 点。** 裁定は「23 本すべてに検出力が出る」としていたが、
実測では 4 ページが**壊れた machine.toml で現に 500 する**:

| URL | 500 する理由 |
| --- | --- |
| `/settings` | `ConfigStore.read_machine_settings`（tomlkit パース）を防御外で呼ぶ |
| `/pasting/paste_solder` | 同上（auto しきい値の現在値） |
| `/pasting/loading` | `state.machine().paste_dispenser` を防御外で呼ぶ |
| `/posctrl/copper_detection` | `state.machine().paste_dispenser.pad_align` を防御外で呼ぶ |

この 4 ページを `_MACHINE_TOML_DEPENDENT_URLS` として明示除外し、残り 19 ページを
`ROBUST_PAGE_URLS` として parametrize した。除外理由はテストのコメントと class
docstring に書いてある。**「machine.toml が壊れると設定ページ系 4 本が 500 する」のは
MR2 の防御では守れない別課題**（`_base_context` の防御対象外）で、修正は要求範囲外の
ため入れていない。→ orchestrator への確認事項。

### S2〜S5・nit

- S2: `test_settings_page_labels_the_machine_name_section`（`<summary>マシン</summary>`
  と `マシン名` ラベル、生キー `machine_name` が出ないこと）
- S3: `settings.py` のコメントを実装に合わせた（`pcb_browse_root` の「OS 全体を閲覧可能に
  する」を撤去し、`pcb_upload_dir` に `pcb_browse_allowed` 内であることを追記）
- S4: `_loading_amount_input(default)` ヘルパで `id="lc-amount" … value="…"` の断片を
  assert する形に変えた（`test_loading_jobs_render_loading_controls_with_stage_contract` /
  `test_loading_page_renders_saved_loading_defaults` / `TestJobDefinitionDrivenContext`）
- S5: `loading-controls` の有無 assert は `loading_param is None` の側だけに残した
  （`loading.html` / `dispense_calibration.html` は無条件 include なので、非 None 側では
  定数 True。段階文字列と `#lc-amount` の value で 4 feature すべてに検出力を出した）
- nit: `StateResponse.nozzle_cap` の `= None` 撤去 / 撤去済み `_PASTING_*` を指す
  コメント 2 行を `JobDefinition.provides_preview` / `.loading_param` に / `test_models.py`
  の `test_detects_a_forbidden_import` 削除 / docstring の折り返し由来の半角スペース解消

## 検出力の測定（影コピー + PYTHONPATH。リポジトリは無変更）

| 壊し方 | 結果 |
| --- | --- |
| `_is_allowed` を `str(resolved).startswith(str(root))` に | 新規 2 本が落ちる（列挙 200・選択 200）。既存 6 本は緑のままなので、prefix 兄弟の追加が唯一の検出源 |
| `AppState.machine_name()` の try/except を外す | `ROBUST_PAGE_URLS` の **19 本すべて**が落ちる（修正前は 1 本も落ちなかった）。`test_unreadable_machine_type_reports_null` は緑のまま = `Machine` の属性アクセスは遅延なので `machine_type` の失敗が `machine_name` に波及しない |
| `pages.py` の `loading_default` 注入ブロックを削除 | 8 本が落ちる（修正前は 0 本） |
| `pages.py` の `loading_param = definition.loading_param` → `None` | `TestJobDefinitionDrivenContext` の **4 feature すべて**が落ちる（修正前は 2 feature） |
| `move_to_cap` を `state.machine().nozzle_cap` に戻す | `test_partially_recorded_cap_returns_400` が `ClassValidationError` で落ちる |
| `from_env` の `pcb_browse_allowed` 追従を戻す | `TestPcbRootEnvIsBrowsable` の 3 本が 400 で落ちる |
| `_default_mainsail_url` の重複回避を外す | `test_mainsail_url_does_not_repeat_local_on_a_dotted_machine_id` が `http://paste-01.local.local` で落ちる |

### 検証結果（2 巡目）

- `make format` / `make type`（0 errors）/ `make test-no-hardware`（1728 passed,
  87 deselected）
- `pytest -m "e2e and not browser and not hardware"`: 20 passed
- `grep -rn '</content>' src tests`: 空
- コミットは作っていない（分割は orchestrator）

______________________________________________________________________

## 検出力ゼロだったテストの作り直し（3 巡目）

再測定で検出力ゼロと判明した 2 点を、決定的に落ちる形へ作り直した。`src/` は無変更。

### 1. `API_VERSION` の同語反復を撤去（リテラルピン 1 本）

`tests/webui/routers/test_app_state.py` の `from webui.models import API_VERSION` を削除し、
`TestMachineInfoApi::test_reports_all_fields` の期待 dict で `"api_version": 1` とリテラル
指定した。frontend / discovery の互換判定に使う定数なので、うっかりのバンプが落ちる。
意図的なバンプ時はこの 1 箇所を同時に更新する（理由はコメントに書いた）。テスト側で
`API_VERSION` を import している箇所は他に無い。

### 2. `JobSpecInfo` ↔ `JobDefinition` の既定値同期をピン

`loading_stages` 単体ではなく、**両者で名前を共有する optional フィールド全部**の既定値
一致を `tests/webui/test_models.py::TestJobSpecInfoMirrorsJobDefinition` で検証する
（`models.py` が `webui.jobs.catalog` を import できない構造の代償が既定値の手複製である、
という裁定の理解をそのままテストにした）。

- 対象名は両者のフィールド定義から集合演算で導出し、`params` のみ除外（`ParamSpec` /
  `ParamSpecInfo` で型が別）
- 比較は**最小構築インスタンスの属性値**同士（メタデータ比較ではなく、省略時に見える値）。
  attrs の tuple と pydantic の list は `_normalized` で正規化する
- `test_mirrored_field_names_are_the_expected_set` が対象 9 名を固定する。改名や片側からの
  フィールド削除で parametrize が空回りするのを防ぐ

### 検出力の測定（影コピー + PYTHONPATH。リポジトリは無変更）

| 壊し方 | 結果 |
| --- | --- |
| `API_VERSION = 1` → `99` | `test_reports_all_fields` が落ちる（修正前は 0 本） |
| `JobSpecInfo.loading_stages` の既定値 → `""` | `test_omitted_field_takes_the_job_definition_default[loading_stages]` が落ちる（修正前は 0 本） |

### 検証結果（3 巡目）

- `make format` / `make type`（0 errors）/ `make test-no-hardware`（1738 passed,
  87 deselected）
- 注意: `tests/webui/test_models.py` は untracked のままなので `pre-commit run -a` の
  対象外。`git add -N` してから `make format` を掛けて整形済み（その後 untracked に戻した）

### docformatter / ruff-format の無限ループを踏んだ（対処済み・注意点）

docstring を `"""` の直後に `"` で始める（`""""/" の列挙は…`）と ruff-format と
docformatter が交互に書き換え合い、`make format` が何度走っても Failed になる。
docstring 冒頭を引用符で始めないこと（該当箇所は「ルート直下の列挙は…」に書き換えた）。
