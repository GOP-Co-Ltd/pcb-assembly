# plan-implementer: ペーストローディング 質量キャリブレーション（src 実装）

計画書: `/home/gop/.claude/plans/claude-webui-rotations-per-ul-greedy-journal.md`
分担: spec-test-author が `tests/` を並列担当。本 agent は `src/` のみ。

## 実装済みファイル（7）
1. `src/pcbasm/pasting/calibration.py` — `TrapezoidalRotationProfile` クラス／`MassFlowCalibration.from_trapezoidal_profile` 削除。`MassFlowCalibration` に `dispense_rate_for` / `dispense_accel_for` 追加（`FlowCalibrationSet` と同式・同 docstring）。
2. `src/pcbasm/pasting/__init__.py` — `TrapezoidalRotationProfile` の import / `__all__` 削除。`MassFlowCalibration` は維持。
3. `src/webui/routers/pasting.py` — `MassFlowCalibration` を `from pcbasm.pasting import (...)` に追加。末尾に `LoadingCalibrationResult` モデル + `GET /api/pasting/loading/calibration` 追加。
4. `src/webui/routers/pages.py` — loading context に `current_max_dispense_rate` / `current_dispense_accel` を追加。
5. `src/webui/templates/pasting/loading.html` — section 差し替え。`#lc-effective-rotations` 行と `data-density`/`data-current-*` 属性を削除。table.loading-calibration-rows で 3 行 + 個別適用 + 一括適用。
6. `src/webui/static/js/loading_controls.js` — 薄いクライアント化。計算式を全削除し GET → 表示。`computed`/debounce/`applyValues`/`CURRENT_OUTPUT_FOR`。
7. `src/webui/static/app.css` — `.loading-calibration-rows` スタイル追加。

## IF 確定事項（spec-test-author / E2E と共有）
- エンドポイント: `GET /api/pasting/loading/calibration`（prefix `/api` 込み）。
- クエリパラメータ: `mass_mg`, `rotations`, `rate`, `accel`（すべて float、デフォルト 0.0）。密度はクライアントから送らずサーバ側 `solder_paste_density` を使用。
- レスポンス JSON キー: `volume_ul`, `rotations_per_ul`, `max_dispense_rate`, `dispense_accel`（すべて `float | None`）。
  - `volume_ul`: mass_mg>0 かつ density>0 のとき算出、それ以外 null。
  - `rotations_per_ul`: mass_mg>0 & density>0 & rotations>0 のとき算出、それ以外 null。
  - `max_dispense_rate` = `dispense_rate_for(rate)`（rate>0 のときのみ、かつ rotations_per_ul が出るときのみ）。
  - `dispense_accel` = `dispense_accel_for(accel)`（accel>0 のときのみ、かつ rotations_per_ul が出るときのみ）。
  - rotations<=0 のときは rate/accel に値があっても max_dispense_rate/dispense_accel は null（rotations_per_ul 算出が前提のため）。
- 算出値（density=3.78, mass=10, rotations=5, rate=0.5, accel=0.5）: volume_ul≈2.645503, rotations_per_ul≈1.89, max_dispense_rate≈0.264550, dispense_accel≈0.264550。検算済み。
- テンプレート id（厳密）:
  - `#lc-mass-mg`（計測質量入力）, `#lc-volume-ul`, `#lc-calibration-message`
  - `#lc-rotations-per-ul` / `#lc-current-rotations-per-ul` / `#lc-apply-rotations-per-ul` → `paste_dispenser.rotations_per_ul`
  - `#lc-dispense-rate` / `#lc-current-dispense-rate` / `#lc-apply-dispense-rate` → `paste_dispenser.max_dispense_rate`
  - `#lc-dispense-accel` / `#lc-current-dispense-accel` / `#lc-apply-dispense-accel` → `paste_dispenser.dispense_accel`
  - `#lc-apply-all`（一括適用）
- 適用 PUT は従来通り `PUT /api/settings/machine` `{values: {...}}`。値は `Number(value.toFixed(6))`。
- 表示は `toFixed(6)`。null は "-"。
- JS debounce ~250ms（GET）。E2E は同期アサート不可 → 自動待機/ポーリング推奨（計画書 C 節）。

## 計画外判断
- `lc-rotations`/`lc-rate`/`lc-accel` 入力（partials/loading_controls.html 側）は既存のローディングコマンド用入力を流用。質量キャリブレーションの GET もこの 3 入力を読む（計画通り）。
- テンプレートは計画書が「table または grid」を許容。`<table class="loading-calibration-rows">`（thead/tbody）を採用。
- `make` 系は親が合流時に実行（並列の tests/ 衝突回避）。import 健全性・計算値・JS は確認済み（node 不在のため JS は目視）。
