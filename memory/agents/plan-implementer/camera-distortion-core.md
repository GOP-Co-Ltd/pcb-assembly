# カメラ歪み補正 Phase 1（pcbasm コア層）

計画書: `/home/gop/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
ブランチ: `feature/20260728/camera-distortion`

## 実装したファイル

| ファイル | 扱い |
|---|---|
| `src/pcbasm/vision/intrinsics.py` | 新設。`CameraIntrinsics` / `Undistorter` |
| `src/pcbasm/vision/calibration.py` | 全面書き換え |
| `src/pcbasm/vision/__init__.py` | re-export 更新（`CheckerboardCalibrator` 撤去） |
| `src/pcbasm/hal/framehub.py` | 二系統バッファ + `subscribe(raw=)` |
| `src/pcbasm/posctrl/setup.py` | `camera` 必須化 + `create_camera` フォールバック削除 |
| `src/pcbasm/session.py` | `PasteSession.setup` の `camera` 型追随のみ |

## 計画から変えた点（すべて意図的）

### 1. `CheckerboardView` に `image_size` フィールドを追加（IF 変更）

計画の 3 フィールド（`stage_position` / `corners` / `pattern_size`）では
`ResidualReport.measure(views)` が**半径帯を計算できない**（画像中心が分からない）。
計画§7 の半径帯 150/300/450/640px は画像中心からの半径で、640 は 1280x720 の半対角
734px と整合するので、画像中心が必要なことは確定。

`measure` に `image_size` 引数を足す案より、view が自分の出所フレームを持つ方を採った。
`CheckerboardDetector.detect()` が `image.size` からタダで埋められる。

```python
CheckerboardView(stage_position, corners, pattern_size, image_size)
```

### 2. `ScanGrid.plan` に `pattern_size` 引数を追加（IF 変更）

計画§2 の signature には無いが、計画の補足指示「`pattern_size` と `pixel_per_mm` から
盤全体の寸法を推定」が `pattern_size` を要求している。コーナー外接矩形に 1 マス分の
静穏枠を足すには 1 マスの px を知る必要があり、それは外接矩形 ÷ (内部コーナー数 - 1)。

```python
ScanGrid.plan(*, image_size, corners, pattern_size, pixel_per_mm,
              columns=SCAN_COLUMNS, rows=SCAN_ROWS) -> Self | None
```

実測で計画§7 の手計算を再現: span=(22.72, 8.70)mm（計画 22.6 / 8.7）、
`max_corner_radius_px`=640.5（計画 ≈640）、15 点、`(0,0)` を含む。

### 3. `ResidualReport.measure` の写像 `A` は一般 2x2（等方スケール×回転にしない）

計画§2 は「2x2 = 等方スケール × 回転、4 パラメータ」と書いており、
「等方スケール×回転」（2 パラメータ）と「4 パラメータ」が矛盾している。
**4 パラメータ＝一般 2x2 を採った。理由は物理**:

下向きカメラでは画像 y 軸が機械 Y 軸と逆向きになる（光軸 = -Z_machine、右手系より
cam_y = -mY）。つまり `A` の行列式は負で、**回転のみのモデルでは表現できない**。
回転+スケールに制限すると実機で残差が発散する。

検証: 合成データで `flip_y=False/True` の両方で ppm=30.3100（真値 30.31）、
補正後残差 0.0µm。鏡映があっても成立することを確認済み。

- `pixel_per_mm` = `sqrt(|det A|)`
- `rotation_deg` = `atan2(A[1,0], A[0,0])`（機械 +X が画像上で向く方向。診断用のみ、
  `CalibrationResult` には入らない）

### 4. `measure` は「同一直線上でない 3 視点以上」を要求する（計画の想定と差異）

一般 2x2 を決めるにはステージ変位が 2 方向に広がっている必要がある。
`np.linalg.matrix_rank(view_deltas) < 2` で `ValueError`。

**計画のテスト欄「`views < 2` で `ValueError`」は、2 視点なら通る前提に読める。
しかし 2 視点は変位方向が 1 つしかなく px↔mm 写像が原理的に決まらない**（放置すると
lstsq が最小ノルム解を返し、det≈0 → ppm≈0 → 残差が NaN になり、`residual_limit`
ゲートが `NaN > limit == False` で**素通り**する＝誤った (K,D) を採用する重大な罠）。
実スキャンは 5x3 なので実運用では常に rank 2。

検証順序は「視点数 → コーナー数一致 → rank」。

### 5. 半径帯への帰属はコーナー対の**平均画像半径**

残差は 2 視点の差分量なので「その残差の半径」が一意でない。帰属方法を 4 通り実測比較:

| 帰属 | barrel の帯別 rms | 単調増加 |
|---|---|---|
| 当該視点の半径 | 117.6 / 138.9 / 136.5 / 132.8 | ✗（ほぼ平坦） |
| **両者の半径の平均** | **82.2 / 104.6 / 139.6 / 195.9** | **✓** |
| 中点の半径 | 123.3 / 96.0 / 177.1 / 186.1 | ✗ |
| 両者の max | 86.7 / 111.4 / 92.8 / 171.5 | ✗ |

barrel / pincushion / 弱歪み(k1=-0.05) の 3 条件すべてで平均半径だけが単調増加。
計画テスト欄の「半径バケット単調増加」を満たすのは平均半径のみ。

### 6. `_SAFETY_MARGIN_PX = 24.0` を module private 定数に（計画の「片側 24px」）

## 公開シグネチャの最終形（Phase 2 / 3 はこれに合わせる）

```python
# src/pcbasm/vision/intrinsics.py
@attrs.frozen
class CameraIntrinsics:
    camera_matrix: tuple[tuple[float, float, float], ...]
    distortion: tuple[float, float, float, float, float]   # (k1,k2,p1,p2,k3)
    resolution: tuple[int, int]
    def matrix(self) -> ImageArray                 # 3x3 float64
    def coefficients(self) -> ImageArray           # (5,) float64
    @classmethod
    def of(cls, camera_matrix, distortion, resolution) -> Self

class Undistorter:
    def __init__(self, intrinsics: CameraIntrinsics) -> None
    @property
    def resolution(self) -> tuple[int, int]
    def apply(self, image: Image) -> Image                    # size 不一致は ValueError
    def apply_points(self, points: ImageArray) -> ImageArray  # (N,2) -> (N,2)

# src/pcbasm/vision/calibration.py
SCAN_COLUMNS = 5
SCAN_ROWS = 3
MINIMUM_SCAN_VIEWS = 8
RESIDUAL_BUCKET_EDGES_PX = (150.0, 300.0, 450.0, 640.0)

@attrs.frozen(eq=False)
class CheckerboardView:
    stage_position: Point2d
    corners: ImageArray            # (N,1,2) float32 フル解像度
    pattern_size: tuple[int, int]  # (cols, rows)
    image_size: tuple[int, int]    # (width, height)  ← 計画に無い追加

@attrs.frozen
class ScanGrid:
    positions: tuple[Point2d, ...]       # 蛇行順の相対 XY [mm]、(0,0) を含む
    columns: int
    rows: int
    span_mm: tuple[float, float]
    max_corner_radius_px: float
    @classmethod
    def plan(cls, *, image_size, corners, pattern_size, pixel_per_mm,
             columns=SCAN_COLUMNS, rows=SCAN_ROWS) -> Self | None

@attrs.frozen
class RadialResidualBucket:
    radius_px: tuple[float, float]; sample_count: int; rms_um: float; max_um: float

@attrs.frozen
class ViewResidual:
    index: int; stage_position: Point2d; rms_um: float

@attrs.frozen
class ResidualReport:
    pixel_per_mm: float; rotation_deg: float; rms_um: float; max_um: float
    buckets: tuple[RadialResidualBucket, ...]
    view_residuals: tuple[ViewResidual, ...]   # index は 1 始まり（基準視点 0 は含まない）
    corner_count: int                          # 残差サンプル総数 = (視点数-1) x 1視点コーナー数
    @classmethod
    def measure(cls, views, *, bucket_count: int = 4) -> Self

@attrs.frozen
class CalibrationQuality:
    reprojection_rms_px: float
    before: ResidualReport
    after: ResidualReport
    pixel_per_mm_std: float
    view_count: int
    def summary_lines(self) -> tuple[str, ...]
    def usable_crop_side_px(self, limit_um: float) -> int

@attrs.frozen
class CalibrationResult:
    intrinsics: CameraIntrinsics
    pixel_per_mm: float
    square_size_mm: float
    quality: CalibrationQuality
    calibrated_at: datetime
    z_position: float | None = None
    @property
    def resolution(self) -> tuple[int, int]
    # to_dict / from_dict / save / load（既存 _converter 再利用）

class CheckerboardDetector:
    def __init__(self, pattern_rows_range=(4,12), pattern_cols_range=(4,12)) -> None
    @property
    def pattern_size(self) -> tuple[int, int] | None
    def detect(self, image: Image, stage_position: Point2d) -> CheckerboardView | None
    def draw(self, image: Image, view: CheckerboardView) -> Image

class IntrinsicsCalibrator:
    def __init__(self, square_size_mm: float, resolution: tuple[int,int],
                 *, min_views: int = 4) -> None
    def solve(self, views: Sequence[CheckerboardView]) -> CalibrationResult

def load_undistorter(calibration_file: Path,
                     resolution: tuple[int,int]) -> Undistorter | None

# src/pcbasm/hal/framehub.py
FrameHub(camera: Camera, undistorter: Undistorter | None = None)
FrameHub.subscribe(timeout: float = 5.0, *, raw: bool = False) -> FrameSource
# FrameSource は 1 行も変えていない

# src/pcbasm/posctrl/setup.py（camera が keyword 必須に）
setup_board_calibration(machine, pcb_file_path, tolerance=0.1, *,
                        camera: Camera, frame_sink=None) -> BoardCalibrationResult
```

## 実測した挙動（合成データ・実 OpenCV、計画の予測と照合）

真値 K: f=1200px, 主点 (645,355), 1280x720、ppm=30.31、パターン 11x8 / 1.5mm、15 視点。

| 項目 | 実測 | 計画の予測 |
|---|---|---|
| 補正前残差 rms | 133.7µm (barrel) / 161.1µm (pincushion) | 131µm |
| 補正後残差 rms（雑音なし） | 0.0µm | — |
| 画素空間マップ誤差（雑音なし・全画面 max） | 0.0001px | — |
| `pixel_per_mm` 復元 | 30.3100（真値 30.31、誤差 <0.001%） | 0.1% 以内 |
| `rotation_deg`（ステージ 3° 回転） | 3.000° | 3° |
| 帯別残差（平均半径帰属） | 82 / 105 / 140 / 196µm 単調増加 | 単調増加 |
| 白画像の黒画素（k1=-0.25 全画面） | 0 | 0 |
| 白画像の黒画素（k1=+0.30 中央600） | 0 | 0 |
| `k1` 単体 | -0.1365（真値 -0.12） | 縮退により一致しない前提 |

`load_undistorter` の degrade 4 ケース（不在 / 壊れた JSON / 旧スキーマ / 解像度不一致）
すべて None + warning 1 行を実測確認。旧スキーマ JSON は cattrs が構造化に失敗して None。

## Phase 2 / Phase 3 への申し送り

### Phase 2A（`posctrl/checkerboard_scan.py`）

- `CheckerboardScanner` は `CheckerboardDetector.detect(image, stage_position)` を呼ぶ。
  `stage_position` には**コマンドした絶対 XY**（開始位置 + `grid.positions[i]`）を渡す。
  `ResidualReport.measure` は差分しか使うので絶対/相対の基準はどちらでもよいが、
  `ViewResidual.stage_position` がログに出るので絶対の方が読みやすい。
- 有効視点は「同一直線上でない 3 点以上」が必須（上記 4）。蛇行順の 15 点なら
  1 行だけ全滅しても rank 2 は保たれるが、**X 方向 1 行だけ生き残った場合は
  `measure` が ValueError を投げる**。`MINIMUM_SCAN_VIEWS`(=8) のチェックでは
  この条件を保証しないので、Phase 3 のジョブは `solve` の ValueError を
  RuntimeError に変換して操作者に伝えるだけでよい（別途 rank チェックは不要）。

### Phase 2B（`visualization/residual_render.py` / `overlay.draw_scan_coverage`）

- コーナー座標は `view.corners.reshape(-1, 2)` で取れる（`_corner_points` は private）。
- `view.image_size` があるので描画側で画像サイズを別引数にする必要はない。
- 半径 vs RMS の折れ線は `report.buckets[i].radius_px[1]` と `rms_um` を使う。
  帯の下限は `radius_px[0]`。

### Phase 3（`webui/jobs/posctrl.py` / `state.py`）

- **`make type` が今 2 件だけ落ちている。両方 Phase 3 の担当範囲**:
  - `src/webui/jobs/posctrl.py:31` — `CheckerboardCalibrator` の import（撤去済みシンボル）
  - `tests/webui/jobs/test_posctrl.py:266` — `CalibrationResult.crop_size`（削除済み属性）
- `state.py` の配線は `load_undistorter(machine.camera.calibration_file, camera.resolution.size)`
  → `FrameHub(camera, undistorter)`。`Resolution.size` が `(w,h)` であることを確認して渡すこと。
- `ScanGrid.plan` には `pattern_size=detector.pattern_size` を渡す（計画に無い引数）。
- `quality.summary_lines()` は「残差 RMS 行 / 再投影・σ・視点数行 / 帯 4 行 /
  視点別最大行 / 傾き診断行」の 8 行を返す。`ctx.log` に 1 行ずつ流すだけでよい。

### 残課題（計画どおりに実装したが、実機で見直す価値あり）

1. **`usable_crop_side_px` はフレームサイズでクランプしない**。指示どおり
   `floor(2r/sqrt(2))` をそのまま返すので、最外帯(640px)まで合格すると **905** を返し、
   1280x720 の**高さ 720 を超える**値を推奨してしまう。`CalibrationQuality` は解像度を
   持たないのでクランプできない設計。Phase 3 側で
   `min(side, image_height, image_width)` して表示するのが妥当。
2. `PasteSession.setup`（`src/pcbasm/session.py:50`）は**どこからも呼ばれていない dead code**。
   計画§3 の指示どおり削除せず、`camera` の型追随だけ行った。
3. **`TestDistortionRecovery` の閾値**（spec-test-author 向け実測値）。コーナー雑音 σ に対し
   画素空間マップ誤差は次のようにスケールする（5 seed の max）:

   | σ [px] | 全画面 max | 中央600 max | `after.rms_um` |
   |---|---|---|---|
   | 0.00 | 0.000 | 0.000 | 0.0 |
   | 0.05 | 0.204 | 0.049 | 3.5 |
   | 0.10 | 0.408 | 0.099 | 7.0 |
   | 0.20 | 0.818 | 0.200 | 14.0 |

   計画の「全画面 max < 0.2px」「`after.rms_um` < 5」は **σ ≲ 0.05px でしか成立しない**。
   レンダリング合成カメラ + `cornerSubPix` は 0.05〜0.15px 程度になりやすいので、
   全画面ではなく**中央 600（実際に使う crop 領域）**でピンするか、全画面の閾値を
   0.5px 程度に緩めるのが妥当。

## 検証結果

- `make format`: **pass**
- `make type`: 自分の担当ファイルは **0 error**。全体では 2 error（上記 Phase 3 の 2 件のみ）
- `uv run pytest tests/pcbasm -m "not hardware"`: **1034 passed**（spec-test-author の
  新規 `test_intrinsics.py` / 書き換え `test_calibration.py` / `TestDistortionRecovery` を含む）
- `tests/pcbasm/hal/test_framehub.py`: 既存 23 テストが**無改造で pass**
  （`FrameSource` 不変・`undistorter=None` で素通しの担保）
- `make test-no-hardware` 全体は未達（`src/webui/jobs/posctrl.py` が Phase 3 待ち）
- `make test` / 実機テストは実行していない
