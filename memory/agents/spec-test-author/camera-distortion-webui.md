# camera-distortion-webui（Phase 3 テスト）

計画書: `/home/gop/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
ブランチ: `feature/20260728/camera-distortion`
前段: `camera-distortion-core.md`（Phase 1）/ `camera-distortion-scan.md`（Phase 2）

担当は `tests/webui/` と `tests/e2e/` のみ。`src/` `data/` は `plan-implementer`（webui-impl）。

## 書いたもの

| ファイル | 変更 |
|---|---|
| `tests/webui/jobs/test_posctrl.py` | params 更新 + 新規 2 / 削除 2 / hardware 1 置換 |
| `tests/webui/routers/test_pages.py` | 新規 2 + overlay 既定値の parametrize 変更 |
| `tests/webui/test_state.py` | `TestUndistortionDegrade` 新規 4 ケース |
| `tests/webui/jobs/test_context.py` | `TestOpenCameraRawFrames` 新規 1 ケース |
| `tests/webui/routers/test_jobs.py` | `TestCameraCalibrationApplyFlow` 削除 → `TestApplyDiscard` に ApplyFile ケース 1 追加 |
| `tests/e2e/test_webui_e2e.py` | **無変更**（2 クラスがそのまま通ることを確認） |

### 追加

- `TestCatalog::test_camera_calibration_params` — `{square_size, residual_limit}`、
  default 1.5 / 30.0、`residual_limit.minimum == 0.0`、`persisted_params` 両方
- `TestCatalog::test_camera_calibration_params_are_all_optional_with_defaults` —
  `validate_params({})` が両 param の既定値を埋める（削除した
  `test_missing_square_size_uses_default_and_succeeds` の移設先）
- `TestCameraCalibrationJob::test_fails_gracefully_after_confirm_without_klipper` —
  prompt に True → 計画用ショットは通り Klipper 不通で FAILED、`record.error`、
  `apply_available is False`、M84 警告、ロック解放。`TestMachineJobsWithoutKlipper`
  と同じアサーション形
- `TestPosctrlJobPages::test_camera_calibration_preview_pane_declares_crosshair_overlay` —
  `data-overlay="crosshair"`
- `TestPosctrlJobPages::test_camera_calibration_renders_residual_limit_form_with_default` —
  `name="residual_limit"` / `value="30.0"` / `[um]`
- `test_posctrl_job_page_default_overlay` の parametrize を
  `camera_calibration: none → crosshair` に変更（ユーザー明示要求）
- `test_state.py::TestUndistortionDegrade` 4 ケース — 校正 JSON が
  **不在 / 壊れている（旧スキーマ相当）/ 解像度不一致 / 一致（対照）** の
  いずれでも `frame_hub()` が構築でき 1 フレーム取得できる。
  「旧 JSON が読めない → 映像が出ない → 校正ジョブを実行できない」デッドロック防止
- `test_context.py::TestOpenCameraRawFrames` — 歪み係数入り（k1=-0.2）の校正を
  config へ置き、`ctx.open_camera(raw=True)` の 1 フレームが **fake_camera.png と
  ビット一致**、`ctx.open_camera()` の 1 フレームは**一致しない**ことで
  `JobContext → JobBridge → PreviewService → FrameHub.subscribe(raw=)` の
  伝播をピン。補正済みフレームで再校正して元の補正を静かに失う罠を塞ぐ
- `test_jobs.py::TestApplyDiscard::test_apply_writes_payload_files_into_the_config_dir` —
  合成ジョブの `ApplyPayload(files=(ApplyFile(...),))` が config ディレクトリへ
  実ファイルとして書かれる

### 削除

| 削除したもの | 理由 |
|---|---|
| `TestCameraCalibrationJob::test_full_run_with_checkerboard_yields_apply_payload` | ステージスキャン（Klipper 必須）になり装置なしで成立しない。カバレッジは Phase 1/2 の `tests/pcbasm` と `@mark_hardware` へ移動（親からの指示どおり） |
| `TestCameraCalibrationJob::test_missing_square_size_uses_default_and_succeeds` | 同上。既定値充填の契約は `TestCatalog` の `validate_params({})` へ移設 |
| `test_jobs.py::TestCameraCalibrationApplyFlow`（クラスごと） | **親の指示に無かったが同じ理由で落ちていた**（WS 完走が Klipper 必須になり `failed`）。ただしここが `ApplyFile` → config ディレクトリ書出の**唯一のカバレッジ**だったので、`ApplyFile` を返す合成ジョブによる同等ケースを `TestApplyDiscard` に足して契約を保存した（`ApplyFile` は `camera_calibration` 以外の本番ジョブが使わないため、合成ジョブ以外の代替が無い） |

### 置換（hardware）

`test_camera_calibration_records_z_position`
→ `test_camera_calibration_scan_yields_camera_matrix_and_residual_report`。

docstring に書いた前提（ジョブがホーミングも Z 移動もしないため必須）:

1. Moonraker が localhost:7125 で稼働
2. 全軸（x / y / z）ホーミング済み（`homed_axes` チェックがある）
3. カメラのフォーカス Z へジョグ済み（その Z が `z_position` に記録される）
4. **1 マス 1.5mm・12x9 マス（内部コーナー 11x8）**のチェッカーボードが
   ステージにあり、マス目が画像中央の十字線に合っている
5. 現在位置の周囲 **±11.3mm(X) / ±4.4mm(Y)** が可動域内

アサーション: SUCCEEDED / artifacts に `scan_verification.json` と `residuals.png` /
`z_position is not None` / K が使い物になる（fx,fy > 0・主点が視野内・歪み非ゼロ）/
`quality.after.rms_um < quality.before.rms_um`。

## `make type` の既知エラー 2 件目の解消方法

`tests/webui/jobs/test_posctrl.py:266` の `loaded.crop_size`（削除済み属性）は、
その行を含む `test_full_run_with_checkerboard_yields_apply_payload` を**テストごと
削除**したことで消えた（`crop_size` を別の値に読み替えるのではなく、その
アサーションが属していたテスト自体が装置なしでは成立しなくなったため）。
`make type` は現在 **0 errors, 0 warnings**。

## 実装側と食い違う可能性がある解釈

1. **hardware テストの校正 JSON の特定方法を `apply_payload()` 経由にした**。
   `scan_verification.json` が追加されて `kind == "file"` の artifact が複数に
   なったため、旧テストの `next(a for a in artifacts if a.kind == "file")` は
   曖昧になる。`payload.values["camera.calibration_file"]` と basename が一致する
   artifact を選ぶ形にしたので、**採用候補 JSON が artifacts にも含まれること**を
   前提にしている（現行実装はそうなっている）。
2. **`scan_verification.json` / `residuals.png` のファイル名を basename で
   ピンしている**。計画 §8 の表の名前をそのまま採った。`corner_coverage.png` は
   アサートしていない（計画の表にはあるが親の指示のアサーション一覧に無い）。
3. **`residual_limit` の label 文言はアサートしていない**。単位 `[um]` の描画だけ
   見ている（文言は仕様ではない）。
4. **overlay 既定値のピンをラジオと `data-overlay` の 2 本に分けた**。片方だけでは
   初期表示に反映されない（preview.js が `data-overlay` からクエリを組む）ため。
5. **`TestUndistortionDegrade` は warning ログの内容をアサートしていない**。
   `load_undistorter` の warning 1 行は `tests/pcbasm/vision/test_calibration.py`
   側でピン済みなので webui では重複させず、「映像が流れる」振る舞いだけを見た。
6. `test_context.py` の歪み校正 fixture は `k1=-0.2` の 1 パラメータ。値そのものは
   仕様ではなく「補正の有無が画素差として観測できる大きさ」という要件のみ。

## 実行結果

```
uv run pytest tests/webui -m "not hardware and not e2e"  → 643 passed
make test-no-hardware                                     → 1702 passed
make test-e2e                                             → 51 passed
make type                                                 → 0 errors
make format                                               → 通る
grep -rn '</content>' src tests data                       → 空
```

**未実装由来の失敗はゼロ**。webui-impl が Phase 3 の `src/webui/` と
`data/testing/config/ov9281_test_fixture.json` を先に完了していたため、
書いた時点から全て緑だった（当初 1 件だけ落ちていたのは上記の
`TestCameraCalibrationApplyFlow` で、原因は未実装ではなく
「Klipper 必須化により装置なしでは成立しなくなった旧テスト」）。

`ov9281_test_fixture.json` を読む既存テスト（`test_state.py::TestMachineConfig`
の `focus_z` 3 ケース、`test_config_store.py:181`、
`routers/test_machine_control.py:50`、e2e）は**すべて無変更で通った**。
ゼロ歪み fixture では補正が恒等写像になるという計画 §10 の予測どおり。

なお `fake_camera.png` は 1280x720 で fixture の解像度と**一致する**ため、
fake camera 経路は「解像度不一致 → 補正なし」ではなく
**「解像度一致 + 歪み全ゼロ → 恒等写像の Undistorter を実際に通る」**経路になる
（計画 §4 の記述は 400x400 の `checkerboard.png` を返す経路の話）。
両方を `TestUndistortionDegrade` で明示的に押さえた。
