# カメラ歪み補正 Phase 3（WebUI + データ）

計画書: `/home/gop/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
ブランチ: `feature/20260728/camera-distortion`
前提: Phase 1 `camera-distortion-core.md` / Phase 2 `camera-distortion-scan.md`

## 実装したファイル

| ファイル | 扱い |
|---|---|
| `src/webui/jobs/posctrl.py` | `camera_calibration` を全面置換（params 2 個・多視点スキャン） |
| `src/webui/jobs/context.py` | `JobContext.open_camera(*, raw=False)` |
| `src/webui/state.py` | `frame_hub()` に `load_undistorter` を配線 |
| `src/webui/templates/posctrl/camera_calibration.html` | `preview_overlay = "crosshair"` |
| `src/webui/static/js/camera_calibration.js` | `square_size` ハードコード撤去 |
| `src/pcbasm/vision/calibration.py` | **`measure_pixel_per_mm` を追加**（下記 1） |
| `src/pcbasm/vision/__init__.py` | 同 re-export |
| `data/config-templates/kurousagi.paste/machine.toml` | `[camera.crop]` 300 → 600 |
| `data/config-templates/README.md` | intrinsics / 旧 JSON 再校正の記述を追記 |
| `data/testing/config/ov9281_test_fixture.json` | 新スキーマで書き直し |

## 計画から変えた点

### 1. `measure_pixel_per_mm(view, square_size_mm)` を pcbasm に追加（IF 追加）

**計画の欠落**。フロー [4] の `ScanGrid.plan(pixel_per_mm=...)` に渡す値の出所が
計画・Phase 1/2 ノートのどこにも無い（Phase 2 ノートは `<計画用ショットの ppm>` と
プレースホルダのまま）。単視点から ppm を出す公開 API は存在せず、
`IntrinsicsCalibrator` は 4 視点以上を要求する。

WebUI 側で `extent / ((cols-1) * square_size)` を書くのは薄ラッパー原則違反
（幾何計算の複製）なので、`calibration.py` に公開関数として置いた。

```python
def measure_pixel_per_mm(view: CheckerboardView, square_size_mm: float) -> float
```

既存の private `_fit_view_scale` をそのまま使う（相似変換フィット）。あわせて
`IntrinsicsCalibrator._object_points` と重複していた盤座標生成を
`_object_points_mm(pattern_size, square_size_mm) -> (N,2) float64` に一本化した
（`solve` の `object_2d` もこれを使う。値は不変）。

実測: 合成カメラ（真値 ppm=30.31、barrel k1=-0.12）の生コーナーで **30.216**
（誤差 0.3%）。歪み補正前なので厳密ではないが、格子の移動幅導出には十分。

### 2. `raw` を通したのは `JobContext.open_camera` だけ

指示は「`JobContext.open_camera` → `JobBridge.hold_camera` → `PreviewService.hold_camera`
→ `hub.subscribe(raw=)`」だったが、`hold_camera` は **`FrameHub` を yield する**
（`Camera` ではない）。`raw` を解釈できるのは `hub.subscribe()` を呼ぶ
`open_camera` だけで、`hold_camera(raw=...)` は使われない引数になる（原則 2）。

```python
# src/webui/jobs/context.py
@contextlib.contextmanager
def open_camera(self, *, raw: bool = False) -> Iterator[Camera]:
    with self._bridge.hold_camera() as hub:
        yield hub.subscribe(raw=raw)
```

`JobBridge` Protocol / `JobManager.hold_camera` / `PreviewService.hold_camera` は無変更。
spec-test-author の `tests/webui/jobs/test_context.py:378` も `ctx.open_camera(raw=True)`
だけをピンしているので齟齬なし。

### 3. 計画用ショットの `stage_position` は `Point2d(0.0, 0.0)`

フロー [2]（計画用ショット）は [3]（Klipper 接続・`start` 取得）より**前**なので、
撮影時点でステージ位置を知らない。この視点は校正データに使わない（格子に中心が
含まれる）ため任意の値でよく、`(0,0)` を渡してコメントで明示した。

### 4. ログの「ステップ」は出していない

計画 [4] は「log に移動幅・ステップ・想定コーナー被覆半径を出す」としているが、
`ScanGrid` は step を公開していない。`span_mm / (columns - 1)` を WebUI で計算するのは
薄ラッパー境界の内側に幾何を持ち込むことになるので、**移動幅・点数・被覆半径**の
3 つを出すに留めた。step が実機で必要なら `ScanGrid` に `step_mm` を足すのが正しい。

### 5. `usable_crop_side_px` は `residual_limit` を渡して summary + log に出す

`CalibrationResult` 側でクランプ済み（Phase 2 の裁定）なので WebUI は `min()` を
取らない。合成データでは 720（フレーム高でクランプ）を返す＝crop 600 が安全である
ことを数値で示せる。

### 6. `scan_verification.json` の組み立て

`CalibrationQuality` 単体の unstructure API が無いので `result.to_dict()["quality"]`
を使う。格子情報（columns / rows / point_count / span_mm / max_corner_radius_px）と
`effective_view_count` / `failed_view_indices` を添える。

## 薄ラッパー境界の自己評価

`camera_calibration` は 5 関数・非空行 198 行（本体 `_run_camera_calibration` は 91 行）。
計画の想定 80〜100 行は「ジョブ本体」の見積りと解釈すれば範囲内。

**書いていないもの**（すべて pcbasm 側）: 格子計算 / 最小二乗フィット / 半径バケット /
µm 換算 / `usable_crop_side_px` / レポート文言（`quality.summary_lines()` を 1 行ずつ
`ctx.log` に流すだけ）/ 描画（`render_scan_residuals` / `draw_scan_coverage`）。

**書いたもの**: params 読み出し、prompt/progress/log/frame/checkpoint、pcbasm API の
呼び出し順、artifacts の保存とファイル名、`JobResult` / `ApplyPayload` の組み立て、
前提チェック（homed_axes・`ScanGrid.plan` の None・`MINIMUM_SCAN_VIEWS`・
`residual_limit` ゲート）のエラーメッセージ。

## 検証結果

- `make format`: **pass**
- `make type`: **0 errors**（既知 2 件は両方解消。`CheckerboardCalibrator` import の撤去が
  こちら、`test_posctrl.py:266` は spec-test-author 側で解消済み）
- `make test-no-hardware`: **1701 passed**（`tests/pcbasm` 1057 を壊していない）
- `uv run pytest tests/webui -m "not hardware and not e2e"`: **642 passed**
- `make test-e2e`: **51 passed**
- `grep -rn '</content>' src tests data`: 空
- `make test` / `pytest -m hardware` は実行していない

### 装置なしで通した pcbasm 呼び出し列（合成カメラ）

`SyntheticCheckerboardCamera` + mock stage/klipper でジョブと同じ順に呼んだ実測:

```
pattern (11,8) / 計画用ショット ppm 30.216
格子 5x3 = 15点 / span 22.78 x 8.73mm
scan → 15 views / 0 failures / 開始位置へ復帰
solve → ppm 30.3137（真値 30.31）
残差 RMS 119.1um → 4.1um（最大 383.9 → 9.5）
帯別 前 72.6/93.3/123.9/176.1 → 後 3.9/4.0/4.2/4.1um
usable_crop_side_px(30) = 720
```

`residuals.png` / `corner_coverage.png` / `scan_verification.json` も生成を確認。

### `ov9281_test_fixture.json`

`CalibrationResult.load()` で読めることを確認（`ppm=40.0` / `z=-25.0` / `res=(1280,720)`）。
`load_undistorter(p, (1280,720))` → `Undistorter`、`(400,400)` → warning + None。
`crop_size` は削除。`intrinsics.distortion` は全ゼロ（恒等写像）。

## ユーザーに見てほしい点（実機）

1. `camera_calibration` ページを開いた時点で**十字線と関心領域の矩形**が出ていること
2. スキャン 15 点が完走し、`residuals.png` の下段折れ線で周辺帯の残差が下がること
3. `usable_crop_side_px` が 600 以上を返すこと（返さない場合は crop 600 に上げない）
4. Apply → 設定ページで `camera.crop` を 600 に変更（ジョブは書かない）
