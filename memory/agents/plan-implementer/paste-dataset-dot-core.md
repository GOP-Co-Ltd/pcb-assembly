# paste-dataset-dot-core (MR1) 実装ノート

計画書: `/home/gop/.claude/plans/docs-image-based-dispense-calibration-m-generic-frog.md`
（MR1 節 + 末尾「設計レビュー反映（確定・上記より優先）」節）

## 判断ログ

### 1. `applicator.retract()` をジョブ先頭に入れない（must-fix 10 の結論）

`fill_sequence.py` を読んだ結論。`FillSequence.to_gcode` は
`retract_amount + prime_extra + total_amount` を押し出したうえで、塗布移動が速度 0 に
なった時点から `retract_amount` を引き戻す。したがって 1 シーケンスの正味送り量は
`total_amount + prime_extra` で、**開始状態も終了状態も「retract 済み（ノズル先に
retract_amount 分の空隙がある）」で自己完結している**。

dataset 収集は手動ローディングを挟まない（直前は吐出量キャリブレーションで、これも
`FillSequence` で終わる）ので、開始時点で既に retract 済み。ここでさらに `retract()`
すると空隙が 2 倍になり、先頭の数点が痩せる。

一方 `paste_solder` / `toolhead_offset` / `flowcalib` は直前に手動ローディング
（プライム）を挟む経路を持つため、先頭 retract は「プライム済みペーストを baseline
まで引き戻す」ために必要（`flowcalib/procedure.py:draw_lines` の docstring が明記）。

→ dataset 収集では **retract しない**。プライム状態のずれは最初のパージが吸収する。
理由は `_run_paste_dataset_collection` の docstring と要件書へ記載した。

### 2. `sweep_schedule` の移設（must-fix 2）

`pcbasm/pasting/sweep.py` を新設して移した。`flowcalib/lines.py` と
`dataset/plan.py` はそこから import する。

ただし `flowcalib/flow.py` には **re-export を 1 行残した**
（`from pcbasm.pasting.sweep import sweep_schedule`）。理由: 既存の
`tests/pcbasm/pasting/flowcalib/test_flow.py` が `flow` から import しており、この MR で
`tests/pcbasm/**` は編集範囲外（spec-test-author 担当）。re-export はコメントで意図を明記。

### 3. `DatasetRunInfo` に plate / seed / セル設定を持たせない（計画からの逸脱）

MR1 節は「`DatasetRunInfo` に `plate` と点塗布設定を入れる」と書いていたが、銅板寸法・
外周余白・セル格子・量スイープ・blank 数・seed は `DotGridPlan.spec`（`DotGridSpec`）が
唯一の出典で、recorder は plan を保持している。二重に持たせると不整合の余地ができるので
`DatasetRunInfo` は plan が知らない値だけを持つ:
`paste_height_mm` / `height_plane_z_mm` / `view_count` / `view_offset_mm` /
`crop_size_px` / 機体・ペースト・started_at・dispenser・calibration。

### 4. `MIN_COMMANDED_ROTATIONS = 0.1 rev`（要ユーザー確認）

must-fix 7 の「`volume_min_ul * rotations_per_ul` が最小回転数を下回らないか」検証のための
しきい値。コードベースに既存の「最小指令回転数」概念が無かったため、`plan.py` に定数として
置いた。値 0.1 rev は暫定。実機のステッパー分解能・オーガー特性から決めるべき値なので、
ユーザー確認事項として報告に含めた。

### 5. 事前検証の実施タイミング

- 装置を開く前（prompt より前）: 設定 validate / `plan_dot_grid` / `plan_views` /
  最小回転数 / crop がカメラ視野に収まるか。crop 判定は
  `CalibrationResult.load(machine.camera.calibration_file)` で calibration ファイルだけを
  読むので装置に触らない。
- `setup_board` 直後・収集の移動を始める前: stage soft limit 判定。`stage.limits` は
  Klipper の printer.cfg 由来で接続が必要、board 変換も基準点計測後にしか無いため、
  ここより前には出せない。

### 6. `dataset/procedure.py` は作らなかった

計画の「採らなかったレビュー提案」表に「dataset 側は `dataset/procedure.py` に閉じる」と
あるが、これは `CopperPlateProcedure` 共通抽出を却下する文脈の但し書きと解釈した。MR1 節の
実行順序表はジョブが `setup_board` → 高さ計測を行う形になっており、実際の配線は
`PasteSession` + `DatasetCapturer` + `plan` の 3 つで足りている。新クラスを 1 つ増やすのは
「要求されていない抽象化を追加しない」に反するので作らず、ジョブ側の薄いヘルパー
（`_measure_plate_height` / `_check_reach` / `_plate_center_z`）に留めた。必要なら
code-simplifier / MR2 で見直す。

### 7. `PasteSession.point_transform(point, *, height_plane)` の `point` は未使用

計画で確定したシグネチャ。バリアント間で入口を揃えるため引数は残し、`del point` で
未使用を明示した。MR4（任意位置パージ）で意味を持つ想定。

### 8. blank セルの型

`DotCell`（塗布する）と `DotBlank`（塗布しない）を分け、`DotTarget = DotCell | DotBlank`。
`DotGridPlan.cells` は塗布サンプルのみ、`blanks` が blank、`targets` プロパティが index 昇順の
撮影対象全体。index は両者で 1 つの採番列を共有する（画像ファイル名が衝突しない）。
metadata も `samples[]` / `blanks[]` に分ける。

## IF 変更通知（他 implementer / spec-test-author 向け）

計画書のシグネチャからの差分:

- `DotGridSpec` に `crop_size_mm=2.0` / `blank_count=4` を追加（レビュー節どおり）。
  既定は `volume_min_ul=0.05` / `volume_max_ul=0.2` / `cell_gap_mm=1.0`。
  `target_count`（= `sample_count + blank_count`）プロパティを追加。
- `DotCell` に `order: int`（塗布実行順、1 起点）を追加。
- `DotBlank` / `DotTarget` を追加。`DotGridPlan` に `blanks` と `targets` を追加。
- `plan.py` に `validate_min_rotations` / `validate_crop_in_frame` /
  `validate_capture_reach` と定数 `DEFAULT_VIEW_COUNT` / `DEFAULT_VIEW_OFFSET_MM` /
  `DEFAULT_PASTE_HEIGHT_MM` / `MIN_COMMANDED_ROTATIONS` を追加。
- `crop_pixel_size` は奇数へ寄せる。
- `PasteDatasetWriter.open(root, *, plate_name, crop_size_px, started_at=None)`。
  `write_capture` は crop が `(n, n, 3)` uint8 であることを強制する。
- `PasteDatasetRecorder.record_pre/record_post` は `DotTarget` を受ける。
  `record_execution(cell: DotCell, result)` / `record_purge_execution(result)`。
- `validate_dataset_run(*, initial_purge_ul, paste_id, paste_height_mm)`。
- `DatasetCapturer(session, *, crop_size_px, settle_time=0.5, frame_sink=None)`、
  `capture(target: DotTarget, view)`。projector は内部で
  `CopperProjector.from_calibration(session.calibration_result)` から組む。
- metadata v2 の追加キー: `plate.height_plane_z_mm` / `samples[].order` /
  `label.kind` / `blanks[]` / `config.crop_size_mm` / `config.crop_size_px` /
  `config.blank_count` / `config.capture_order`（`crop_pixel_size` は廃止）。
- `CopperProjector.from_calibration(result, *, layer=Layer.TOP)` を追加。
