# MR4 flowcalib — orchestrator による直列レビュー

サブエージェント（code-reviewer）の permission 継承バグのため、orchestrator が旧実装（28d1351）と新実装を突き合わせてレビューした。

## verdict: approve（must-fix なし）

確認した観点:
- ① rotations_per_ul: ローディング Z=0 → run_loading_loop → retract → 線 N 本（各線前に checkpoint）→ 退避 Z → 質量入力 → 算出 → 4 択 prompt → machine.toml 反映 → adopt の順序・文言・選択肢は旧実装と同一。`commanded_rotations` = line_count × amount × rpu も同一
- ② max_dispense_rate: `retract()` 1 回 → 各点 `draw_lines(retract=False, fill_speed=max_fill_speed, rate_cap=rate)`、amount = rate × 線長 / 速度、tare / 質量 prompt 文言、効率ログ、`prompt_positive_number(default=auto)` は同一。`baseline_efficiency` が None のときの文言は旧実装が `.3f` でクラッシュし得た箇所を `"-"` 表示に修正（改善）
- ③ max_fill_speed: 総量 = ul_per_mm2 × slot_area(線長, ビード幅)、`fill_speed=v, rate_cap=inf`、選択肢と default の書式、`apply_to_machine_toml` は同一
- 高さ計測: `PasteSession.measure_height_plane` は outline を渡すため、旧 `height_measurer.measure(..., outline=)` と同等（銅板端のマージンが維持される）
- `FlowCalibrationProcedure.__enter__/__exit__` と `adopt` は旧 `_CalibrationContext.__init__`/`close`/`rebuild_applicator` と同じ enable/disable 順
- 開始時の一括レイアウト検証 `validate_line_layout(params.line_layout(params.required_line_count))` は旧 `needed_lines = max(line_count, rate_divisions, speed_divisions)` と同一

## 軽微な挙動差（許容、実機確認で注意）

1. ②③ の掃引列が空のとき、旧実装は「② をスキップします」と log して次のキャリブへ進んだが、新実装は `CalibrationCancelled` でメニューへ戻る。`divisions` は `max(1, int)` 正規化されるため列は空にならず、実運用では到達しない
2. ログのタイミング: ① の「線 i/N を塗布」は線を引く直前（旧は直後）、③ の「番号 n: 速度 …」は全線描画後にまとめて出る（旧は各線直後）。progress はどちらも各線前に更新される

## nit（MR6 へ）

- web-facing 名 `paste_flow_calibration_board`（URL / ページ id / router / テンプレート / JS）と router の `PasteFlowCalibration*Model` は据え置き。MR6 のミラー縮小で整理
