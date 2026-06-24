# ペーストローディング 質量キャリブレーションのテスト更新

計画書: `/home/gop/.claude/plans/claude-webui-rotations-per-ul-greedy-journal.md`

## 書いたテスト一覧

### A. tests/pcbasm/pasting/test_calibration.py（unit）
- 削除: `TestTrapezoidalRotationProfile` クラス全体、`TrapezoidalRotationProfile` import、
  `test_from_trapezoidal_profile_uses_plateau_rotations`、`test_from_short_trapezoidal_profile_raises`
- 維持: `test_rotations_per_ul_from_mass_and_density`、`test_values_must_be_positive`、
  `FlowCalibration`/`FlowCalibrationSet` 系すべて
- 追加:
  - `TestMassFlowCalibration::test_dispense_rate_for` — 正常系。rotations=5/mass=10/density=3.78
    → rpu=1.89、`dispense_rate_for(0.5)==approx(0.5/1.89)`
  - `TestMassFlowCalibration::test_dispense_accel_for` — 正常系。同条件で
    `dispense_accel_for(0.5)==approx(0.5/1.89)`

### B. tests/webui/routers/test_pasting.py（integration TestClient）
新クラス `TestLoadingCalibration`（PCB 選択不要。machine 設定の `solder_paste_density` のみ
参照するため素の `client` fixture を使用。density は test-fixture で 3.78）:
- `test_all_positive_inputs_return_full_result` — 正常系。mass=10/rotations=5/rate=0.5/accel=0.5
  → volume_ul=approx(10/3.78)、rotations_per_ul=approx(1.89)、max_dispense_rate=approx(0.5/1.89)、
  dispense_accel=approx(0.5/1.89)、status 200
- `test_zero_rotations_nulls_rotation_derived_values` — 異常系。rotations=0 → rpu/rate/accel が
  None、volume_ul は非 None（mass>0）
- `test_zero_mass_nulls_everything` — 異常系。mass=0 → 4 値すべて None
- `test_zero_rate_nulls_only_dispense_rate` — エッジ。rate=0（他正）→ max_dispense_rate のみ None、
  volume_ul/rpu/dispense_accel は値あり
- `test_all_params_omitted_returns_all_null` — エッジ。クエリ全省略（各 0.0 既定）→ 4 値すべて None

### C. tests/e2e/test_webui_e2e.py（e2e、実ブラウザ）
`TestLoadingOverBrowser::test_mass_calibration_calculates_and_applies_dispense_values`
（旧 `_rotations_per_ul` からリネーム）に全面更新:
- `from playwright.sync_api import expect` を追加
- 算出値は debounce GET で非同期に届くため、`inner_text()==` 即時アサートを
  `expect(locator).to_have_text(...)` の自動待機に変更（flaky 回避）。`#lc-effective-rotations`
  アサートは削除
- 入力 amount=0.2/rotations=5/rate=0.5/accel=0.5/mass=10 →
  `#lc-volume-ul`="2.645503"、`#lc-rotations-per-ul`="1.890000"、`#lc-dispense-rate`="0.264550"、
  `#lc-dispense-accel`="0.264550"（toFixed(6)）
- `#param-amount/rotations/rate/accel` の hidden 同期アサートは維持
- 個別適用: `#lc-apply-rotations-per-ul` クリック → `/api/settings/machine` を REST ポーリングで
  `paste_dispenser.rotations_per_ul==1.89` を確認、`#lc-current-rotations-per-ul`="1.890000" を待つ
- 一括適用: `#lc-apply-all` クリック → 3 キー（1.89 / 0.26455 / 0.26455）の永続化と
  `#lc-current-dispense-rate`/`#lc-current-dispense-accel`="0.264550" 更新を確認
  （保存値は `Number(toFixed(6))` トリム後 = 0.26455）
- 無効化 tail: `#lc-mass-mg`="0" → 4 出力が "-"、4 適用ボタン（rotations-per-ul/dispense-rate/
  dispense-accel/all）が `to_be_disabled()`。`is_disabled()` 即時判定ではなく自動待機にした
  （無効化も debounce GET 後に反映されるため）
- ヘルパ `_wait_machine_field(base_url, key, expected)` を追加（machine 設定フィールドの REST ポーリング）

### D. tests/webui/routers/test_pages.py（計画は「触らない」だったが 1 行のみ修正）
- `test_loading_page_renders_rotation_controls_and_mass_calibration` から
  `assert 'data-density="3.78"' in text` を **削除**（下記「計画書との差分」参照）

## 仕様根拠の対応表
- A test_dispense_rate_for/accel_for → 計画書 確定 IF「`dispense_rate_for(r)=r/rotations_per_ul`、
  `dispense_accel_for(a)=a/rotations_per_ul`」
- B 5 テスト → 計画書 §3 エンドポイント `GET /api/pasting/loading/calibration`、非正入力は該当値 null・
  500 にしない（refactor-conventions「バリデーションは None 返却」）、density はサーバ側 3.78
- C → 計画書 テスト節 C・テンプレート id 群・「値は非同期取得なので自動待機/ポーリングに変更」

## 検証結果
- `uv run pytest --collect-only`（A/B/C）: **70 tests collected**（収集 OK）
- A test_calibration.py: **24 passed**（実装側 calibration が既に計画通り = 緑）
- B TestLoadingCalibration: **5 passed**（エンドポイントが実装済み = 緑）
- D test_pages.py: **66 passed**（data-density 削除後）
- C E2E: collect-only のみ確認（実 uvicorn + Playwright が必要なため、計画どおり親が合流時に
  `make test-e2e` で実行。テンプレ/JS は既に新 id 実装済みなので緑になる見込み）
- pre-commit（ruff / ruff-format / docformatter）: 全 **Pass**

## 期待される失敗 / 実装側に求める修正（plan-implementer 向け）

> 実装は plan-implementer が並列で既にほぼ完了しており、A/B/D は緑。残課題は 1 点のみ。

### 残オーファン: src/pcbasm/pasting/__init__.py:55 の `__all__` 残骸
`src/pcbasm/pasting/calibration.py` から `TrapezoidalRotationProfile` は削除済みだが、
`src/pcbasm/pasting/__init__.py` の import（行付近）と `__all__`（55 行）に
`"TrapezoidalRotationProfile"` が**残っている**。
- これは計画書 §2「`TrapezoidalRotationProfile` の import と `__all__` を削除」未完。
- `tests/` からは触れない（src 領分）。**plan-implementer に削除を依頼**。
  放置すると import エラーまたは `__all__` 不整合になる。

## 計画書との差分（planner にフィードバック）
計画書テスト節 D は「test_pages.py は id 維持により**変更不要・触らない**」としていたが、
§5 で「JS が密度を使わなくなるため `data-density` data 属性は削除可」とテンプレートから
`data-density` を消す方針が示されている。両者は矛盾し、`data-density` を消すと
`test_pages.py:374 assert 'data-density="3.78"' in text` が fail する。
- 仕様根拠（§5）が明確なため、spec-test-author 判断で当該 1 行を削除（テンプレ実装と整合）。
- 同様に `#lc-effective-rotations` 廃止だが test_pages.py には参照なし（影響なし）。

## tests/helpers.py への追加
なし（実 fixture（client/TestClient、browser_page、live_server）のみ使用。3rd-party モックなし）。
