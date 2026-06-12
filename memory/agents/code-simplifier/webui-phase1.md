# WebUI Phase 1 簡素化ノート（code-simplifier）

入力: `memory/agents/plan-implementer/webui-phase1.md`。公開 IF（計画書のシグネチャ・ステータスコード、tests/webui 91 件）は不変。`tests/webui/` `configs/` は未編集。

## 実施した簡素化（正味 -41 行）

1. **config_store.py**: `_find_cfg_value` / `_replace_cfg_value` が同一のセクション走査ループを重複保持していたため、単一の `_find_cfg_line(lines, key) -> (行番号, Match) | None` に統合（rsplit も内包）。read は match から値を、write は行置換に同じ結果を使う。書き込み前の全キー検証 → 失敗時ファイル無変更のセマンティクスは維持
2. **config_store.py**: `write_motion_settings` のインラインキー検証 + assert を、`_machine_spec` と対称な `_motion_spec` + `float(_coerce(...))` に置換
3. **routers/settings_api.py**: `machine_settings_fields` / `motion_settings_fields` の同形 10 行コンプリヘンションを私的ヘルパ `_fields(specs, values)` に共通化（公開名は維持、pages.py の import 不変）。`isinstance(v, bool) or isinstance(v, str)` → `isinstance(v, (bool, str))`
4. **routers/pages.py**: tab_page / feature_page で重複していた `active_tab / features / feature_labels` 構築を `_tab_context(tab)` に集約
5. **routers/machine.py**: 非エンドポイントの `build_state_response` の引数注釈を `StateDep/SettingsDep`（Depends 付き Annotated）から素の `AppState/Settings` へ（誤解を招く DI メタデータの除去。実行時挙動不変）
6. **routers/system.py + machine_control.py**: Klipper 生成 5 行の重複を `system.create_klipper(state, timeout)` に共有（machine_control は既に `fetch_status` を system から import しており新たな依存方向なし）
7. **templates/settings.html**: machine / motion 2 フォームのテーブル行レンダリング重複を Jinja マクロ `settings_table(fields, prefix)` に統合（motion 側にも str 分岐が付くが motion に str 項目は無く無害）

## 変更しなかった点（判断）

- `machine_control.py` の `except BusyError: raise` — BusyError は RuntimeError 派生のため後続の 502 変換に飲まれないよう必須
- `state.py` の `_resolve_pcb`（1 箇所使用の薄いラッパ）— `_resolve_machine` と対称で意図が明瞭なため維持
- `settings.js` の 2 フォームハンドラ — restart 確認・結果トースト分岐が異なり、パラメータ化はかえって複雑化
- `app.js` / `machine_control.js` / CSS / その他テンプレート — 重複・冗長なし
- `pcbasm/hal/klipper.py` の timeout 追加分 — 既に最小（引数 1 個のパススルー）

## 検証

- `uv run pyright src/` — 0 errors
- `uv run pytest tests/webui -m "not hardware" -q` — **91 passed**（5 deselected = hardware）
- `uv run pre-commit run --files <変更ファイル>` — 全フック pass（ruff-format が config_store.py を 1 回整形 → 再実行で全 pass）
- コミットは未実施（main が実施）
