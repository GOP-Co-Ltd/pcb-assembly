# single-config-dir — tests/ 移行メモ（spec-test-author）

対象計画書: `/home/gop/.claude/plans/claude-configs-config-git-configs-1-glowing-glade.md` Phase 3b。
`tests/` のみ変更。`src/` は未編集（`plan-implementer` 担当）。

## 結果

`make format` / `make type` / `make test-no-hardware` すべてグリーン（1607 passed, 87 deselected）。
src 側が同時に着地していたため「期待される失敗」は残っていない。

残骸 grep 0 件: `configs_root` / `TEST_FIXTURE_DIR` / `select_machine` / `selected_machine` /
`default_machine` / `list_machines` / `PCBASM_WEBUI_CONFIGS_ROOT` / `configs`（`tests/` 全体）。

## 共有ヘルパ

`tests/helpers.py` に `TESTING_CONFIG_DIR` と `copy_testing_config(tmp_path) -> Path` を新設。
webui / e2e の両 conftest がこれを呼ぶ（e2e 側の重複 copytree を解消。fixture の暗黙再エクスポートはしない）。
`data/testing/machine.toml`（47 行・コア層用）を使う約 25 箇所は無変更。

## fixture 改名

- `tests/webui/conftest.py`: `configs_root` → `config_dir`（2 マシン複製 → 1 回の copytree）。
  `webui_settings` は `config_dir=` を明示（`Settings.config_dir` が env factory になったため、
  素の `Settings()` は env を拾う。アプリ fixture は env を使わず hermetic に保つ方針を維持）
- `real_settings`（`@mark_hardware` 専用）は `config_dir=PROJECT_ROOT / "config"`

env を使うのは 2 箇所のみ: `tests/webui/test_settings.py`（`from_env()` 契約）と
`tests/pcbasm/test_config.py::TestGetMachineConfig`（コア層の唯一の注入口）。

## 削除したテスト（機能廃止）

- `routers/test_machine.py` → `routers/test_app_state.py` に `git mv`。`TestMachineApi` 3 件削除
- `test_state.py`: `TestMachineSelection` から 5 件、`test_select_machine_while_locked_raises_busy_error`、
  `test_select_machine_rebuilds_hub`
- `test_config_store.py`: `TestListMachines`
- `routers/test_pages.py`: `"マシン選択:"` assert
- `e2e/test_webui_e2e.py`: `test_state_reports_selected_machine`

残した保証（`TestPcbSelection` へ移設し `selected_pcb` ベースに書き換え）:
`test_corrupted_state_file_falls_back_to_default`（壊れた JSON → `selected_pcb is None`）、
`test_legacy_state_file_is_read_when_new_file_missing`（legacy `data/webui_state.json` の `pcb_file`）。

## 検証手段の付け替え

- `test_app.py` 409 ハンドラ / `routers/test_jobs.py` `state_changed` ブロードキャスト
  （`test_pcb_switch_broadcasts_state_changed` に改名）/ `jobs/test_manager.py` の busy 検証は
  `PUT /api/pcb-file` ないし `state.select_pcb(...)` へ
- `test_config_store.py` の `test_unknown_machine_raises_file_not_found` は
  `test_missing_machine_toml_raises_file_not_found`（存在しない config_dir の `ConfigStore`）に。
  machine 引数が消えても「machine.toml 不在 → FileNotFoundError」の契約自体は残した
- `routers/test_nozzle_cap.py` は `PROJECT_ROOT` 直接連結をやめ `real_settings.config_dir` 経由に

## 計画外だが必要だった変更（報告事項）

計画書 3b の「17 ファイル / 64 箇所」リストに **`tests/webui/test_board_settings.py` が入っていなかった**が、
`BoardSettingsStore` の全メソッドから `machine` 引数が消え保存パスから machine セグメントが外れる
（計画書 Phase 3 IF 表 / 調査事項 1）ため必須。実施内容:

- `load_or_init` / `save` / `export_doc` / `prune` の第 1 引数 `"kurousagi"` を削除
- `model_from_doc(expected_machine=...)` を削除
- 保存パス `board_settings/<machine>/<id>.json` → `board_settings/<id>.json`（`_saved_doc` ヘルパ含む）
- `doc["machine"]` assert とテスト名 `test_doc_has_version_source_machine_settings` →
  `test_doc_has_version_source_pcb_settings`、legacy JSON fixture の `"machine"` キー

同様に vestigial な `machine` フィールドの assert を `routers/test_settings_api.py`（`data["machine"]`）と
`routers/test_pasting.py`（pad-config / export の `["machine"]`）から削除した。

その他の改名（振る舞い不変）:
`jobs/test_context.py`: `test_machine_is_selected_machine_config` → `test_machine_is_config_dir_machine`、
`test_source_pcb_and_machine_name_reflect_selection` → `test_source_pcb_reflects_selection`。
`routers/test_pasting_loading.py`: `test_density_comes_from_selected_machine` →
`test_density_comes_from_machine_config`。

## モック方針

モックは一切追加していない。実ファイル（tmp コピーの machine.toml / キャリブ JSON）・実 HTTP
（TestClient / httpx）・実 uvicorn のまま。`class TestXxx` 構造も維持。
