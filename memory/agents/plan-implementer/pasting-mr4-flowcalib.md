# MR4 flowcalib — 実装ノート

計画書: `memory/agents/implementation-planner/pasting-mr4-flowcalib.md`。
plan-implementer（サブエージェント）が大半を実装し、サブエージェントの permission 継承バグで停止したため、
残り（テスト 3 件の修正・検証・commit）は orchestrator（メイン）が直列で引き継いだ。

## 実装したもの

- `pasting/flowcalib/params.py`: `CalibrationParams`（`from_mapping` で int 系を `max(1, int(v))` 正規化、
  `required_line_count`、`line_layout`）、`LAYOUT_MARGIN_MM`。web の `DISPENSE_CALIBRATION_DEFAULT_*` 13 定数を置換
- `pasting/flowcalib/flow.py`: `FlowCalibration`（旧 `MassFlowCalibration` / `FlowCalibration` / `FlowCalibrationSet` を統合、
  `density_mg_per_ul`）、`MassFlowEstimate` / `estimate_mass_flow`、`sweep_schedule`（旧 `dispense_rate_schedule` /
  `fill_speed_schedule` を 1 本に）、`slot_area`、`rate_sweep_amount_ul`、`speed_sweep_amount_ul`（web のインライン式を回収）、
  `RateMeasurement` / `DispenseRateCalibration`（raise 廃止・None 返却）、`RotationsPerUlRound` / `rotations_per_ul_round`、
  `CONVERGENCE_REL_TOL`
- `pasting/flowcalib/lines.py`: `LineLayout`（`fits`、`__attrs_post_init__` の raise と `LineLayoutOverflowError` を廃止）、
  `validate_line_layout`（超過文言を core が返す）、`RateSweepPoint` / `plan_rate_sweep`、`SpeedSweepPoint` / `SpeedSweep` /
  `plan_speed_sweep`
- `pasting/flowcalib/procedure.py`: `FlowCalibrationProcedure(session, transform)` + `setup(result)`、context manager で
  applicator を enable/disable、`move_to_loading_z` / `removal_z` / `move_to_removal_z` / `draw_lines` / `adopt`
- `paste_flow_calibration_board/` → `flowcalib/board/`（`config` / `catalog` / `layout` / `generator`）。`PasteFlowCalibration`
  接頭辞を全削除（`BoardConfig`, `BoardSpec`, `PurgePadSpec`, `PatternSpec`, `CustomPad*`, `PadPattern`, `PadCatalog`,
  `BoardLayout`, `PadLayout`, `PatternLayout`, `LayerPolygon`, `BoardGenerator`, `BoardPreview`, `ResolvedConfig`,
  `PatternAddition`, `BOARD_KIND`, `BOARD_SCHEMA_VERSION`, `CUSTOM_PAD_SHAPES`）。`build_board_layout` は `(layout | None, error)`。
  例外は `BoardConfigError` のみ。document JSON は schema v1 のままキー・値不変
- web `jobs/pasting/dispense_calibration.py`（922 → 726 行）: `_CalibrationContext` / `_removal_z` / `_move_to_removal_z` /
  `_build_line_layout` / `_layout_overflow_message` / `_layout_for_sweep` / `DEFAULT_*` を削除し、`FlowCalibrationProcedure` と
  `flowcalib.flow` / `lines` を呼ぶ。メニューループ・tare / 質量 prompt・採用/再計測の状態機械・machine.toml 反映は web に残す
- web `routers/pasting_loading.py`, `routers/paste_flow_calibration_board.py`, `app.py`, `dependencies.py`,
  `docs/image-based-dispense-calibration.md` の import / 型名追従（router の URL・pydantic モデル・レスポンス形は不変。
  ミラー縮小は MR6）

## 計画書からの逸脱・計画外判断

- `FlowCalibrationProcedure.__init__(session, transform)` を公開 ctor にし、`setup(result)` を classmethod にした
  （unit テストで `BoardCalibrationResult` 無しに FakeKlipper + 実 HAL で組めるようにするため。計画書の「薄い内部 ctor」に相当）
- `draw_lines(..., retract: bool = True)` を追加。② の点ごと計量で直前の retract が累積しないよう続きの線は `retract=False`
  （旧 web 実装の挙動を保つため）
- `draw_lines` の `checkpoint` は `Callable[[int], None]`（線番号を渡す。web の進捗表示に必要）
- `_per_line` は `amount_ul` / `fill_speed` / `rate_cap` のスカラー・列を正規化し、長さ不一致は ValueError（invariant）
- テスト 3 件の修正（orchestrator）: `converged` 境界テストは浮動小数の丸め（`1.0 + 0.02` の相対変化が 0.02 を僅かに超える）を
  避けて `rel_tol / 2` に。`draw_lines` の送信数 assert は `with` ブロック内に移動（`__exit__` の disable 送信を数えないため）

## 検証

- `make format` / `make type`（0 errors）/ `make test-no-hardware` → 2695 passed
- e2e `tests/e2e/test_paste_flow_calibration_board_browser.py`（`-m "e2e and not hardware"`）→ 9 passed

## 未対応・MR6 送り

- web 側の名前 `paste_flow_calibration_board`（URL・ページ id・router モジュール名・テンプレート・JS）と router の
  `PasteFlowCalibration*Model`、`PasteFlowCalibrationBoardGeneratorDep` は web-facing 名として据え置き（MR6 でミラー縮小と一緒に整理）
- `docs/image-based-dispense-calibration-ml-plan.md` の旧モジュール名は MR6（docs）で更新
