# single-config-dir レビュー指摘対応（src / tests 側）

対象: ブランチ `refactor/20260727/single-config-dir`。
`memory/agents/code-reviewer/single-config-dir.md` の should-fix / nit のうち、
orchestrator が採用を裁定した項目のみを実施。公開 IF は不変。

## 実施内容

### S2. CSS orphan の削除

- `src/webui/static/app.css`: `.settings-header p`（4 行ルール）を削除。
  `grep -rn "settings-header" src/webui/templates src/webui/static` で
  `settings.html:65` の `<header>` のみ、配下の `<p>` は 0 件を確認済み。
- 同ファイルの `.global-controls` 直後にあった空行 2 連続を 1 行に（machine-select
  ルール削除の跡）。

### S3. テスト fixture の用途差を明記

- `tests/webui/conftest.py` の module docstring に 1 項目追加:
  `data/testing/config/machine.toml` が WebUI / E2E 用、
  `data/testing/machine.toml` / `machine_minimal.toml` が pcbasm コア層の単体テスト用。
  （`data/config-templates/README.md` 側の表への追記は orchestrator 担当）

### S4. `legacy_root` が到達不能である旨の注記

- `src/webui/board_settings.py` の `__init__` docstring（`legacy_root` 引数）に
  「machine セグメント除去により、実在しうる旧データ
  `data/board_settings/<machine>/<board_id>.json` とは一致しない」を追記。
- `tests/webui/test_board_settings.py::test_legacy_root_is_read_only_fallback` に
  docstring を追加し、ピンしているのは経路の read-only 性だけであることを明記。
- コード自体は削除していない（元から dead。CLAUDE.md 原則 3）。

### nit 群

- `tests/webui/jobs/conftest.py` の `real_appstate` docstring:
  「実機（実 Moonraker, kurousagi）向け」→「実機（実 Moonraker）向け」。
- `tests/webui/routers/test_machine_control.py`:
  「実 Moonraker（kurousagi）に対する操作」→「実 Moonraker に対する操作」。
- テスト docstring / コメント内の「test-fixture」というマシン名表現 18 箇所を
  「テスト用 config」系の表現へ。**テスト関数名・fixture 名は未変更**。
  対象: `tests/webui/jobs/{test_machine_commands,test_pasting,test_posctrl}.py`,
  `tests/webui/routers/{test_jobs,test_nozzle_cap,test_machine_control,test_pasting}.py`。
  ファイル名由来の `ov9281_test_fixture.json` は正しいので触っていない。
- docformatter による文頭大文字化でパス表記が壊れていた 4 箇所をバッククォート囲みに:
  `src/webui/config_store.py`（`Config/ 配下` → `` `config/` 配下``）、
  `src/pcbasm/config.py`（`Config ディレクトリ` → `` `config/` ディレクトリ``）、
  `tests/helpers.py`（`Data/testing/config` → `` `data/testing/config` ``）、
  `tests/webui/{conftest,test_state}.py`（`Tmp_path` / `Config ディレクトリ`）。
  バッククォートで囲むと docformatter が再度大文字化しない。

## 実施しなかったもの（裁定どおり）

- `src/webui/routers/pages.py` の `_paste_solder_context` 未使用 `state` 引数 → 残置。
- `create_app()` の TOML 検査追加 / `_SCHEMA_VERSION` の bump /
  `test_corrupted_state_file_falls_back_to_default` の書き換え /
  `data/testing/config/printer.cfg` の削除 → 却下。
- shell スクリプト・`data/config-templates/README.md`・`.gitignore` 等 → orchestrator 担当。

## 申し送り

初回 `make format` で mdformat が md ファイルを改変した（`data/config-templates/README.md`
を含む可能性がある。orchestrator が並行編集中のファイル）。2 回目の実行は全 hook Passed で
再改変なし。orchestrator 側で当該ファイルの diff を確認しておくこと。

## 検証

- `make format`: pass（2 回目は改変なし）
- `make type`: 0 errors / 0 warnings
- `make test-no-hardware`: 1607 passed, 87 deselected（維持）
- 実機テストは未実行（方針どおり）
