# カメラ歪み補正 Phase 2（スキャン層 + レポート描画）

計画書: `/home/gop/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
ブランチ: `feature/20260728/camera-distortion`
前提: Phase 1 のノート `camera-distortion-core.md`（公開シグネチャの確定形）

## 実装したファイル

| ファイル | 扱い |
|---|---|
| `src/pcbasm/posctrl/checkerboard_scan.py` | 新設。`ScanProgress` / `ScanFailure` / `ScanOutcome` / `CheckerboardScanner` |
| `src/pcbasm/posctrl/__init__.py` | 上記 4 つを re-export |
| `src/pcbasm/vision/calibration.py` | `undistort_views` / `residual_field` を公開、`usable_crop_side_px` を `CalibrationResult` へ移設 |
| `src/pcbasm/vision/overlay.py` | `draw_scan_coverage` を追加 |
| `src/pcbasm/vision/__init__.py` | `undistort_views` / `residual_field` / `draw_scan_coverage` を re-export |
| `src/pcbasm/visualization/residual_render.py` | 新設。`render_scan_residuals` |
| `src/pcbasm/visualization/__init__.py` | `render_scan_residuals` を re-export |

## 公開シグネチャの最終形

```python
# src/pcbasm/posctrl/checkerboard_scan.py
@attrs.frozen(eq=False)
class ScanProgress:
    index: int          # 0 始まり。ScanGrid.positions のインデックス
    total: int
    stage_position: Point2d   # コマンドした絶対 XY
    annotated: Image | None   # 検出成功時のみ非 None
    raw: Image

@attrs.frozen(eq=False)
class ScanFailure:
    index: int; stage_position: Point2d; raw: Image

@attrs.frozen(eq=False)
class ScanOutcome:
    views: tuple[CheckerboardView, ...]
    failures: tuple[ScanFailure, ...]

class CheckerboardScanner:
    def __init__(self, klipper: Klipper, stage: XYZStage, camera: Camera,
                 *, pattern_size: tuple[int, int],
                 settle_time: float = 0.5, speed_ratio: float = 0.5) -> None
    def scan(self, grid: ScanGrid,
             on_view: Callable[[ScanProgress], None] | None = None) -> ScanOutcome

# src/pcbasm/vision/calibration.py（追加・移設）
def undistort_views(views: Sequence[CheckerboardView],
                    intrinsics: CameraIntrinsics) -> tuple[CheckerboardView, ...]
def residual_field(views: Sequence[CheckerboardView]) -> tuple[ImageArray, ImageArray]
CalibrationResult.usable_crop_side_px(limit_um: float) -> int   # ← Quality から移設
# CalibrationQuality.usable_crop_side_px は削除。summary_lines() は無変更で Quality に残る

# src/pcbasm/vision/overlay.py
def draw_scan_coverage(image_size: tuple[int, int],
                       views: Sequence[CheckerboardView],
                       crop_sizes: Sequence[tuple[int, int]] = ()) -> Image

# src/pcbasm/visualization/residual_render.py
def render_scan_residuals(quality: CalibrationQuality,
                          before_views: Sequence[CheckerboardView],
                          after_views: Sequence[CheckerboardView],
                          output_path: Path) -> None
```

## 判断ログ

### 1. 移動は全点絶対座標。`relative` は一切使わない

計画§5 は `stage.move(dx, dy, relative=True)` と書いていたが、orchestrator の指示どおり
`start + grid.positions[i]` への絶対移動にした。`ScanGrid.positions` は開始位置に対する
相対で定義されるが、G-code は絶対にしないと 15 点ぶんの往復で丸め誤差が累積する。
蛇行順なので連続する移動は自然に単軸の小ステップになる。

- スキャン中の移動は `z` を渡さない（`XYZStage.move` は `None` 軸を G1 に出さない）ので
  Z は物理的に動かない。復帰移動だけ `x, y, z` の 3 軸を渡す（`OffsetTransformMeasurer.measure()`
  と同形）。
- 送信は `klipper.send_gcode(move + gcode.wait(settle_time) + gcode.wait_for_done())` の 1 回。
  `M400` 復帰時点で settle 済みなので捨てフレームは不要。**`send_gcode` の呼び出しは
  15 点 + 復帰 1 = 16 回**。

### 2. `pattern_size` の固定は `CheckerboardDetector` のレンジを 1 サイズに絞る

```python
columns, rows = pattern_size
CheckerboardDetector(pattern_rows_range=(rows, rows + 1),
                     pattern_cols_range=(columns, columns + 1))
```

`_candidates()` が `range(min, max)` なので候補は 1 個だけになる。`CheckerboardDetector` に
新しい引数を足す必要はなかった（既存 IF のまま速度要件を満たせる）。総当たりは 1 視点
5.45 秒なので絶対に走らせない。

### 3. `_fit_residuals` を private ヘルパとして抽出（数式の複製回避）

`ResidualReport.measure` の本体（差分・rank 検証・2x2 最小二乗・平均画像半径）を
`_fit_residuals(views) -> _ResidualFit` に切り出し、`measure` と `residual_field` が共有する。
`_ResidualFit` は ndarray を持つので `attrs.frozen(eq=False)`。

- `measure` の挙動は不変（既存テストが無改造で通ることで確認）。
- `residual_field` の戻りは `(corners, residuals)`、ともに `(N,2) float64` で単位は px。
  **`N = (視点数 - 1) × 1視点のコーナー数`**（基準視点 `views[0]` は差分の基準なので行を持たない）。
- `corners` は**非基準視点側のコーナー位置**を選んだ。「その残差を測ったときにコーナーが
  実際にあった場所」が quiver の矢印の根元として最も literal。対の中点にする案は、半径帯の
  帰属（Phase 1 で実測比較して平均半径を採用）と整合しないので採らない。

### 4. `undistort_views` は `solve` の内部処理をそのまま公開化

`IntrinsicsCalibrator.solve` が持っていた `attrs.evolve(view, corners=undistorter.apply_points(...))`
のループを関数に切り出し、`solve` はそれを呼ぶだけにした。重複ゼロ。

### 5. `usable_crop_side_px` の移設先と `summary_lines` の扱い

- **移設先は `CalibrationResult`**。`min(int(2r/sqrt(2)), width, height)` でクランプする
  （`self.resolution` = `intrinsics.resolution`）。`CalibrationQuality` は解像度を持たないので
  クランプできず、720px 高のフレームに 905 を返していた。WebUI で `min()` を取るのは
  薄ラッパー原則違反。
- **`summary_lines()` は `CalibrationQuality` に無変更で残した**。移設前から
  `usable_crop_side_px` を参照していなかった（8 行の内訳は残差 RMS / 再投影・σ・視点数 /
  帯 4 行 / 視点別最大 / 傾き診断）ので、引数追加も移設も不要。**`quality` の情報だけで
  組み立てられる行を `quality` に置く**という切り分けが保てている。crop 辺長は解像度が
  必要なので `CalibrationResult` 側の関心事で、Phase 3 が `JobResult.summary` に混ぜる。

### 6. `draw_scan_coverage` は `draw_overlay` を触らない

矩形描画が 3 行ほど似るが、`draw_scan_coverage` の矩形はラベル付きで複数枚描くので
別物。既存 `draw_overlay` をリファクタして共通化するのは「ついでの改善」（原則 3）に
当たるため行わない。配色は既存に合わせ、コーナー/十字線 `(0,255,0)` と矩形 `(0,255,255)`。

### 7. `render_scan_residuals` は constrained layout（`height_render.py` からの唯一の逸脱）

`height_render.py` は `plt.tight_layout()` を使うが、colorbar を 2 面にまたがらせると
tight_layout と両立しない。`plt.figure(layout="constrained")` にした。figure の作り方
（`matplotlib.use("Agg")` を import 時に、`fig.savefig(path, dpi=120)` → `plt.close(fig)`）は
`height_render.py` と同じ。ラベルは既存の描画モジュールに揃えて英語
（日本語フォントが無い環境で豆腐になる）。

- 上段 2 面は**カラースケール（`clim=(0, 共通 vmax)`）と矢印縮尺（`scale=共通`）の両方を共有**
  する。片方だけ共有だと「補正後の方が矢印が長い」ような誤読が起きる。
- 矢印は最大残差が 60px 相当になるよう誇張する（残差は数 px なので等倍では見えない）。
- 下段の折れ線は `bucket.radius_px[1]`（帯上限）vs `rms_um`。

## 検証結果

- `make format`: **pass**
- `make type`: **既知の Phase 3 2 件のみ**（`src/webui/jobs/posctrl.py:31` の
  `CheckerboardCalibrator` import、`tests/webui/jobs/test_posctrl.py:266` の `crop_size`）。
  自分の担当ファイルは 0 error、エラーを増やしていない。
- `uv run pytest tests/pcbasm -m "not hardware"`: **1057 passed**（Phase 1 は 1034。
  spec-test-author の Phase 2 テスト 23 ケース増を含む）。**移設由来の失敗も、それ以外の
  既存テストの破壊もゼロ**。
- 合成カメラ（`tests/helpers.SyntheticCheckerboardCamera`）で全経路を通した実測:
  計画用ショットで `pattern_size=(11,8)` 確定 → `ScanGrid.plan` で 15 点
  （span 22.71 x 8.70mm、被覆半径 640.4px）→ `scan` で **15/15 検出**、`send_gcode` 16 回、
  開始位置へ復帰 → `solve` で ppm 30.315、残差 **118.7µm → 4.4µm**。
  `residuals.png` / `corner_coverage.png` を目視確認し、600x600 矩形の全域がコーナーで
  覆われている（計画§7 の「crop 600 に戻せる」根拠を再現）。
- `make test` / 実機テストは実行していない。
- `grep -rn '</content>' src tests` は空。

## Phase 3（WebUI）への申し送り

### ジョブから呼ぶ API の確定形と呼び出し順

```python
# [2] 計画用ショット（生フレーム）
camera = ctx.open_camera(raw=True)          # ← raw 伝播が Phase 3 の実装項目
plan_detector = CheckerboardDetector()      # ここだけ総当たり 1 回（1 視点 5.45s を許容）
plan_view = plan_detector.detect(camera.capture(), Point2d(start.x, start.y))
# None → log して [1] の prompt へ戻る

# [3] Klipper / homed_axes 確認 → start = stage.get_position() / z_position = start.z

# [4] 格子
grid = ScanGrid.plan(
    image_size=camera.resolution.size,      # machine.toml ではなく実カメラの解像度
    corners=plan_view.corners,
    pattern_size=plan_view.pattern_size,    # ← 計画に無い引数（Phase 1 で追加）
    pixel_per_mm=<計画用ショットの ppm>,
)   # None → RuntimeError（盤が視野に対して大きすぎる）

# [5] スキャン
scanner = CheckerboardScanner(klipper, stage, camera,
                              pattern_size=plan_view.pattern_size)
outcome = scanner.scan(grid, on_view=<ctx.checkpoint/progress/log/frame を呼ぶ関数>)
# on_view は成功・失敗の両方で毎点呼ばれる。progress.annotated が None の点が失敗。
# ctx.checkpoint() の例外はそのまま伝播し、finally で開始位置へ復帰する（握らない）。
# 失敗フレームは outcome.failures[i].raw を scan_failure_{index}.png に保存。

# [6] len(outcome.views) < MINIMUM_SCAN_VIEWS → RuntimeError

# [7] 校正
result = IntrinsicsCalibrator(square_size, camera.resolution.size).solve(outcome.views)
# solve / measure の ValueError（視点数不足・pattern_size 不一致・ステージ位置が同一直線上）は
# RuntimeError に変換して操作者に見せるだけでよい。別途 rank チェックは不要。

# [8] レポート
for line in result.quality.summary_lines():   # 8 行。文言を作らずそのまま ctx.log
    ctx.log(line)
side = result.usable_crop_side_px(residual_limit)      # ← CalibrationResult 側（解像度クランプ済み）
after_views = undistort_views(outcome.views, result.intrinsics)
render_scan_residuals(result.quality, outcome.views, after_views, artifacts / "residuals.png")
coverage = draw_scan_coverage(camera.resolution.size, outcome.views,
                              [(300, 300), (600, 600)])
# → coverage.save(artifacts / "corner_coverage.png")

# [8'] result.quality.after.rms_um > residual_limit → artifacts 保存後に RuntimeError
# [9] JobResult(summary, artifacts, ApplyPayload(files=[ApplyFile(...result.to_dict()...)]))
```

### 注意点

- **`render_scan_residuals` の `before_views` / `after_views` は `quality.before` /
  `quality.after` と同じ視点集合を渡すこと**（`solve` に渡した `outcome.views` と、その
  `undistort_views` 結果）。別集合を渡すと quiver と折れ線の数字が食い違う。
- `usable_crop_side_px` は `CalibrationQuality` **ではなく** `CalibrationResult` のメソッド。
  JS / router 側で `min()` を取り直さないこと（解像度クランプ済み）。
- `ScanProgress` / `ScanFailure` / `ScanOutcome` は `eq=False`（`Image` を持つ）。値等価比較は
  できないのでフィールド単位で扱う。
- `CheckerboardScanner` は Z を動かさず、`finally` で必ず開始位置（x, y, z）へ絶対移動で戻る。
  ジョブ側で復帰処理を重ねる必要はない。
- 実測所要は 15 点で移動 + settle 0.5s + 検出 ≈ 20 秒。`ctx.checkpoint()` を `on_view` で
  毎点呼べば中止応答は 1.5 秒以内になる。
