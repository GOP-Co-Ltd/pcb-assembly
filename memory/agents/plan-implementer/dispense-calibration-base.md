# 吐出量キャリブレーション刷新 — pcbasm 基盤リネーム + webui リネーム追従

計画書: `/home/gop/.claude/plans/claude-webui-algorithm-practical-1-drifting-dewdrop.md`
ブランチ: `feature/20260624/dispense-calibration`
担当範囲: pcbasm 基盤（config/fill_sequence/applicator/settings/session）+ configs 4ファイル
+ webui リネーム追従（config_store/routers.pasting の ResolvedSettings/pad_editor model.js/jobs.pasting の1行）
+ 上記に紐づくテスト。

## 実装した公開 IF（他 agent との契約・厳守済み）

- `config.PasteDispenser.max_fill_speed`（旧 `fill_speed`）。属性名・コメント変更。
- `FillSequence.max_fill_speed`（旧 `fill_speed`）+ 新フィールド `rate_cap: float | None = None`
  （末尾・デフォルト None）。
  - `None` = `max_dispense_rate` で cap（**現行と数学的に同一**・既存期待値不変）
  - `float` = その値で cap
  - `math.inf` = cap 無効（移動速度のみで律速）
  - `_effective_rate()` を `min(total*max_fill_speed/length, cap)` に。点フィル（length<=0）は cap を返す。
- `PasteApplicator.__init__(max_fill_speed=...)`（旧 `fill_speed`）。`from_config` は `config.max_fill_speed` 経由。
- `PasteApplicator.draw_line(start: Point2d, end: Point2d, *, amount, paste_height=None,
   prime_extra_delay=None, bead_width_factor=None, rate_cap=None) -> Speed | None`
  - 内部で `_draw_polyline([start, end], ..., dispense_mode="line", rate_cap=...)`。
  - 戻り値は実効塗布移動速度（`fill_speed_actual()`）。経路長0等で None。
  - 塗布面積はスロット近似（既存 `_resolve_paste_height` line モードと同一式）。
- `PasteApplicator._draw_polyline(raw, *, total_amount, paste_height, dispense_mode,
   ul_per_mm2, prime_extra_delay, bead_width_factor, rate_cap=None) -> Speed | None`
  （`_fill` ループ本体を切り出し・`_fill` とキャリブが共用。塗布挙動不変）。
- `PasteApplicator.apply(...)`: `fill_speed` キーワード引数を**削除**（他キーワードは不変）。
- `session.make_applicator(transform=Identity(), *, rotations_per_ul: float | None = None)`
  - `rotations_per_ul` 明示時のみ HAL `PasteDispenser` を新値で再構成（air_pump_enabled は machine 設定踏襲）。
  - `None` のときは従来どおり `self.paste_dispenser`（machine 値）を使う。
- `settings.py`: `PASTE_OVERRIDE_FIELDS` / `NUMERIC_PASTE_OVERRIDE_FIELDS` から `fill_speed` を除去。
  `PasteOverride.fill_speed` / `ResolvedPaste.fill_speed` 属性削除・`base_override_from_config` /
  `_resolved_from_values` の fill_speed 行削除。`resolve_*`/`with_level_patch`/round-trip はタプル駆動で自動追従（確認済み）。
- webui: `config_store` の machine キー `paste_dispenser.max_fill_speed`（label「最大塗布速度」）。
  `routers/pasting.ResolvedSettings` から `fill_speed: float` 削除。`pad_editor/model.js` の
  FIELDS/FIELD_LABELS から `fill_speed` 行削除。`jobs/pasting._run_paste_solder` の `fill_speed=r.fill_speed` 行削除。

## configs（4ファイル全部・キー名のみリネーム、値は不変）

`fill_speed = X` → `max_fill_speed = X`:
- configs/kurousagi/machine.toml (0.8)
- configs/pd_china_frame/machine.toml (200.0)
- configs/test-fixture/machine.toml (0.8)
- data/testing/machine.toml (2.0)

## 計画外の判断ログ

1. **テストの fill_speed override 代表 → prime_extra_delay へ移植**（計画は「ul_per_mm2 等へ」）。
   pad override テストは「数値 override 1 項目が階層で正しく解決される」ことを見るのが本質。
   `ul_per_mm2` は既に多くのケースで独自値を持つため衝突回避に `prime_extra_delay` を採用。
   test-fixture の `prime_extra_delay = 0.0` を継承既定値とし、`_full_base()` 等の合成 base では
   `prime_extra_delay = 0.8`（旧 fill_speed 既定 0.8 と同値）に置き換え、`== 0.8` 系の継承アサートを温存。
   対象: test_settings.py / test_pasting.py / test_board_settings.py。
2. **`tests/e2e/test_paste_solder_browser.py` を計画外で修正**（計画のテスト一覧は test_webui_e2e.py のみ記載）。
   このファイルは pad-editor の代表 override field として `fill_speed` を直接使っており、
   `PASTE_OVERRIDE_FIELDS`/`model.js FIELDS` から消えると locator が崩れる＝**私の IF 変更の直接の fallout**。
   担当が明記されていないため、`fill_speed` → `prime_extra_delay` に機械置換して整合を取った（値 0.33/0.77 は不変）。
   collect-only と ruff のみ確認（実 uvicorn+playwright は親が合流時に実行）。
   ※ 他 webui agent がこのファイルを触る予定なら衝突注意（同ファイルへの編集は本変更のみ）。
3. `test_webui_e2e.py` には `fill_speed`/`paste_dispenser.fill_speed` 参照が無く、移植対象なし（計画の想定と差分）。

## 検証結果（自分の担当テストのみ・全体 make は親が合流時）

- ruff format / check: 全対象 Pass
- pyright（src 5ファイル + webui 3ファイル + 変更テスト6ファイル）: 0 errors
- 触れた全モジュールの import: OK
- pytest（in-scope 9ファイル, e2e 除く）: **272 passed**
  - test_config.py / test_fill_sequence.py(rate_cap 3モード+点フィル追加) / test_applicator.py(draw_line 6本追加)
  - test_settings.py / test_board_settings.py / test_config_store.py
  - test_pasting.py(fill_speed patch=400 の新テスト2本追加) / test_settings_api.py / test_jobs.py
- e2e（test_paste_solder_browser.py / test_webui_e2e.py）: collect-only のみ（親が `make test-e2e`）

## 並列作業で触っていないもの（契約どおり）

- `src/pcbasm/pasting/dispense_calibration.py`（新規・agent B）
- `src/pcbasm/pasting/__init__.py` の re-export（agent B）
- `webui/jobs/pasting.py` の dispense_calibration / flow_calibration 関連（agent C）
- `webui/routers/pasting.py` の `MassFlowCalibration` import・`loading/calibration` endpoint（agent C・**未変更で残置**）
- `loading.html` / `loading_controls.js`（agent C）

## 残課題 / 未解決点

- 全体 `make type` / `make test` は未実行（並列ツリー編集中のため計画指示どおり回避）。親が合流時に実行。
- `routers/pasting.py` は `MassFlowCalibration` import を残置済み（agent C が `loading/calibration`
  endpoint を削除する際に同 import も外す前提）。私の変更単独では未使用 import にはならない（endpoint が使用中）。
