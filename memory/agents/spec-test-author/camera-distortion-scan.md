# camera-distortion-scan（Phase 2 テスト）

計画書: `/home/gop/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
ブランチ: `feature/20260728/camera-distortion`
前段: `memory/agents/spec-test-author/camera-distortion-core.md`（Phase 1）

担当は `tests/` のみ。`src/` は `plan-implementer`（scan-impl）が並行実装。

## 書いたもの

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/posctrl/test_checkerboard_scan.py`（新規） | `TestCheckerboardScanner` 8 ケース + fake stage 位置モデル `_StageModel` + `_BlindCamera` |
| `tests/pcbasm/vision/test_calibration.py`（追記・再編） | `TestUsableCropSide` 5 / `TestUndistortViews` 3 / `TestResidualField` 4 を追加。`TestCalibrationQuality` は `summary_lines` 専用に縮小し、`_quality` / `_result_with` を module 関数へ引き上げ（計 61 ケース） |
| `tests/pcbasm/vision/test_overlay.py`（追記） | `TestDrawScanCoverage` 3 ケース（計 10） |
| `tests/pcbasm/test_visualization.py`（追記） | `TestResidualRender` 2 ケース + `_scan_quality` ヘルパ |

### `TestCheckerboardScanner` のケース

1. `test_visits_every_grid_point_in_absolute_coordinates` — 15 点で `views` 15 件、
   `stage.move` の XY が `start + grid.positions[i]`、`view.stage_position` も同じ絶対 XY
2. `test_scan_moves_do_not_touch_z` — 巡回移動で `z` を渡さない（フォーカス Z を壊さない）
3. `test_returns_to_the_start_position_after_the_scan` — 最後に絶対 x/y/z で開始位置へ復帰、
   移動回数 = 点数 + 1
4. `test_each_move_is_sent_once_with_settle_and_wait_for_done` — 1 移動 = 1 `send_gcode` で
   `G4 P500` と `M400` を含む（`OffsetTransformMeasurer._move_to` と同形）
5. `test_on_view_is_called_for_every_point_with_progress` — `index` 0..14 / `total` / `raw` /
   `annotated`（`raw` と異なる画像）
6. `test_undetectable_points_are_recorded_and_the_scan_continues` — 2 点だけ `failures`、
   `on_view` は 15 回呼ばれ失敗点の `annotated` が None、移動回数は不変
7. `test_on_view_exception_propagates_and_still_returns_to_start` — 例外伝播 + finally 復帰
8. `test_detection_uses_only_the_given_pattern_size` — 盤と違う `pattern_size` を渡すと
   1 点も検出できない（= 総当たりフォールバックが無いことの behavioral な証明。速度契約）

## fake の構成

- `Klipper` / `XYZStage` は `mocker.Mock`（自前 HAL。先例 `test_offset.py:8`）
- `_StageModel.move(x, y, z, *, speed, relative)` を `stage.move.side_effect` に、
  `lambda: model.position` を `stage.get_position.side_effect` に割り当てる。
  **G-code 文字列は読まず、`stage.move` の呼び出し引数だけで位置モデルを更新する**
- 合成カメラには `model.view_offset()`（= 現在位置 − 開始位置）を渡す。
  `SyntheticCheckerboardCamera` はボードを機械原点に置くため、
  「ボードは開始位置に置かれている」というモデルにして開始位置を原点から離せる
  （`START = (12.0, -34.0, -5.5)`）。これで絶対移動と相対移動を区別できる
- `_BlindCamera` は指定撮像回だけ無地フレームを返す `Camera` ラッパー
  （先例 `test_framehub.py` の `GatedPatternCamera`）

### 軽量化

制御フロー検証に精度は要らないので、`480x360` / 内部コーナー `7x5` / `ppm=20` /
`supersample=4` にした。15 点スキャンが 0.08 秒（Phase 1 の 1280x720・supersample=8 は
15 点 0.8 秒）。ファイル全体で 1.0 秒。歪み復元の精度検証は
`test_calibration.py::TestDistortionRecovery` に任せ、Phase 2 で重複させていない。

## 実装側と合意した解釈（scan-impl に確認済み）

計画 §5 は「相対移動 + 開始位置復帰」と書いてあるが、orchestrator の裁定どおり
**巡回は絶対座標**で実装される。scan-impl から確定回答を得た内容:

- `CheckerboardScanner(klipper, stage, camera, *, pattern_size, settle_time=0.5, speed_ratio=0.5)`
- 巡回移動は `x` / `y` / `speed` のみ（`relative` を渡さない・`z` を渡さない）。
  復帰移動だけ `x, y, z` の 3 軸
- `ScanProgress.index` は **0 始まり**（`grid.positions` のインデックス）、`total = 点数`
- `ScanProgress` / `ScanFailure` / `ScanOutcome` は `attrs.frozen(eq=False)` なので
  値等価で比較せずフィールド単位で見る
- `draw_scan_coverage(image_size, views, crop_sizes=())` / `render_scan_residuals(quality,
  before_views, after_views, output_path)` はいずれも全引数 positional 可
- `residual_field` の戻りは `(corners, residuals)`。ともに `(N,2)` px で
  **N = (視点数 − 1) × 1 視点コーナー数**（基準視点 `views[0]` の行は無い）
- `usable_crop_side_px` は `CalibrationResult` のみが持ち `CalibrationQuality` から消える

## 追随した既存テスト

`usable_crop_side_px` の移設に伴い、`TestCalibrationQuality` の crop 系 2 ケースを
`TestUsableCropSide` へ移し `CalibrationResult` 経由に変えた。加えて**解像度クランプを
2 ケースでピン**した:

- `test_side_never_exceeds_the_shorter_frame_edge` — 上限を無限大にすると素の式は
  905px（最外帯 640px → `floor(2r/√2)`）だが `min(RESOLUTION) == 720` を返す（不具合の回帰）
- `test_side_is_not_clamped_when_the_frame_is_large_enough` — 解像度だけ `(2000,2000)` に
  した対照でクランプが常時効いているわけではないことを示す
- `test_quality_does_not_own_the_crop_side` — `CalibrationQuality` に残っていない
  （残すと解像度を知らずクランプできず二重管理になる）

## 実装側と食い違う可能性がある解釈

下記 1 / 3 と「`G4 P500` の文字列一致」は scan-impl に照会し、**いずれも変更予定なし**の
確認を得た（1 移動 = 1 `send_gcode` は計画§5 の設計契約そのもの、配色は既存
`draw_overlay` / `draw_crosshair` 準拠、総当たりフォールバックは速度契約なので足さない）。
それでも実装詳細に寄せたアサートである点は変わらないので、下記に残す。

1. **`draw_scan_coverage` の色でチャネルを識別している**。コーナー点と十字線が緑
   `(0,255,0)`、crop 矩形とラベルが `(0,255,255)` なので「赤チャネルの有無 = 矩形の有無」で
   アサートした。矩形の色を変えるとこのテストが落ちる（配色は仕様ではないので、
   変えるならテストも直す）
2. **`render_scan_residuals` は PNG の中身を検証していない**。既存 `TestHeightRender` と
   同粒度（cv2 で復号可能・400px 超）+「歪み形状が違えば図も違う」の 2 本のみ。
   図の読み取りやすさは実機の `residuals.png` で人が判断する
3. **`test_each_move_is_sent_once_with_settle_and_wait_for_done` は `G4 P500` を文字列で
   見ている**。`settle_time` を既定 0.5 で渡しているので `int(0.5*1000)` に依存する。
   `gcode.wait` の書式が変わったら要修正
4. `residual_field` の µm 換算規約（`residuals / report.pixel_per_mm * 1000`）を
   `rms_um` / `max_um` との一致で `rel=1e-6` にピンした。集計方法（ノルムの RMS）を
   変えるならここが落ちる

## 実行結果

```
uv run pytest tests/pcbasm -m "not hardware"  → 1057 passed（Phase 1 は 1034）
pyright（担当 4 ファイル）                     → 0 errors
make format                                     → 通る
grep -rn '</content>' tests/                     → 空
```

`src/` の Phase 2 実装（`checkerboard_scan.py` / `undistort_views` / `residual_field` /
`usable_crop_side_px` 移設 / `draw_scan_coverage` / `residual_render.py`）が先に完了して
いたため、**未実装由来の失敗はゼロ**。追加分の実行時間は
`test_checkerboard_scan.py` 約 1.0 秒、`TestResidualRender` 約 1.7 秒、その他 0.1 秒未満。

## 閾値の読み方（scan-impl と合意）

scan-impl のスキャン層を通した通し実測（118.7µm → 4.4µm、ppm 30.315）は Phase 1 の
`TestDistortionRecovery` 実測（119µm → 4.24µm）と一致する。`solve` が使うのは
`outcome.views` のコーナーだけで、スキャン層が介在してもレンダリング +
`cornerSubPix` の雑音以外は値が変わらないため。**ここがずれ始めたら疑うのは閾値では
なく検出精度の側**（`SyntheticCheckerboardCamera` の `supersample` / `cornerSubPix`）。

## 担当外（Phase 3）

`tests/webui/**`（`jobs/test_posctrl.py` / `routers/test_pages.py`）。
`make test-no-hardware` 全体は `src/webui/jobs/posctrl.py` の Phase 3 実装待ち。
