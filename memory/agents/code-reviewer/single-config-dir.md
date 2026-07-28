# configs/ 廃止 → 単一 config/ レビュー

対象: ブランチ `refactor/20260727/single-config-dir` の未コミット変更全体（未追跡の
`setup-machine-config.sh` / `scripts/migrate_config_layout.sh` を含む）。
仕様正典: `/home/gop/.claude/plans/claude-configs-config-git-configs-1-glowing-glade.md`。

## verdict: approve

must-fix なし。仕様準拠・パス解決・マシン選択の全廃・テスト移行はいずれも計画書の公開 IF に
一致しており、残骸も 0 件。should-fix は 4 件（うち 1 件は実機データ損失リスクなので
コミット前に裁定推奨）、nit は 9 件。

## must-fix

なし。

## should-fix

### S1. `setup-machine-config.sh` の再実行が `~/printer_data/config/printer.cfg` を無条件に置き換える（深刻度: 高 / 確信度: 高）

対象: `setup-machine-config.sh:98-107, 155-160`

`printer_action` は 3 分岐すべてで最後に `cp "${template_path}/printer.cfg" "$KLIPPER_CONFIG_FILE"`
へ落ちる。初回実行後に実機で `SAVE_CONFIG`（`load_cell_probe` 較正・`[stepper_*]` の
`position_endstop` 等）が走った状態で 2 回目を実行すると、`[ -f ]` 分岐に入り
`.bak.<TS>` へ退避したうえで **live の printer.cfg がテンプレートのスナップショットへ巻き戻る**。

根拠:

- 同スクリプトは `config/machine.toml` について「既存なら常にスキップ」という方針を明示的に
  採っている（`:72-84`、README `:58-59`「上書きすると操作者の設定が黙って巻き戻る」）。
  printer.cfg も `SAVE_CONFIG` で実測値が蓄積されるファイルであり、同じ理由が当てはまるのに
  扱いが非対称。
- 計画書 Phase 4「ユーザーによる実機確認」3 は **「`./setup-machine-config.sh` を 2 回実行し
  冪等性を確認」** を指示している。較正後にこれを実行すると較正値を失う。
- 計画書の挙動表は「実ファイルなら `.bak.<TS>` へ退避 → テンプレートを実ファイルとして配置」と
  書いており、**この実装は計画書どおり**。仕様違反ではなく、計画自体に残った穴。

緩和されている点: 実行前の plan 表示に「既存を printer.cfg.bak.<TS> へ退避して配置」と出るため
完全な無告知ではない。バックアップも残る。

提案（どれか 1 つ）: (a) `[ -f ]` かつ内容がテンプレートと異なる場合はスキップし
「作り直したいなら printer_data 側を退避してから」と案内する、(b) plan 行に
「SAVE_CONFIG の較正値が巻き戻ります」を明記する、(c) README に再実行時の注意を追記する。

### S2. `app.css` の `.settings-header p` が orphan になっている（深刻度: 低 / 確信度: 高）

対象: `src/webui/static/app.css:448-452`

`settings.html:68` から `<p>{{ selected_machine }}</p>` を削除した結果、`.settings-header`
配下の `<p>` はリポジトリ全体で 0 件（`grep -rn "settings-header" src/webui/templates` は
`settings.html:65` のみ）。計画書 3a-6 は `.machine-select-label` / `#machine-select` を
「自分の変更で生じた orphan」として削除対象に挙げていたが、同種のこれが漏れている。
CLAUDE.md 原則 3「自分の変更で生じた orphan は消す」に該当。

あわせて `settings.html:66-68` の `<div>` は `<h1>` 1 要素だけを包む無意味なラッパーになり、
`.settings-header` の `justify-content: space-between`（2 要素前提）も機能しなくなっている。
plan-implementer メモが「構造変更はスコープ外」として残置を選択しているので、CSS の orphan 削除
だけに絞るのが妥当。

### S3. `data/config-templates/README.md` の役割表に `data/testing/machine.toml` が無く、`tests/helpers.py` の参照先が空振りしている（深刻度: 中 / 確信度: 高）

対象: `data/config-templates/README.md:10-15`、`tests/helpers.py:19-21`、`tests/webui/conftest.py:1-11`

`tests/helpers.py` は

```python
# コア層用の data/testing/machine.toml とは別物（用途差は data/config-templates/README.md 参照）
```

と README を指しているが、README の 4 分割表は `config/` /
`data/config-templates/<machine>.<用途>/` / `data/testing/config/` /
`~/printer_data/config/printer.cfg` の 4 つで、**`data/testing/machine.toml` に一切触れていない**。
計画書リスク 6 は「`data/config-templates/README.md` の役割表と `tests/webui/conftest.py` の
docstring に用途差を明記」を明示的に要求しており、どちらも未達。

併存自体はユーザー判断で承認済みなので、必要なのは表への 1 行追加（コア層用・47 行・
`klipper 192.168.1.100:7125`）と conftest docstring への 1 文だけ。

### S4. `BoardSettingsStore(legacy_root=...)` の fallback が到達不能になり、テストが実在しない契約をピンしている（深刻度: 低 / 確信度: 高）

対象: `src/webui/board_settings.py:294-303`、`src/webui/app.py:98-100`、
`tests/webui/test_board_settings.py:328-341`

`_doc_path` の legacy fallback は machine セグメント除去後 `data/board_settings/<board_id>.json`
を見る。旧レイアウトは `data/board_settings/<machine>/<board_id>.json` だったので、**実在しうる
旧データとは永久に一致しない**（両ディレクトリのディスク上不在は確認済み: `data/webui/board_settings`
`data/board_settings` ともに存在しない → データ移行不要という計画書の前提は正しい）。

問題は `test_legacy_root_is_read_only_fallback` が「新レイアウトの legacy_root」を自分で書いてから
読めることを確認しており、**本番で起こり得ない経路を保証として固定している**点。計画書は
`legacy_root` を「触らない（元から dead）」に分類しているので削除はスコープ外だが、テストの
docstring か `app.py` の配線側に「machine セグメント除去により旧レイアウトとは一致しない」旨を
1 行残さないと、次に触る人が生きた後方互換だと誤読する。

## nit

1. **`app.css:159-161` に空行が 2 連続で残っている**（確信度: 高）。削除したルールの前後の
   空行が両方残った跡。pre-commit に CSS フォーマッタが無いため検出されない。
2. **`tests/webui/jobs/conftest.py:152` の docstring が「実機（実 Moonraker, kurousagi）向け
   AppState」のまま**（確信度: 高）。同じ変更で `tests/webui/conftest.py:196-203` の
   `real_settings` docstring は「実機向け Settings」に更新済みなので、姉妹 fixture 間で不整合。
   `tests/webui/routers/test_machine_control.py:144`「実 Moonraker（kurousagi）に対する操作」も同種。
   マシン名の概念自体を廃止した変更なので、この 2 箇所は追随したほうがよい。
3. **テスト docstring / コメントに「test-fixture」というマシン名表現が 18 箇所残っている**
   （確信度: 高、`tests/webui/jobs/test_posctrl.py`, `test_pasting.py`,
   `routers/test_machine_control.py`, `routers/test_jobs.py`, `routers/test_pasting.py`,
   `routers/test_nozzle_cap.py`, `jobs/test_machine_commands.py`）。ディレクトリ名は
   `data/testing/config/` になり「test-fixture マシン」は存在しない。ファイル名由来の
   `ov9281_test_fixture.json` は正しいので区別が必要。機械的置換はスコープを広げるため、
   裁定は orchestrator に委ねる（CLAUDE.md 原則 3 的には触らない選択も妥当）。
4. **`create_app()` の fail-fast が存在チェックのみ**（確信度: 中）。`machine.toml` が壊れた TOML /
   必須キー欠落の場合は `is_file()` を通り、`focus_z()` / `machine_type()` が全例外を握り潰すため
   計画書 3a-4 が問題視した「黙って壊れる」状態は残る（`_copper_detection_context` /
   `_nozzle_cap_context` / `create_klipper` は 500）。`Machine(store.machine_toml_path()).machine_type`
   まで踏めば起動時に捕まえられるが、計画書の要求は「不在時の fail-fast」なので追加はスコープ外。
5. **`test_corrupted_state_file_falls_back_to_default` の assert が `selected_pcb is None` になり、
   `test_initial_pcb_is_none` と区別が付かなくなった**（確信度: 高）。実質的に残っている保証は
   「壊れた JSON でも `AppState.__init__` が例外を出さない」ことだけ（`_load_persisted:225-228` が
   `ValueError` を吸うので保証自体は成立している）。有効な `pcb_file` を書いた state を壊してから
   読ませると「fallback」の意味が復活する。
6. **`_SCHEMA_VERSION` を 1 のままにしたため、`export_doc` の JSON 形状変更（`machine` キー削除）が
   バージョンで区別できない**（確信度: 高）。実害の方向は安全側（旧 export JSON の余分な
   `machine` キーは `model_from_doc` が無視するので import 可能）で、逆方向（新 export を旧コードへ）
   は単一デプロイのため発生しない。計画書どおりなので指摘のみ。
7. **`_paste_solder_context(state, store)` の `state` が未使用になった**（確信度: 高、
   `src/webui/routers/pages.py:303`）。`_FEATURE_CONTEXT` の共通シグネチャ
   `Callable[[AppState, ConfigStore], ...]` に縛られており、`_nozzle_cap_context` /
   `_copper_detection_context` の `store` 未使用と同じ既存パターン。残置が妥当。
8. **シェルスクリプト 2 本の `config/printer.cfg` 実ファイル検出による中断が、テンプレート選択の
   後に走る**（確信度: 高、`setup-machine-config.sh:111-112`、
   `scripts/migrate_config_layout.sh:149-150`）。前提チェック（`:30-31` / `:106`）と同じ
   guard clause 位置に寄せれば、選択させてから落とす無駄が消える。書き込み前の中断なので
   安全性の問題はない。
9. **`data/testing/config/printer.cfg` は依然どのコード / テストからも読まれない**（確信度: 高）。
   `configs/test-fixture/printer.cfg` 時代からの既存状態で、今回の変更が作った dead ではないため
   削除しない判断は正しい（CLAUDE.md 原則 3）。
10. **docformatter が summary 先頭を大文字化した結果、docstring 中のパス表記が実体とずれている**
    （確信度: 高）。`config_store.py:239`「Config/ 配下の…」、`tests/helpers.py:26`
    「Data/testing/config を…」、`tests/webui/conftest.py:141`「Tmp_path にコピーした…」。
    既存コードベース全体に同じ癖があるので統一の問題。

## 確認して問題なかった重点観点

重点観点として指示された 9 項目のうち、以下は実測して問題なしと判断した（指摘なし）。

- **`.gitignore` の `/config/` アンカー**: `git check-ignore -v config/machine.toml
  data/testing/config/machine.toml` → 前者のみ `.gitignore:25:/config/` でマッチ、後者は無視されず。
  `git ls-files data/testing/config` が 3 ファイルを返し fixture は追跡下。コメント 3 行が
  「なぜ中身ごと外すか」「なぜ先頭 `/` が必須か」を説明していて、計画書リスク 2 の再発防止として十分。
- **`get_config_dir()` の呼び出し時評価**: `os.environ.get` を関数内で読み、`PROJECT_ROOT` も
  モジュールグローバル参照（`config.py:492-501`）。`monkeypatch.setenv` /
  `monkeypatch.setattr(...PROJECT_ROOT...)` の両方が効く。空文字 env は既定値へ落ちる
  （`Path("")` = `.` にならない）。`Machine.__init__` の `_config_dir = path.parent.resolve()`
  は無変更なので `calibration_file` の相対解決は移動後もそのまま成立（`config.py:475-478`）。
- **`Settings.config_dir = attrs.field(factory=get_config_dir)`**: `@attrs.frozen` での plain
  default 混在は動作し `make type` も 0 errors。フィールド順（先頭）が変わっていないため位置引数の
  互換も保たれる。テストの hermetic 性: `Settings(` を構築している 3 箇所
  （`tests/e2e/conftest.py:219`, `tests/webui/conftest.py:114,206`）すべてが `config_dir=` を明示、
  引数なし `Settings()` / `create_app()` はテスト内に 0 件、`test_settings.py` の `clean_env` は
  `PCBASM_CONFIG_DIR` を delenv 済み、`test_config.py::test_defaults_to_project_root_config` も
  delenv 済み。env leak の経路なし。
- **`create_app()` の fail-fast 置き場**: `AppState` 構築より前（`app.py:84-89`）で、失敗時に
  部分初期化された app が残らない。`AppState.__init__` ではなく wiring 層に置いた判断は妥当
  （`AppState` は fixture が直接構築するため、そこで落とすとテストの自由度を奪う）。
  メッセージにパスと `./setup-machine-config.sh` の案内がある。網羅性の限界は nit 4 に記載。
- **`BoardSettingsStore` の machine セグメント除去**: `data/webui/board_settings` /
  `data/board_settings` ともにディスク上不在を確認（データ移行不要）。`legacy_root` の扱いは S4、
  `_SCHEMA_VERSION` は nit 6。
- **マシン選択廃止の取り残し**: `grep -rn "machines|selected_machine|machine-select"
  src/webui/templates src/webui/static` = 0 件、JS の `state.machine` 参照 0 件、
  `configs_root|TEST_FIXTURE_DIR|select_machine|selected_machine|machine_name|list_machines|
  default_machine|PCBASM_WEBUI_CONFIGS_ROOT` が src・tests で 0 件、`grep -rn "configs" src/` = 0 件。
  WS `state_changed` は `{"type": "state_changed"}` のみのペイロード（`jobs/manager.py:632-634`）で
  machine を含まないため整合。CSS orphan は S2 のみ。
- **テスト品質**: モック追加 0 件（3rd-party 表面のモックは増えていない。tmp コピーの実
  machine.toml / 実 HTTP / 実 uvicorn のまま）。`class TestXxx` 集約維持。`@mark_hardware` 分離維持。
  削除 12 件で失われた保証を個別に確認 —
  409 ハンドラは `PUT /api/pcb-file` で代替可能（`files.py:126` の `state.select_pcb` が
  `BusyError` を投げ、`boards/sample.kicad_pcb` は `pcb_root` fixture が実在させるので
  pre-lock の 400/404 に落ちない）、`state_changed` ブロードキャストも
  `put_pcb_file` が `jobs.publish_state_changed()` を呼ぶので代替成立、
  `test_machine_endpoints_return_409_while_job_running` は `/api/pcb-file` の 409 assert を
  元から持っているため `/api/machine` 行の削除で失うものなし、
  hub 再構築は `TestCameraLifecycle` の `test_rebuild_camera_*` が覆う。
  `real_settings` は `PROJECT_ROOT / "config"` を指し、`config/` 不在環境でも
  `make test-no-hardware` では deselect されて fixture が評価されない（実測: 87 deselected で赤なし）。
- **シェルスクリプト 2 本**: `bash -n` 両方 OK（shellcheck は未インストール・pre-commit にも無い）。
  `${json_to_copy+"${json_to_copy[@]}"}` は空配列でも `set -u` 下で安全、非空では空白入り要素も
  glob 文字も保持されることを bash 5.2.37 で実測確認（bash 4.4+ なら `"${arr[@]}"` で足りるので
  この形は過剰だが害はない）。`for json in "$dir"/*.json` の glob 不一致ガード
  （`[ -e "$json" ] || continue`）あり。`config/machine.toml` は両スクリプトとも既存なら
  スキップし上書きパスを持たない。`config/printer.cfg` が実ファイルなら書き込み前に `die`。
  dangling symlink は `[ -L ]` が先に判定されるため正しく張り直される。
  klipper.env の sed は置換先が定数なので冪等、かつ `env_action=noop`（`grep -Fq` 一致時）で
  そもそも sed を走らせない。sed 後の `grep -Fq` 検証と失敗時警告あり。
  **`migrate_config_layout.sh` を実機の現状に当てた場合の追跡**:
  `~/printer_data/config/printer.cfg` は `configs/kurousagi002/printer.cfg` への symlink、
  klipper.env は `klippy.py /home/gop/pcb-assembly/configs/kurousagi002/printer.cfg` を指している。
  → `printer_action=replace_symlink` で symlink のみ `rm`（実体は残る）→ `cp` で実ファイル化 →
  `ln -sfn` → sed が `[^ ]+\.cfg` にマッチして `/home/gop/printer_data/config/printer.cfg` へ置換。
  期待どおり通る。`config/` は未作成なので machine.toml とキャリブ JSON も配置される。
- **ドキュメントの正確さ**: `data/config-templates/README.md` の 4 分割表・命名規則
  （`kurousagi.paste` の `machine_type = "paste"` 一致を実測確認）・テンプレート判定条件
  （machine.toml と printer.cfg の両方）・セットアップ手順 1-4 はすべて
  `setup-machine-config.sh` の実装と一致。`calibration_file` はファイル名のみという記述も
  `Machine._config_dir` 相対解決の実装と一致（テンプレートの
  `ov9281_20260722_113444.json` が同ディレクトリに実在）。`probe_guide.html:29-42` の
  symlink 構成の説明は実装どおりで、symlink 化により成立しなくなった旧文言 2 つは削除されている。
  `README.md:73` の `PCBASM_CONFIG_DIR` 名・既定値・「WebUI と pcbasm コア層で共通」も正しい。
  未達は S3 のみ。
- **成果物汚染**: `grep -rln "</content>"` を src / tests / scripts / *.sh / *.md /
  data/config-templates に対して実行 → 0 件。
- **`install-printer-cfg.sh` 削除の取り残し**: 参照は `migrate_config_layout.sh:7` のコメント
  （経緯説明として意図的）のみ。README / install-softwares.sh / Makefile / webui-service.sh
  からの参照 0 件。

## シーケンスに関する申し送り（指摘ではない）

`.gitignore` から `configs/*/printer-*.cfg` が消えたため、ユーザーが
`scripts/migrate_config_layout.sh` を実行するまでの間、実機で `SAVE_CONFIG` が走ると
`configs/kurousagi002/printer-<TS>.cfg` が **untracked として `git status` に現れる**。
移行 + `git rm -r configs/kurousagi002` で解消する一時的な状態（計画書 Phase 1 → 2 の順序どおり）。

## 検証結果

- `make format`: pass（全 hook Passed、ファイル改変なし = 実行前後で対象ファイルの md5 集約が一致）
- `make type`: pass（0 errors / 0 warnings）
- `make test-no-hardware`: pass（1607 passed, 87 deselected, 55.84s）
- `bash -n setup-machine-config.sh` / `bash -n scripts/migrate_config_layout.sh`: pass
  （shellcheck は環境に未インストール・pre-commit にも未登録のため未実行）
- `git check-ignore -v config/machine.toml data/testing/config/machine.toml`: 前者のみ無視
- 実機テスト（`make test` / `@mark_hardware`）・スクリプト実行: 未実施（方針どおり）
