# code-simplifier: load-cell-probe 簡素化メモ

## 実施内容

### 1. float_pair 型機構の全面削除（orphan 除去）

probe.shift 削除により `FieldSpec` での利用がゼロになったため、機構ごと削除。
grep で src/tests 全体に利用ゼロを確認済み（tests に float_pair 参照なし）。

- `src/webui/config_store.py` — Literal から `"float_pair"` 除去、
  `case "float_pair"` 分岐と `_coerce_float_pair` を削除、
  `MachineSettingValue` から `list[float]` を除去
- `src/webui/routers/settings_api.py` — `SettingsField.value_type` Literal から除去
- `src/webui/templates/settings.html` — float_pair 入力ペアの分岐を削除
- `src/webui/static/js/settings.js` — `pairValue` / pair 分岐 / `controlKey` を削除
  （`scheduleSave` は `control.name` を直接使用）
- `src/webui/static/app.css` — `.settings-pair` ルール 2 件を削除

### 2. height.py の `_probe_position` インライン化

shift 削除後は `board_to_machine.apply(board_pt)` を返すだけの純粋な
パススルーになっていた（2 箇所共用でも抽象として何も足していない）ため、
`probe_at` と `route_points` の呼び出し箇所へ直接インライン化しメソッドを削除。

## 簡素化しなかった項目

- `pasting/probe.py` — import（`gcode`, `GCode`）は両方使用中。docstring も適正量
- `hal/__init__.py` / `session.py` / `webui/jobs/pasting.py` — implementer の
  削除が既にクリーン。orphan import なし
- `probe_guide.html` — 既存規約（`tab.html` 継承・`<h2>{{ feature_label }}`・
  `status-card`・`mainsail_url` は base context 由来）に沿っており修正不要。
  テストがピンする substring（LOAD_CELL_CALIBRATE / SAVE_CONFIG / klipper3d.org）も維持

## 検証

- `make format` — グリーン（Literal の 1 行化で 2 files reformatted → 再実行 pass）
- `make type` — 0 errors
- `make test-no-hardware` — 1346 passed
- `</content>` 等の混入なし（grep 確認）

## IF 変更

なし。公開インターフェース（ProbeExecutor / Probe / HeightPlaneMeasurer /
WebUI ルート）はすべて維持。
