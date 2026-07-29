# 銅箔照合を「数領域の平均補正」へ作り直す

上位計画: `/home/gop/.claude/plans/claude-maximize-parallels-majestic-pike.md`（方針・決定事項はそちらが正典。本書は実装粒度への落とし込み）。
ブランチ: `feature/20260729/region-alignment-average`

## 概要

部品単位の pad 照合（開口問題でコスト平坦域が生じ、部品ごとにバラバラな 0.1mm 級のずれを出していた）を撤去し、幾何で「両方向に拘束がある」ことを保証した数個の大領域から 1 ショットずつ並進を測り、その平均を基板全体の単一並進補正として機械座標へ出る瞬間に 1 回だけ適用する。

## 実測で確定させた数値（後続はこれをテスト期待値に使ってよい）

環境: OpenCV 4.12.0 / numpy 2.2.6。スクリプトは
`/tmp/claude-1000/-home-gop-pcb-assembly/b495f7b4-e830-4365-8865-c0a78c6d65d3/scratchpad/exp{_sharpness,2,3,4,5}.py`。

### distanceTransform の精度（DIST_MASK_PRECISE を使う根拠）

真の距離 30px の点で `maskSize=3` は **28.6501**（−4.50%）、`DIST_MASK_PRECISE` は **30.0000**（誤差 0）。

### sharpness の分布（`region_size_px=400`, `window_px=42`, `search_window=1.4mm`, `pixel_per_mm=30.225`）

| ケース | `|T|` | `sharpness` | 復元した並進 | 判定 |
|---|---|---|---|---|
| 正方リング 100×100（真値 +2,+3） | 400 | **0.7059** | (+2.000, +3.000) | 採択 |
| pad 群 6×6 = 24×12 矩形（真値 +2,+3） | 2592 | **0.5693** | (+2.000, +3.000) | 採択 |
| 実 PCB `TJ-56-67` の 400px 領域（幾何予測 √(λmin/L)） | — | **0.63〜0.70** | — | 採択 |
| 細長リング 240×8 | 496 | **0.1758** | (0, 0) | 採択（境界付近） |
| 水平線 2 本 + 20px 縦線 | 623 | **0.1921** | (0, 0) | 採択（境界付近） |
| 水平線 1 本のみ（両端が視野内 = 端点だけが x を拘束） | 301 | **0.0576** | (0, 0) | **棄却** |
| 水平線 1 本のみ（観測が視野を縦断 = 真に平坦） | 301 | **0.0000** | dx=**+38.17** デタラメ | **棄却** |
| 水平ストライプ 8 本のみ（400px 領域） | 3048 | — | `min_loc` が窓端 | **棄却（`None`）** |

→ **`min_sharpness` の既定値は `0.15`**。棄却側の最大 0.0576 と採択側の最小 0.1758 の間に置く。
実機の 400px 領域は 0.5〜0.7 に出るはずで、境界からは十分離れている。実機で調整するのはユーザー。

**テストを書くときの注意**: 0.1758 / 0.1921 は閾値 0.15 との距離が近く脆い。**テストの採択ケースは 0.5 以上、棄却ケースは 0.06 以下**のものを使うこと（上表の 1・2 行目と 6・7 行目）。

### サブピクセル復元精度

円 4 個（`cv2.circle(..., shift=4)` で 1/16px 精度に描画）を −2.0〜+2.0px まで 0.2px 刻みで
21 通りずらし、`(dx, dy) = (d, −d/2)` を復元した結果:

- **最大誤差 0.110 px**、整数ずれのとき誤差 0.000 px
- `rms_distance_px` は整数一致で **0.0000**、サブピクセルずれで **0.485〜0.520**（0 飽和が消えている）
- 生の `min_val` は FFT 丸めで負値（実測 −8.9e-08）になり得るので `max(c*, 0)` は必須

→ テストの許容は **`abs=0.15` px**（0.110 に余裕を足した値）。「0.1px 精度」は達成できない。

### 領域計画の実測（実 PCB `data/TJ-56-67/TJ-56-67.kicad_pcb`）

copper polygon 48・TOP pad 48・外形 89.5×58.0mm → 線分 2880 本、候補格子 14×5 = 70、
**採点全体で 0.04 秒**。選ばれた 4 領域は board 座標で (16.0, 8.9) (54.8, 8.9) (67.8, 22.0) (80.7, 8.9)、
λmin = 594 / 546 / 265 / 265、予測 sharpness 0.63〜0.68。

---

## 公開インターフェース（**確定。ここから逸脱しないこと**）

### `src/pcbasm/posctrl/copper.py`

```python
type PixelRect = tuple[int, int, int, int]  # (x0, y0, x1, y1) 半開区間・全画面 px（既存のまま）


def centered_roi(image_size: tuple[int, int], size_px: int) -> PixelRect:
    """画像中心に一辺 size_px の正方 ROI を取る（フレームへクランプ）.

    Args:
        image_size: 画像サイズ (width, height)
        size_px: ROI の一辺 [px]

    Returns:
        ROI 矩形。size_px が画像より大きい場合はフレーム全体にクランプする

    Raises:
        ValueError: size_px が 1 未満の場合
    """


@attrs.frozen
class CopperProjection:  # 変更なし
    fill_mask: ImageArray = attrs.field(eq=False)
    edge_mask: ImageArray = attrs.field(eq=False)


@attrs.frozen
class EdgeMatch:
    """観測エッジと想定エッジの照合結果.

    Attributes:
        offset: サブピクセル並進（観測 − 想定、px。``.mm`` で mm）
        rms_distance_px: 補間した最小コストでの想定エッジ 1 点あたり RMS chamfer 距離 [px]
        sharpness: コスト曲面の拘束の強さ [px]。弱軸方向へ 1px ずらしたときの
            RMS 距離の増分。0 = 完全な開口問題、正方リングで 0.71 が上限に近い
    """

    offset: Offset
    rms_distance_px: float
    sharpness: float

    @property
    def camera_transform(self) -> Transform: ...  # Shift.from_point(self.offset.mm)（既存のまま）


class CopperProjector:
    def __init__(
        self,
        polygons: Sequence[Polygon],
        board_transform: Transform,
        offset_transform: Transform,
        pixel_per_mm: float,
        image_size: tuple[int, int],
    ) -> None: ...                                        # 変更なし

    def project(self, stage_xy: Point2d) -> CopperProjection: ...      # 変更なし
    def pixel_of(self, board_point: Point2d, stage_xy: Point2d) -> Point2d: ...  # 変更なし

    # ↓ private からの昇格（plan_alignment_regions が全線分をベクトル化投影するのに必要。
    #    pixel_of を頂点ごとに呼ぶと数千回の Transform.apply になるため）
    def board_to_pixel_affine(self, stage_xy: Point2d) -> tuple[ImageArray, ImageArray]:
        """board 座標 → pixel 座標のアフィン (2x2 行列, 平行移動) を返す.

        ``pixels = coords @ matrix.T + shift``。matrix は stage_xy に依存しない
        （stage_xy は shift のみを動かす）。
        """

    @property
    def polygons(self) -> tuple[Polygon, ...]:
        """投影対象の銅箔ポリゴン（board 座標、mm）."""

    # roi_of は削除


class CopperEdgeMatcher:
    def __init__(
        self,
        pixel_per_mm: float,
        search_window_mm: float = 2.0,
        *,
        min_sharpness: float = 0.15,
    ) -> None:
        """Args:
            pixel_per_mm: pixel/mm 比率
            search_window_mm: 探索窓の片側幅 [mm]
            min_sharpness: これ未満の sharpness を拘束不足として棄却する閾値 [px]
        """

    @property
    def window_px(self) -> int:
        """探索窓の片側幅 [px] = round(search_window_mm * pixel_per_mm)."""

    def match(
        self,
        observed_edges: ImageArray,
        expected_edges: ImageArray,
        roi: PixelRect,
    ) -> EdgeMatch | None:
        """観測エッジと想定エッジのサブピクセル並進ずれを照合する.

        Returns:
            照合結果（offset = 観測 − 想定）。次のいずれかで None:
            観測エッジが空 / ROI 内の想定エッジが空 / 最小コスト位置が
            探索窓の端に張り付いた / sharpness < min_sharpness（拘束不足）
        """

    # crop_size 引数と _template_rect は削除。roi は必須（None 経路なし）
```

### `src/pcbasm/posctrl/region.py`（新規・純幾何・HAL 非依存）

```python
@attrs.frozen
class AlignmentRegion:
    """銅箔照合の関心領域.

    Attributes:
        index: 巡回順の 0 始まり通し番号
        anchor: 機械座標 [mm]。ここへ移動して撮像すると領域が画像中心に来る
        roi: 照合 ROI（全画面 px）。全 region で同一の画像中心固定矩形
        constraint: λ_min(A) [px]。A = Σ L·n nᵀ（ROI 内に入る線分長 L と単位法線 n）
        edge_length_px: ROI 内に入る想定エッジの総長 [px]。
            予測 sharpness = sqrt(constraint / edge_length_px)
    """

    index: int
    anchor: Point2d
    roi: PixelRect
    constraint: float
    edge_length_px: float


def plan_alignment_regions(
    projector: CopperProjector,
    pad_centers: Sequence[Point2d],
    board_transform: Transform,
    *,
    region_size_px: int,
    count: int,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """拘束の強い関心領域を count 個まで選び、巡回順に並べて返す.

    Args:
        projector: 設計銅箔の投影器（ポリゴンとアフィンの供給元）
        pad_centers: 候補格子を張る範囲を決める TOP pad 中心（board 座標、mm）
        board_transform: board 座標 → 機械座標の変換（anchor の算出に使う）
        region_size_px: 領域の一辺 [px]
        count: 選ぶ領域数の上限
        image_size: カメラ画像サイズ (width, height)
        tour_start: 巡回の起点（機械座標、mm）

    Returns:
        0 個以上 count 個以下の AlignmentRegion（巡回順、index は 0 始まりで振り直し）。
        constraint <= 0 の候補は除外するので、銅箔が無ければ空リストを返す。
        **不足しても例外は投げない**（min_regions の判定は呼び出し側の責務）

    Raises:
        ValueError: region_size_px < 1 / count < 1 / pad_centers が空 /
            region_size_px > min(image_size)
    """
```

### `src/pcbasm/posctrl/aligner.py`（`pad.py` をリネームして全面置換）

```python
@attrs.frozen
class RegionAlignment:
    """1 領域の照合結果.

    照合が並進のみなので machine_transform も純並進になり、補正量はアンカーからの
    距離に依存しない。

    Attributes:
        region: 対象領域
        match: 照合結果
        machine_transform: 設計 machine 点 → 観測 machine 点の変換（純並進）
    """

    region: AlignmentRegion
    match: EdgeMatch
    machine_transform: Transform

    @property
    def translation(self) -> Point2d:
        """machine 空間の並進補正量 [mm]（= machine_transform.apply(anchor) − anchor）."""


class RegionAligner:
    """領域単位で銅箔照合による位置ずれを 1 ショット計測するクラス."""

    def __init__(
        self,
        *,
        camera: Camera,
        klipper: Klipper,
        stage: XYZStage,
        projector: CopperProjector,
        matcher: CopperEdgeMatcher,
        edge_detector: CopperEdgeDetector,
        offset_transform: Transform,
        max_correction_mm: float | None = 1.0,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None: ...

    def measure(self, region: AlignmentRegion) -> RegionAlignment:
        """領域のアンカーへ移動し、1 回の撮像で並進ずれを計測する.

        Raises:
            RuntimeError: 照合に失敗（matcher が None）した場合、または
                照合ずれが max_correction_mm を超えた場合
        """
```

### `src/pcbasm/posctrl/alignment.py`

```python
@attrs.frozen
class BoardAlignment:
    """複数領域の計測から得た基板全体の平均並進補正.

    Attributes:
        results: 成功した領域計測（1 件以上）
    """

    results: tuple[RegionAlignment, ...]

    def __attrs_post_init__(self) -> None:
        """Raises: ValueError: results が空の場合"""

    @property
    def translation(self) -> Point2d:
        """machine 空間の平均並進 [mm]."""

    @property
    def machine_transform(self) -> Transform:
        """Shift.from_point(self.translation)."""

    @property
    def spread(self) -> Point2d:
        """領域間の標準偏差 [mm]（母標準偏差 ddof=0。1 領域なら (0, 0)）."""


class RegionAlignmentSession:
    """TOP 層銅箔照合による領域位置合わせの配線をまとめたセッション."""

    def __init__(
        self, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> None:
        """Raises:
            ValueError: region_size_px + 2 * window_px がキャリブレーション解像度に
                収まらない場合
        """

    @classmethod
    def from_calibration(
        cls, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> Self: ...

    def plan_regions(self) -> list[AlignmentRegion]:
        """TOP pad の分布から照合領域を計画する（撮像・移動なし）.

        巡回起点は呼び出し時の ``stage.get_position()``。
        """

    def measure(self, region: AlignmentRegion) -> RegionAlignment | None:
        """1 領域を計測し、失敗時は警告 log の後 None を返す."""

    def corrected_projector(self, machine_transform: Transform) -> CopperProjector:
        """board 変換を補正済みに差し替えた CopperProjector を生成する（既存と同じ）."""

    @property
    def projector(self) -> CopperProjector: ...      # 既存のまま（表示用）

    @property
    def edge_detector(self) -> CopperEdgeDetector: ...  # 既存のまま（表示用）

    @property
    def region_roi(self) -> PixelRect:
        """全 region 共通の画像中心 ROI（overlay の描画範囲に使う）."""
```

### `src/pcbasm/posctrl/render.py`

```python
class PadResultRenderer:
    def __init__(
        self,
        *,
        projector: CopperProjector,
        edge_detector: CopperEdgeDetector,
        roi: PixelRect,
        paste_polygons: Sequence[Polygon],
        position: Point2d,
    ) -> None: ...

    def render(self, image: Image, lines: Sequence[str]) -> Image: ...  # 変更なし
```

- `roi_polygons` / `pad_align` 引数を削除し、`roi: PixelRect` を直接受け取る
- モジュールから `_centered_roi` を削除（`copper.centered_roi` へ移設・公開）
- `from pcbasm.config import PadAlign` の import を削除
- `render_edge_match` / `render_label` は変更なし
- **ラベルへの `rms_distance_px` / `sharpness` 表示は renderer を変えない**。`render(image, lines)` が
  任意行を受けるので、文字列は webui 側（`posctrl.py`）で組み立てる

### `src/webui/jobs/board_ops.py`

```python
def measure_regions(
    ctx: JobContext,
    session: RegionAlignmentSession,
    regions: Sequence[AlignmentRegion],
    *,
    min_regions: int,
    on_failure: Callable[[AlignmentRegion], None] | None = None,
) -> BoardAlignment:
    """領域単位の銅箔照合ループの共通骨格.

    領域ごとに progress("銅箔照合") → checkpoint → session.measure →
    dx/dy/rms/sharpness の log。失敗は警告 log の後 on_failure（あれば）を
    呼んで続行する。最後に成功数が min_regions 未満なら中止する。

    Args:
        min_regions: 成功が必要な最小領域数（1 以上）
        on_failure: 照合失敗時に呼ぶコールバック（board_tour が FAILED overlay に使う）

    Raises:
        ValueError: 計画領域数が min_regions 未満の場合（移動前に判定）、
            または成功領域数が min_regions 未満の場合
    """
```

`setup_board` / `confirm_next_point` は変更なし。`align_component_groups` と
`pad_align_abort_message` は削除。

### `src/pcbasm/config.py` — `PadAlign`

```python
@attrs.frozen
class PadAlign:
    """領域単位の銅箔照合による位置合わせの設定."""

    max_correction: float = 1.0     # 1 照合で許容する最大ずれ [mm]。超過は照合失敗
    search_window: float = 2.0      # 照合の探索窓 片側幅 [mm]
    region_size_px: int = 400       # 照合領域の一辺 [px]
    region_count: int = 4           # 計画する照合領域数
    min_regions: int = 3            # 成功が必要な最小領域数。下回ると塗布ジョブを中止
    min_sharpness: float = 0.15     # 拘束不足として棄却する sharpness 閾値 [px]
    canny_low: float = 100.0
    canny_high: float = 200.0
    blur_ksize: int = 5

    def __attrs_post_init__(self) -> None:
        """region_size_px / region_count / min_regions が 1 以上、
        min_sharpness が 0 以上であることを検証する（いずれも ValueError）.

        min_regions <= region_count は検証しない（設定ファイルが読めなくなる
        より、実行時に measure_regions が明示的に中止するほうが直せる）。
        """
```

削除: `tolerance` / `roi_margin` / `min_roi` / `max_failures`。

---

## 実装ステップ

### `plan_implementer` の担当（`src/` のみ）

#### 1. `src/pcbasm/posctrl/copper.py`

**`centered_roi`（新規・モジュール関数）**

```
w, h = image_size
size = clamp(size_px, 1, min(w, h))     # size_px < 1 は ValueError
x0 = (w - size) // 2 ; y0 = (h - size) // 2
return (x0, y0, x0 + size, y0 + size)
```

**`CopperProjector`**: `_board_to_pixel_affine` → `board_to_pixel_affine`（本体は変更なし）、
`polygons` プロパティ追加（`tuple(self._polygons)`）、`roi_of` と `import math` 削除。

**`CopperEdgeMatcher.match` の確定アルゴリズム**（順序どおりに）

```
1.  observed_edges の非零が 0 → None
2.  x0, y0, x1, y1 = roi
    template = (expected_edges[y0:y1, x0:x1] > 0).astype(np.float32)
    n = count_nonzero(template);  n == 0 → None
3.  w = self.window_px
    sx0, sy0 = max(0, x0 - w), max(0, y0 - w)
    sx1, sy1 = min(width, x1 + w), min(height, y1 + w)
4.  background = np.where(observed_edges > 0, 0, 255).astype(np.uint8)
    distance = cv2.distanceTransform(background, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    search = (np.minimum(distance, float(w)) ** 2)[sy0:sy1, sx0:sx1].astype(np.float32)
5.  result = cv2.matchTemplate(search, template, cv2.TM_CCORR)
    _, _, min_loc, _ = cv2.minMaxLoc(result)
    cx, cy = min_loc                          # (col, row)
6.  境界ガード: rh, rw = result.shape
    if not (0 < cx < rw - 1 and 0 < cy < rh - 1): return None
7.  p = result[cy-1:cy+2, cx-1:cx+2].astype(np.float64)     # p[dy+1, dx+1]
8.  sharpness（下記「二次形式の当てはめ」）; < min_sharpness → None
9.  サブピクセル補間（下記）→ dsx, dsy, cstar
10. offset_px = Point2d(x=(sx0 - x0) + cx + dsx, y=(sy0 - y0) + cy + dsy)
    return EdgeMatch(
        offset=Offset(px=offset_px, pixel_per_mm=self._pixel_per_mm),
        rms_distance_px=math.sqrt(max(cstar, 0.0) / n),
        sharpness=sharpness,
    )
```

**二次形式の当てはめ（3×3 の 6 項最小二乗、閉形式）**

モデル `c(x, y) = a0 + a1 x + a2 y + a3 x² + a4 y² + a5 xy`、`x, y ∈ {−1, 0, 1}`。
3×3 格子上では `(1, x², y²)` だけが結合するので、その 3×3 正規方程式
`[[9,6,6],[6,6,4],[6,4,6]]` を解いた閉形式が使える:

```
S0  = p.sum()
Sx2 = p[:, 0].sum() + p[:, 2].sum()          # Σ x² c
Sy2 = p[0, :].sum() + p[2, :].sum()          # Σ y² c
Sxy = p[0, 0] - p[0, 2] - p[2, 0] + p[2, 2]  # Σ xy c

a3 = -S0 / 3 + Sx2 / 2
a4 = -S0 / 3 + Sy2 / 2
a5 = Sxy / 4

Hxx, Hyy, Hxy = 2 * a3, 2 * a4, a5           # c ≈ c0 + gᵀu + ½ uᵀ H u
lambda_min = (Hxx + Hyy) / 2 - hypot((Hxx - Hyy) / 2, Hxy)
sharpness  = sqrt(max(lambda_min, 0.0) / (2 * n))
```

- **正規化と単位**: コスト `c` は「template 画素の chamfer 距離の二乗和」なので
  `c / n` が平均二乗距離 [px²]、そのヘッセが `H / n` [無次元]。弱軸方向へ 1px ずらしたときの
  平均二乗距離の増分は `½ · λ_min(H) / n` なので、その平方根 `sqrt(λ_min(H) / (2n))` を
  **RMS 距離の増分 [px]** として `sharpness` と定義する。
- 理論上の対応: 想定エッジが線分長 `L_i`・単位法線 `n_i` の集まりなら `H = 2 Σ L_i n_i n_iᵀ = 2A`、
  `n ≈ Σ L_i` なので `sharpness ≈ sqrt(λ_min(A) / Σ L_i)` = 「弱軸を拘束しているエッジ長の割合の平方根」。
  等方な正方リングで `sqrt(1/2) = 0.707`、一方向のみで 0。`AlignmentRegion` の
  `constraint` / `edge_length_px` と同じ量なので、幾何で予測した値と照合後の実測値が比較できる。
- `a3` の検算: `c = x²` を入れると `S0 = 6, Sx2 = 6` → `a3 = -2 + 3 = 1`、`Sy2 = 4` → `a4 = -2 + 2 = 0`。

**サブピクセル補間（3 点放物線、x/y 独立）**

```
cxm, c0, cxp = p[1, 0], p[1, 1], p[1, 2]
cym,     cyp = p[0, 1], p[2, 1]
denx = cxm - 2 * c0 + cxp
deny = cym - 2 * c0 + cyp
dsx = clip((cxm - cxp) / (2 * denx), -0.5, 0.5) if denx > 0 else 0.0
dsy = clip((cym - cyp) / (2 * deny), -0.5, 0.5) if deny > 0 else 0.0
cstar = c0 - 0.25 * ((cxm - cxp) * dsx + (cym - cyp) * dsy)
```

ガードの決定（3 つとも意図的）:

| 状況 | 扱い | 理由 |
|---|---|---|
| `min_loc` が窓端（0 または `2w`） | **`match` が `None`** | 真の最小が探索窓の外。3×3 も取れない。`max_correction`(1.0mm) < `search_window`(1.4mm) なので正常な照合はここに来ない |
| `denx <= 0`（凸でない／潰れた） | その軸の `dsx = 0`（整数に戻す） | 補間が発散する。もう一方の軸は生かす |
| 補間量が ±0.5px 超 | **±0.5 にクランプ**（整数へ戻さない） | 3 点放物線の頂点が隣接セルへ出るのは当てはめ誤差なので、最も近い妥当値へ寄せる |

`cstar` はクランプ後の `dsx/dsy` で計算する（クランプで補正量が減る＝`cstar` が大きくなる方向なので
過小評価にならない）。`max(cstar, 0.0)` は FFT 丸めの負値（実測 −8.9e-08）対策。

削除: `crop_size` 引数・`self._crop_size`・`_template_rect`・`roi=None` 経路。

#### 2. `src/pcbasm/posctrl/region.py`（新規）

```
plan_alignment_regions:
  0. 引数検証（ValueError）: region_size_px < 1 / count < 1 / pad_centers 空 /
     region_size_px > min(image_size)

  1. 参照アンカー a0 = board_transform.apply(pad_centers の bbox 中心)
     matrix, shift = projector.board_to_pixel_affine(a0)
     ppm_board = hypot(matrix[0, 0], matrix[1, 0])      # board 1mm の pixel 長
     region_mm = region_size_px / ppm_board

  2. 全 ring（exterior + interiors）を 1 回だけ pixel 空間へ:
     px = np.asarray(ring.coords) @ matrix.T + shift
     P0 = px[:-1], P1 = px[1:]  を全 ring 分 concat
     D = P1 - P0 ; L = hypot(D) ; L > 1e-9 のものだけ残す
     U = D / L[:, None] ; N = stack([-U[:,1], U[:,0]], axis=1)   # 単位法線

  3. 候補格子（board 座標）:
     step = region_mm / 2
     nx = max(1, ceil((xmax - xmin) / step) + 1)     # ny も同様
     gx = linspace(xmin, xmax, nx)  （nx == 1 のときは [(xmin + xmax) / 2]）
     候補 = gx × gy の直積

  4. 各候補 b について:
     c = np.array([b.x, b.y]) @ matrix.T + shift        # 参照フレームでの ROI 中心 px
     w_i = Liang-Barsky で [cx ± region_size_px/2] × [cy ± region_size_px/2] に
           クリップした線分長（下記）
     A = np.einsum("i,ij,ik->jk", w_i, N, N)
     constraint = float(np.linalg.eigvalsh(A)[0])
     edge_length_px = float(w_i.sum())

  5. constraint <= 0 を捨て、constraint 降順にソート
  6. 貪欲選択: 既選択すべてと board 座標のユークリッド距離 >= region_mm なら採用。
     count 個に達するか候補が尽きたら終了
  7. anchor = board_transform.apply(b)
     roi = centered_roi(image_size, region_size_px)     # 全 region で同一
  8. sort_by_nearest(選択列, tour_start.to3d(), key=anchor.to3d()) で巡回順に並べ、
     その順で index を 0 から振り直して AlignmentRegion を構築
```

**Liang-Barsky（ベクトル化、軸平行矩形）** — 全線分に対し `t0 = 0, t1 = 1` から始め、
軸ごと・境界ごとに 4 回

```
for axis, lo, hi in ((0, cx - half, cx + half), (1, cy - half, cy + half)):
    d = D[:, axis]
    for pcoef, qcoef in ((-d, P0[:, axis] - lo), (d, hi - P0[:, axis])):
        zero = pcoef == 0
        r = np.zeros(len(L)); np.divide(qcoef, pcoef, out=r, where=~zero)
        t0 = np.where((~zero) & (pcoef < 0), np.maximum(t0, r), t0)
        t1 = np.where((~zero) & (pcoef > 0), np.minimum(t1, r), t1)
        t1 = np.where(zero & (qcoef < 0), -1.0, t1)      # 矩形の外で平行 → 棄却
clipped = np.maximum(t1 - t0, 0.0) * L
```

**設計判断（迷ったら理由はここ）**

- **ROI 中心を pixel 空間で決める**: `board_transform` に回転があると board 空間の正方形は
  画像上で正方形にならない。照合 ROI は画像上の正方形なので採点も pixel 空間で行う。
- **アフィンを 1 回しか取らない**: `pixel_of(b, s) = center + ppm·R(s − T_b(b))` は
  `b` について線形で、`s` は平行移動しか動かさない。参照アンカー a0 のフレームで
  「board 点 b の pixel 位置」を出せば、実際に `anchor = T_b(b)` へ移動したとき
  b は画像中心に来るので、参照フレームの ROI と実撮像時の中心 ROI は同じ銅箔を切り出す。
- **格子間隔は `region_mm / 2`**（半分重ね）: 貪欲に選択の自由度を残す。
  実 PCB で 70 候補・0.04 秒なので刻みを細かくするコストは問題にならない。
- **貪欲の最小分離はユークリッド `>= region_mm`**（board mm）。斜め方向はわずかに重なり得るが、
  重なりは平均の相関を弱めるだけで正しさには影響しない。Chebyshev による厳密な非重複判定は
  board_transform の回転を跨ぐと煩雑なので採らない。
- **不足時は例外を投げない**: `min_regions` の判定は計測失敗も込みで数える必要があるので
  `measure_regions` に一本化する。

#### 3. `src/pcbasm/posctrl/aligner.py`（`git mv pad.py aligner.py` 相当。中身は全面置換）

`RegionAligner.measure(region)` の手順:

```
1. klipper.send_gcode(
       stage.move(x=region.anchor.x, y=region.anchor.y, speed=Speed.rate(0.5))
       + gcode.wait(settle_time) + gcode.wait_for_done())
2. projection = projector.project(region.anchor)
3. image = camera.capture()
   edges = edge_detector.detect_edges(image)
4. frame_sink があれば
   frame_sink(render_edge_match(image, edges, projection.edge_mask, region.roi))
5. match = matcher.match(edges, projection.edge_mask, region.roi)
   None → RuntimeError(f"領域 {region.index} の銅箔エッジ照合に失敗しました"
                       "（拘束不足または観測エッジなし）")
6. norm = match.offset.mm.norm
   max_correction_mm を超えたら RuntimeError（現行 CopperPadObserver と同じ文言）
7. observed_at = stage.get_position().to2d()
8. machine_transform = to_machine_transform(
       match.camera_transform, offset_transform,
       projection_anchor=region.anchor, observed_at=observed_at)
9. return RegionAlignment(region=region, match=match, machine_transform=machine_transform)
```

- `correction.py` は**呼ぶだけで一切変更しない**。`observed_at` は必ず
  「その撮像を撮った実ステージ位置」＝ `stage.get_position().to2d()` を渡す。
  移動後に補正しないので `observed_at ≈ anchor` になり `M = Shift(−R(d))`（純並進）になる。
- 収束ループは持たない（`XYPositionAdjustor` を使わない）。
- `board_transform` は不要になったので引数から落とす（anchor は region が持つ）。

#### 4. `src/pcbasm/posctrl/alignment.py`

- `sorted_top_component_pads` / `ComponentAlignments` / `PadAlignmentSession` を削除
- `BoardAlignment`:
  - `translation` = `Point2d(mean(dx), mean(dy))`（`r.translation` の平均）
  - `machine_transform` = `Shift.from_point(self.translation)`
  - `spread` = `Point2d(np.std(dxs), np.std(dys))`（`ddof=0`）
  - `__attrs_post_init__` で `results` 空を `ValueError`
- `RegionAlignmentSession.__init__`:
  - `pad_align = result.machine.paste_dispenser.pad_align`
  - `self._pcb = result.pcb` / `self._stage = result.stage` を保持（`plan_regions` 用）
  - `matcher = CopperEdgeMatcher(pixel_per_mm, search_window_mm=pad_align.search_window,
    min_sharpness=pad_align.min_sharpness)`
  - `self._region_roi = centered_roi(self._image_size, pad_align.region_size_px)`
  - 検証: `pad_align.region_size_px + 2 * matcher.window_px > min(resolution)` なら
    `ValueError`（「region_size_px … + 探索窓 … がキャリブレーション解像度 … に収まりません」）
  - `aligner = RegionAligner(...)`（`max_correction_mm=pad_align.max_correction`）
  - **`tolerance` の √2px 警告ブロックと `import math` は削除**
- `plan_regions()`:
  ```
  pad_centers = [p.center for p in self._pcb.pads if p.layer == Layer.TOP]
  return plan_alignment_regions(
      self._projector, pad_centers, self._board_transform,
      region_size_px=..., count=pad_align.region_count,
      image_size=self._image_size,
      tour_start=self._stage.get_position().to2d())
  ```
- `measure(region)`: `try: return self._aligner.measure(region)` /
  `except RuntimeError as exc: logger.warning("領域 %d の照合に失敗: %s", region.index, exc); return None`
- `corrected_projector` / `projector` / `edge_detector` は現行のまま。`region_roi` を追加

#### 5. `src/pcbasm/posctrl/render.py`

- `PadResultRenderer.__init__` を新シグネチャへ。`roi_polygons` 分岐と `projector.roi_of`
  呼び出しを削除し、渡された `roi` をそのまま使う
- `_centered_roi` を削除（`copper.centered_roi` へ）。`import math` と
  `from pcbasm.config import PadAlign` も削除

#### 6. `src/pcbasm/posctrl/__init__.py`

| 操作 | シンボル |
|---|---|
| 削除 | `ComponentAlignments`, `ComponentPads`, `CopperPadObserver`, `PadAligner`, `PadAlignmentResult`, `PadAlignmentSession`, `group_pads_by_component`, `sorted_top_component_pads` |
| 追加 | `AlignmentRegion`, `BoardAlignment`, `RegionAlignment`, `RegionAligner`, `RegionAlignmentSession`, `plan_alignment_regions`, `centered_roi` |
| 維持 | `BoardCalibrationResult`, `BoardTransformMeasurer`, `CircleDetectionError`, `CopperEdgeMatcher`, `CopperProjection`, `CopperProjector`, `EdgeMatch`, `OffsetObserver`, `OffsetTransformMeasurer`, `OrthogonalityMetrics`, `PadResultRenderer`, `PixelRect`, `XYPositionAdjustor`, `display_at_point`, `interactive_display_at_point`, `machine_session`, `render_edge_match`, `render_label`, `setup_board_calibration`, `to_machine_transform`, `wait_for_keypress`, `window_sink` |

`from .pad import ...` → `from .aligner import RegionAligner, RegionAlignment`、
`from .region import AlignmentRegion, plan_alignment_regions` を追加。`__all__` はアルファベット順を維持。

#### 7. `src/pcbasm/config.py`

上記 `PadAlign` へ置換。`__attrs_post_init__` は `max_failures` 検証を捨て、
`region_size_px` / `region_count` / `min_regions` の 1 以上（`bool` は弾く）と
`min_sharpness >= 0` を検証する。

#### 8. `src/webui/config_store.py`

`FieldSpec` リスト（96〜106 行）を差し替え:

```python
    # [paste_dispenser.pad_align]
    FieldSpec("paste_dispenser.pad_align.max_correction", "最大補正量", "float", "mm"),
    FieldSpec("paste_dispenser.pad_align.search_window", "探索窓 片側幅", "float", "mm"),
    FieldSpec("paste_dispenser.pad_align.region_size_px", "照合領域の一辺", "int", "px"),
    FieldSpec("paste_dispenser.pad_align.region_count", "照合領域数", "int"),
    FieldSpec("paste_dispenser.pad_align.min_regions", "必要な成功領域数", "int"),
    FieldSpec("paste_dispenser.pad_align.min_sharpness", "拘束の下限", "float", "px"),
    FieldSpec("paste_dispenser.pad_align.canny_low", "Canny下側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.canny_high", "Canny上側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.blur_ksize", "ブラーカーネルサイズ", "int"),
```

`_coerce`（201 行付近）の `max_failures` 検証を差し替え:

```python
        case "int":
            ...
            if spec.key in (
                "paste_dispenser.pad_align.region_size_px",
                "paste_dispenser.pad_align.region_count",
                "paste_dispenser.pad_align.min_regions",
            ) and value < 1:
                raise UnknownFieldError(f"{spec.key}: 1以上の値が必要です")
```

`case "float"` に `min_sharpness < 0.0` → `UnknownFieldError(f"{spec.key}: 0以上の値が必要です")` を追加。

`src/webui/routers/common.py:144` のセクションラベルは変更しない（キーは `pad_align` のまま）。

#### 9. `src/webui/jobs/board_ops.py`

`align_component_groups` / `pad_align_abort_message` を削除し `measure_regions` を置く。
`ctx.progress` / `ctx.checkpoint` / `ctx.log` / `on_failure` の契約は現行を踏襲する。

```
if len(regions) < min_regions:
    raise ValueError(f"照合領域を {len(regions)} 個しか計画できませんでした"
                     f"（必要 {min_regions}）。region_size_px を小さくするか "
                     f"region_count を見直してください")
results = []
for region in regions:
    ctx.progress("銅箔照合", 100.0 * region.index / len(regions))
    ctx.checkpoint()
    alignment = session.measure(region)
    if alignment is None:
        ctx.log(f"警告: 領域 {region.index + 1}/{len(regions)} の照合に失敗")
        if on_failure is not None:
            on_failure(region)
        continue
    t, m = alignment.translation, alignment.match
    ctx.log(f"領域 {region.index + 1}/{len(regions)}: "
            f"dx={t.x:+.4f} dy={t.y:+.4f} mm, "
            f"rms={m.rms_distance_px:.2f} px, sharpness={m.sharpness:.3f}")
    results.append(alignment)
if len(results) < min_regions:
    raise ValueError(f"銅箔照合に成功した領域が不足しています"
                     f"（成功 {len(results)} / 必要 {min_regions} / 計画 {len(regions)}）。"
                     f"基板の向き・種類と照明・Canny 閾値を確認してください")
board = BoardAlignment(results=tuple(results))
ctx.log(f"平均補正: dx={board.translation.x:+.4f} dy={board.translation.y:+.4f} mm / "
        f"領域間ばらつき: sx={board.spread.x:.4f} sy={board.spread.y:.4f} mm")
return board
```

#### 10. `src/webui/jobs/posctrl.py` — `_run_board_tour`

import: `ComponentPads` / `PadAlignmentResult` / `PadAlignmentSession` /
`sorted_top_component_pads` / `align_component_groups` を落とし、
`AlignmentRegion` / `BoardAlignment` / `RegionAlignmentSession` / `measure_regions` を入れる。
`Layer` は `_orthogonality_points` で既に使っているのでそのまま。

`_pad_renderer`（362-377 行）:

```python
def _pad_renderer(
    session: RegionAlignmentSession,
    projector: CopperProjector,
    paste_polygons: Sequence[Polygon],
    position: Point2d,
) -> PadResultRenderer:
    return PadResultRenderer(
        projector=projector,
        edge_detector=session.edge_detector,
        roi=session.region_roi,
        paste_polygons=paste_polygons,
        position=position,
    )
```

（`result` 引数が不要になる＝`pad_align` を読まなくなる。`shapely.Polygon` の import 追加）

`_run_board_tour` の銅箔照合ブロック（448-469 行）:

```python
        session = RegionAlignmentSession.from_calibration(result, frame_sink=ctx.frame)
        regions = session.plan_regions()
        ctx.log(f"照合領域数: {len(regions)}")
        for region in regions:
            ctx.log(f"領域 {region.index + 1}: anchor=({region.anchor.x:.2f}, "
                    f"{region.anchor.y:.2f}) mm, 予測 sharpness="
                    f"{math.sqrt(region.constraint / region.edge_length_px):.3f}")

        def render_failed(region: AlignmentRegion) -> None:
            renderer = _pad_renderer(
                session, session.projector, [], result.stage.get_position().to2d()
            )
            _stream_pad_result(ctx, result, renderer,
                               [f"Region {region.index + 1}/{len(regions)}", "FAILED"])

        alignment = measure_regions(
            ctx, session, regions, min_regions=1, on_failure=render_failed
        )
```

- **`min_regions=1`** にする理由: `board_tour` は現行 `max_failures=None`（無制限）だった。
  `BoardAlignment` が空を許さないので下限 1 が必要だが、それ以上厳しくすると診断ツールとしての
  用途（FAILED overlay を見る）が壊れる。塗布の厳しさは `paste_solder` 側の `pad_align.min_regions`
  が担う。
- `math` は既に import されていないので追加が必要（`posctrl.py` は現在 `math` を import していない）。

補正巡回（471-513 行）:

```python
def _corrected_entries(
    result: BoardCalibrationResult,
    session: RegionAlignmentSession,
    alignment: BoardAlignment,
) -> tuple[CopperProjector, list[tuple[Pad, Point2d]]]:
    """平均補正を適用した全 TOP pad の巡回先を nearest neighbor 順で構築する."""
    corrected_transform = Compose([result.board_transform, alignment.machine_transform])
    projector = session.corrected_projector(alignment.machine_transform)
    entries = [
        (pad, corrected_transform.apply(pad.center))
        for pad in result.pcb.pads
        if pad.layer == Layer.TOP
    ]
    current = result.stage.get_position()
    return projector, sort_by_nearest(
        entries, current.to2d().to3d(), key=lambda entry: entry[1].to3d()
    )
```

呼び出し側:

```python
        projector, entries = _corrected_entries(result, session, alignment)
        for index, (pad, target) in enumerate(entries):
            ctx.progress("補正巡回", 100.0 * index / len(entries))
            ctx.checkpoint()
            _move_to(result, target, speed=Speed.rate(0.5))
            renderer = _pad_renderer(session, projector, [pad.polygon], target)
            _stream_pad_result(ctx, result, renderer,
                               [f"{pad.designator}.{pad.pad_number} "
                                f"{index + 1}/{len(entries)}"])
```

- 補正が全 pad 共通になったので投影器も 1 つ使い回す（region ごとの投影器生成が消える）
- 巡回対象は「照合成功部品の pad」から **全 TOP pad** へ変わる（全 pad に同じ補正が乗るため）

summary:

```python
    return JobResult(summary=(
        f"照合成功 {len(alignment.results)}/{len(regions)} 領域 / "
        f"平均補正 dx={alignment.translation.x:+.4f} dy={alignment.translation.y:+.4f} mm"
        f"（ばらつき {alignment.spread.x:.4f}, {alignment.spread.y:.4f} mm）/ "
        f"補正巡回 {len(entries)} pads"
    ))
```

#### 11. `src/webui/jobs/pasting.py` — `_run_paste_solder`

import 差分: `ComponentAlignments` / `PadAlignmentSession` / `sorted_top_component_pads`
（69-73 行）と `align_component_groups`（81 行）と `transform_polygon`（26 行、
このファイル唯一の利用が 875 行なので import ごと削除）を落とし、
`RegionAlignmentSession` と `measure_regions` を入れる。
**`XYPositionAdjustor`（72 行）は残す** — 1911 行のツールヘッドオフセット較正で使っている。

826-853 行を置換:

```python
        # 銅箔照合（領域単位）。基板全体の平均並進を 1 つ求める。
        # 成功領域が pad_align.min_regions を下回ったら即中止。
        align_session = RegionAlignmentSession.from_calibration(
            result, frame_sink=ctx.frame
        )
        regions = align_session.plan_regions()
        ctx.log(f"照合領域数: {len(regions)}")
        alignment = measure_regions(
            ctx,
            align_session,
            regions,
            min_regions=session.machine.paste_dispenser.pad_align.min_regions,
        )
        correction = alignment.machine_transform
```

- `align_designators` / `initial_purge.pad.designator` を照合対象へ足す分岐は**丸ごと消える**
  （領域は幾何で決まり pad の有効/無効と無関係）。

863-882 行を置換:

```python
        # 順路順の (polygon, ResolvedPaste) ペア。pad polygon は board 座標のまま渡し、
        # 補正は機械座標へ出る瞬間（transform）に 1 回だけ掛ける
        pairs: list[tuple[Polygon, ResolvedPaste | None]] = [
            (pad.polygon, resolved.get(hierarchy.pad_ref_for_pad(pad)))
            for pad in routed_pads
        ]
        stage = session.stage

        # board→machine 全変換（平均補正は toolhead_offset の前）
        transform = Compose([
            session.board_transform, correction, session.toolhead_offset, height_plane
        ])
```

- `correction` は **`toolhead_offset` の前**。M はカメラ機械座標系で定義されているため。
  `height_plane` の定義域はノズル機械 XY なので最後尾のままで正しい
  （`pasting/height.py:47` が `Compose([board_transform, toolhead_offset])` で probe 点を作る）。
  純並進 0.1mm 級の挿入で高さ面の評価点がずれる影響は無視できる。
- 「未照合 pad は無補正」の警告 log と分岐が消える（全 pad に同一補正）。

896-901 行の初回パージ: `initial_purge_point` を廃止し `initial_purge.pad.center`
（board 座標）を直接渡す。`_initial_purge_point`（932-944 行）を**関数ごと削除**。

```python
            if initial_purge is not None:
                ctx.progress("初回パージ")
                ctx.checkpoint()
                applicator.deposit_at(
                    initial_purge.pad.center, amount=initial_purge.amount_ul
                )
```

summary（921-929 行）の `照合成功 {len(aligned)}/{len(groups)} 部品` を
`照合成功 {len(alignment.results)}/{len(regions)} 領域` に置換。

#### 12. 設定テンプレート 2 本

`data/config-templates/kurousagi.paste/machine.toml`（41-50 行）と
`data/testing/config/machine.toml`（38-47 行）の `[paste_dispenser.pad_align]` を:

```toml
[paste_dispenser.pad_align]
# 領域単位の銅箔照合による位置合わせ（基板全体の平均並進を 1 つ求める）
max_correction = 0.3   # 1 照合で許容する最大ずれ [mm]。超過は誤マッチとして照合失敗
search_window = 1.4    # 照合の探索窓 片側幅 [mm]
region_size_px = 400   # 照合領域の一辺 [px]（400 + 2*42 = 484 でフレームに収まる）
region_count = 4       # 計画する照合領域数
min_regions = 3        # 成功が必要な最小領域数。下回ると塗布ジョブを中止
min_sharpness = 0.15   # 拘束不足の棄却閾値 [px]（合成実測: 一方向のみ <= 0.06 / 良好 >= 0.5）
canny_low = 55.0       # ← kurousagi は 55.0 / 104.0 / 3、testing は 81.0 / 192.0 / 5 を維持
canny_high = 104.0
blur_ksize = 3
```

`tolerance` / `roi_margin` / `min_roi` は削除。`config/machine.toml` は gitignore なので触らない
（削除キーは cattrs が無視、新キーは既定値。ユーザーが WebUI 設定画面で調整する旨を MR 説明に書く）。

### `spec-test-author` の担当（`tests/` のみ）

**絶対に変更しない**: `tests/pcbasm/posctrl/test_correction.py`（符号規約 A2/A4 の安全網）。

#### 削除／リネーム

| 現在 | 操作 |
|---|---|
| `tests/pcbasm/posctrl/test_pad.py` | `test_aligner.py` へリネームし全面書き直し |
| `tests/pcbasm/posctrl/test_alignment.py` | 全面書き直し（`ComponentPads` / `PadAligner` / `ComponentAlignments` / `sorted_top_component_pads` のテストを撤去） |
| `tests/pcbasm/posctrl/test_copper.py:239-311` | `roi_of` の 5 テストを削除 |
| `tests/pcbasm/posctrl/test_copper.py:395-` | `crop_size` のテストを削除（`crop_size` 引数が消える） |
| `tests/webui/jobs/test_board_ops.py:75-100` | `pad_align_abort_message` のテストクラスを削除 → `measure_regions` のテストへ |

#### 新規 `tests/pcbasm/posctrl/test_region.py`

- 一方向エッジだけの領域は `constraint ≈ 0`、両方向がある領域は高い（`λ_min` 採点）
- 選ばれた領域が互いに `region_size_px / pixel_per_mm` 以上離れている
- 候補が `count` に満たなければ候補数まで返す。銅箔が無ければ空リスト
- 全 region の `roi` が同一で、画像中心の `region_size_px` 正方形
- 引数不正（`region_size_px < 1` / `count < 1` / `pad_centers` 空 /
  `region_size_px > min(image_size)`）で `ValueError`
- `board_transform` に回転が入っても ROI は pixel 空間の正方形になる

#### `tests/pcbasm/posctrl/test_copper.py` の追加（**今回のバグの直接ピン。最重要**）

1. **開口問題の棄却**: 観測が視野を縦断する水平線・想定が水平線のみの template で
   `match` が **`None`** を返す（実測 sharpness 0.0000、旧実装は dx=+38px のデタラメを返した）
2. **サブピクセル復元**: `cv2.circle(..., shift=4)` で 1/16px 精度に描いた円 4 個を
   `(dx, dy) = (+2.4, −1.2)` などの非整数量ずらし、`abs=0.15` px で復元する
3. **0 飽和の消滅**: サブピクセルずれで `rms_distance_px > 0.2`（実測 0.485〜0.520）
4. **完全一致**: `rms_distance_px == pytest.approx(0.0, abs=1e-3)`、かつ **負値にならない**
5. **`sharpness` の順序**: 正方リング（実測 0.7059）> 細長リング 240×8（0.1758）>
   単一水平線（0.0576）。境界に近い値は使わず、`> 0.5` と `< 0.1` で切る
6. **`min_sharpness` の効き**: 同じ入力で `min_sharpness=0.0` なら `EdgeMatch`、
   `min_sharpness=0.9` なら `None`
7. **窓端ガード**: 探索窓を超えるずれ（例 `search_window` の 1.5 倍）を与えると `None`
8. **`centered_roi`**: 画像中心・偶奇両方のサイズ・`size_px > min(image_size)` のクランプ・
   `size_px < 1` の `ValueError`

#### `tests/pcbasm/vision/test_copper.py`（既存ファイルへ追加）— エッジ検出の前提ピン

`CopperEdgeDetector.detect_edges` の出力が 1px 細線であること
（`2×2` が全て非零になるブロックが 0 個）。将来 `dilate` が入ると照合が壊れるため。

#### `tests/pcbasm/posctrl/test_aligner.py`（新規）

- `RegionAligner.measure` が anchor へ移動し、1 回だけ撮像する（収束ループがない）
- **`machine_transform` が純並進でレバー腕に依存しない**（MR !149 のピンを新型へ移植）:
  異なる `region.anchor` でも同じ観測ずれなら `translation` が一致する
- 照合失敗（matcher が `None`）で `RuntimeError`
- `max_correction_mm` 超過で `RuntimeError`
- `frame_sink` に 1 フレーム届く

#### `tests/pcbasm/posctrl/test_alignment.py`（書き直し）

- `BoardAlignment.translation` が各領域の平均、`spread` が母標準偏差、1 領域で `spread == (0, 0)`
- `BoardAlignment(results=())` で `ValueError`
- `machine_transform` が `Shift(translation)` と一致
- `RegionAlignmentSession.__init__` が `region_size_px + 2*window_px > 解像度` で `ValueError`
- `RegionAlignmentSession.measure` が `RuntimeError` を握って `None` を返す
- `region_roi` が `centered_roi(resolution, region_size_px)` と一致

#### `tests/pcbasm/posctrl/test_render.py`

`PadResultRenderer` の新シグネチャ（`roi` 直接指定）へ。
`test_empty_roi_polygons_falls_back_to_min_roi`（227 行）は前提が消えるので削除。

#### `tests/webui/jobs/test_board_ops.py`

`measure_regions` のテストへ:
- 全成功で `BoardAlignment` が返り、`ctx.log` に dx/dy/rms/sharpness と平均・ばらつきが出る
- 失敗領域では `on_failure` が呼ばれ、続行する
- 成功数 < `min_regions` で `ValueError`
- 計画領域数 < `min_regions` なら `session.measure` を 1 度も呼ばずに `ValueError`

#### 設定まわりの同期

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/test_config.py:98-127` | `tolerance` / `min_roi` の参照を新キーへ。既定値 `PadAlign()` の確認 |
| `tests/pcbasm/test_config.py:255-276` | `max_failures` のクラスを `region_size_px` / `region_count` / `min_regions` / `min_sharpness` の検証へ（1 未満・負値で `ValueError`） |
| `tests/webui/test_config_store.py:361-388` | `max_failures` の読み書きを `region_count` へ（int 型・1 未満で拒否） |
| `tests/webui/routers/test_settings_api.py:200-230` | 同上 |
| `tests/e2e/test_webui_e2e.py:368-396` | `TestPadAlignMaxFailuresOverRealHttp` を `region_count` 版へ（クラス名も改名） |
| `tests/webui/test_preview.py:177` / `tests/webui/routers/test_pages.py:312` | canny 系のみ参照。**変更不要**（確認だけ） |

---

## テスト観点

- **正常系**: 両方向に拘束のある領域でサブピクセル並進を 0.15px 以内で復元 / 複数領域の平均と
  ばらつき / 全 pad に同一補正が乗る / `transform` の合成順（`board → correction → toolhead → height`）
- **異常系**: 開口問題領域の `None` 棄却 / 観測エッジなし / 想定エッジなし / 窓端張り付き /
  `max_correction` 超過 / 成功領域数不足の中止 / 計画領域数不足の中止 / `BoardAlignment` 空
- **エッジケース**: pad bbox が 1 領域より小さい（候補 1 個）/ 銅箔ゼロ（候補 0 個）/
  `region_size_px` がフレームより大きい / 補間の分母 0 / 補間量 ±0.5px 超のクランプ /
  コストの FFT 負値 / `board_transform` に回転がある場合の ROI

---

## 想定リスク・トレードオフ

- **`min_sharpness = 0.15` は採択側の下限 0.1758 と 17% しか離れていない**。実 PCB の 400px 領域は
  0.63〜0.70 なので実害はないが、`region_size_px` を大きく下げると境界に近づく。実機で
  `sharpness` の実測が 0.2 台に出るようなら閾値を下げる（ログに毎回出す設計にしてある）。
- **サブピクセルは 0.11px の pixel-locking バイアスが残る**。放物線当てはめの既知の性質で、
  0.033mm 相当。今回の目標（0.1mm のずれを潰す）には十分だが、テストで 0.1px を切る期待値は書けない。
- **平均並進で直らない誤差**（board キャリブレーションの回転・スケール、基板の反り）は
  `spread` に出る。実機で `spread` が大きければ剛体フィットを検討する — 今回は入れない。
- **`board_tour` の巡回対象が全 TOP pad に増える**。補正が全 pad 共通になった以上これが正しいが、
  pad 数の多い基板では巡回時間が伸びる。
- **`pad.py` → `aligner.py` のリネーム**は上位計画に明記がない（上位計画は「pad.py を作り直す」）。
  pad 単位の概念が全て消えるのでファイル名だけ残すと誤解を招くと判断した。orchestrator が
  不要と判断するなら `pad.py` のままでも実装内容は変わらない（テスト側のファイル名も連動）。
- **`CopperProjector` に public を 2 つ足す**（`board_to_pixel_affine` / `polygons`）が、
  `roi_of` を削るので公開面の総量は減る。代案（`plan_alignment_regions` に polygons を
  別引数で渡す）は projector と polygons の二重渡しになるので採らない。
- **`plan_alignment_regions` に `tour_start` 引数を足した**（上位計画のシグネチャにはない）。
  純関数のまま `sort_by_nearest` で巡回順に並べ、その順で `index` を振るために必要。

---

## 確認事項（orchestrator 経由）

1. **`board_tour` の `min_regions` を 1 に固定してよいか。** 現行は `max_failures=None`（無制限）で、
   診断ツールとして「失敗も含めて全領域を見る」用途だった。`BoardAlignment` が空を許さないため
   下限 1 は必須。暫定案は `min_regions=1` 固定（設定値は使わない）。
   代案は `pad_align.min_regions` をそのまま使い、巡回も塗布と同じ厳しさにする。
2. **`pad.py` → `aligner.py` のリネームを認めるか**（上記トレードオフ参照）。
   暫定案はリネームする。No なら `pad.py` のまま中身だけ差し替え、
   `tests/pcbasm/posctrl/test_pad.py` も名前を維持する。

---

## 並列実行の分担

| agent | 触るファイル |
|---|---|
| `plan-implementer` | `src/pcbasm/posctrl/{copper,region,aligner,alignment,render,__init__}.py`, `src/pcbasm/config.py`, `src/webui/{config_store}.py`, `src/webui/jobs/{board_ops,posctrl,pasting}.py`, `data/config-templates/kurousagi.paste/machine.toml`, `data/testing/config/machine.toml` |
| `spec-test-author` | `tests/pcbasm/posctrl/{test_copper,test_region,test_aligner,test_alignment,test_render}.py`, `tests/pcbasm/vision/test_copper.py`, `tests/pcbasm/test_config.py`, `tests/webui/{test_config_store}.py`, `tests/webui/jobs/test_board_ops.py`, `tests/webui/routers/test_settings_api.py`, `tests/e2e/test_webui_e2e.py` |
| どちらも触らない | `src/pcbasm/posctrl/correction.py`, `src/pcbasm/posctrl/position.py`, `tests/pcbasm/posctrl/test_correction.py`, `src/pcbasm/posctrl/README.md`（`code-simplifier` が同期） |

---

## 検証

```
make format && make type && make test-no-hardware
make test-e2e
grep -rn 'tolerance\|roi_margin\|min_roi\|max_failures\|mean_distance\|roi_of\|crop_size' \
     src/pcbasm/posctrl/ --include=*.py
grep -rn '</content>' src tests           # サブエージェント Write の既知事故
```

`pad_align` 由来の残骸がないこと（board キャリブレーションの `tolerance` と
カメラの `crop_size` は別物なので残る）。**実機テスト（`make test` / `@mark_hardware`）は実行しない。**

## 参照

- 上位計画: `/home/gop/.claude/plans/claude-maximize-parallels-majestic-pike.md`
- 符号規約: `src/pcbasm/posctrl/correction.py`（変更禁止）
- 前タスクのメモ: `memory/agents/implementation-planner/{copper-alignment,pad-alignment,paste-align-max-failures}.md`
- skill: `testing-strategy`, `refactor-conventions`
- 実測スクリプト: `/tmp/claude-1000/-home-gop-pcb-assembly/b495f7b4-e830-4365-8865-c0a78c6d65d3/scratchpad/exp{_sharpness,2,3,4,5}.py`
