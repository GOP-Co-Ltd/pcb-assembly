# 銅箔照合の補正モデルを「平均並進」から「アフィン最小二乗」へ作り直す

ブランチ: `feature/20260729/region-alignment-average`（MR !151）の上に積む。
前ラウンドの計画: `memory/agents/implementation-planner/region-alignment-average.md`（数式・符号規約はそちらが正典。本書は差分）。

## 概要

区ごとに測った変位 `d_i` から基板全体の **6 自由度アフィン**（並進 + 回転 + スケール + スキュー）を
最小二乗で 1 つ求め、機械座標へ出る最後の 1 回だけ適用する。領域計画は重なりなしのタイル張りへ
単純化し（貪欲選択・最小離間・`region_count` を撤去）、塗布対象 pad を含む区だけを条件を満たす限り
全部使う。区ごとの残差を公開・ログ出力し、「残差 ≈ 照合ノイズならアフィンで足りている / 数倍なら
非線形なひずみが残っている」をユーザーが実機で判定できるようにする。

## 実測（すべて本タスクで測定。テスト期待値と既定値の根拠）

環境 numpy 2.2.6 / OpenCV 4.12.0、実 PCB `data/TJ-56-67/TJ-56-67.kicad_pcb`（89.5x58mm、TOP 銅箔 48・
TOP pad 48、pad の分布は x [3.0, 87.2] / **y [2.4, 28.5]** = 基板下半分に偏在）、
`pixel_per_mm=30.225` / `image_size=(1280,720)` / `board_edge_margin=2.0` / `search_window=1.4` /
`min_sharpness=0.15` / board 変換は回転 0.5°。スクリプトは
`/tmp/claude-1000/-home-gop-pcb-assembly/b495f7b4-e830-4365-8865-c0a78c6d65d3/scratchpad/exp_{tile,tile2,iter,iter2,fit,fit2,e2e}.py`。

### 1. アフィンにする効果（本タスクの存在理由）

3 点法の基準点合わせを「TL/TR/BL それぞれ 50um の目視誤差」としてモデル化すると、pad 群に残る
位置誤差は中央値 73um・p95 150um。これを補正した後の**全 pad での残存誤差**（MC 500 回、
区あたり照合ノイズ 5um）:

| 配置 | n | アンカー広がり | アフィン med/p95 | 平均並進 med/p95 |
|---|---|---|---|---|
| 6 区 (400px) | 6 | 6.6mm | **7.8 / 13.4 um** | 46.3 / 92.4 um |
| 11 区 (200px) | 11 | 5.2mm | **7.2 / 12.7 um** | 46.5 / 91.3 um |
| 18 区 (150px) | 18 | 4.1mm | **6.4 / 12.1 um** | 47.7 / 91.3 um |

平均並進は 46um を残す = 「ピタリ合う pad と合わない pad が混在する」の実機観察と一致する。
アフィンは 6 倍改善し、区数を 6 から 18 へ増やしても 7.8 → 6.4um しか変わらない
（**区数より模型の次数が効く**）。

### 2. 通し検証（実 PCB・合成観測、`exp_e2e.py`）

真の board 変換に 3 点法誤差（この試行では pad 群最大 206um）を仕込み、タイル計画 → 2 パス計測 →
アフィン当てはめ → 全 48 pad の残存誤差まで通した:

| region_size_px | 計画区数 | 広がり | 残差 RMS | 全 pad 残存誤差 med/max（アフィン） | 同（平均並進） |
|---|---|---|---|---|---|
| 400 | 6 | 4.70mm | 4.0um | **5.8 / 13.3 um** | 76.2 / 114.0 um |
| 300 | 9 | 6.26mm | 3.8um | **2.0 / 3.7 um** | 65.8 / 134.1 um |
| 200 | 11 | 6.09mm | 7.2um | 4.5 / 7.3 um | 61.0 / 138.9 um |
| 150 | 18 | 5.82mm | 6.3um | 1.9 / 4.1 um | 75.6 / 118.9 um |

区ごとの計測誤差は 2.1〜11.7um、全区が 2 パスで収束（pass2 の増分 <= 0.01mm）、
実測 sharpness 0.585〜0.649、`rms_distance_px` 0.26〜0.48。

**`region_size_px=400` では計画区数が 6 で、うち y が違う区は 1 つだけ**（5 区が y=69.58 の 1 行 +
1 区が y=82.81）。ユーザーが想定した「~20 区」にはならない。pad が y 方向に 26mm しか広がって
いないためで、pad を含む区だけを採る条件の直接の帰結。→ 確認事項 1。

### 3. 反復計測: 「移動して再計測」だけでは**二重計上する**（`exp_iter.py` / `exp_iter2.py`）

ステージを動かすと想定投影と観測が画像内で同じだけ動くので、**測れる offset はステージ位置に
不変**。1 パス目の並進を当てたアンカーへ移動して素朴に再計測すると、同じ変位をもう一度測って
足してしまう:

| 真の変位 \|D\| | 1 パス目の誤差 | 投影を補正して再計測（正しい） | 移動だけで再計測（誤り） |
|---|---|---|---|
| 0.02mm | 4.6um | 増分 4.4um → 誤差 **1.6um** | 増分 19.7um → 誤差 **18.4um** |
| 0.10mm | 3.1um | 増分 1.6um → 誤差 **1.4um** | 増分 98.7um → 誤差 **99.1um** |
| 0.30mm | 1.8um | 増分 0.7um → 誤差 **1.4um** | 増分 298.9um → 誤差 **298.2um** |

→ **2 パス目は「累積変位を board 変換の後段に挿した投影器」で投影しなければならない**
（= `Compose([board_transform, Shift(累積)])`）。移動は「実際の銅箔を ROI 中心へ戻す」ためだけの
副次的な効果しかない。

理想モデル（合成観測・エッジ検出なし）では 1 パス目の誤差は既に 1.8〜6.9um で、
`|D|` が 0.02〜0.30mm のどこでも増えない。窓の出入りによる片方向 chamfer の偏りは
この範囲では 7um 未満。**2 パス目の利得は理想モデルでは 2 倍程度で、実機での価値は
照明・Canny・銅箔形状の実誤差に依存する**（ユーザー決定に従い実装するが、実機で
`passes=2` の増分ログが常に小さいなら `max_passes=1` に落として時間を半分にできる）。

### 4. 共線縮退の閾値（`exp_fit2.py`）

アンカーを 1 行に並べ、y のジッタを振って「アンカーの最小主軸方向 RMS 広がり
`spread = σ_min(P_centered) / sqrt(n)`」と残存誤差を対応させた（真の場は 3 点法誤差モデル）:

| spread | アフィン med/p95 | 並進のみ med/p95 |
|---|---|---|
| 0.00mm（厳密に共線） | 23 / 49 um | 50 / 99 um |
| 0.16mm | **259 / 853 um** | 50 / 93 um |
| 0.38mm | 114 / 367 um | 48 / 95 um |
| 0.73mm | 60 / 168 um | 48 / 93 um |
| 1.51mm | 29 / 85 um | 50 / 95 um |
| **2.24mm** | 20 / 53 um | 50 / 97 um |
| 4.40mm | 11 / 31 um | 48 / 89 um |

危険なのは厳密な共線ではなく**準共線**（`lstsq` の最小ノルム解が効かなくなる 0.2〜1mm 帯）で、
並進のみに負けるのは spread < 約 1.5mm。→ **`_MIN_ANCHOR_SPREAD_MM = 2.0`**（両側に余裕を
取った値。2.0mm でもアフィンは並進の 2 倍良い）。厳密共線が偶然マシに見えるのは最小ノルム解の
副作用なので、閾値未満は一律で並進へ縮退させる。

タイル格子ではアンカー間隔が `region_mm`（= `region_size_px / pixel_per_mm`）の整数倍なので、
2 行に分かれていれば spread は `region_mm` のオーダーになる（実測の 5 区 + 1 区という
最も偏った配置で 4.70mm = 0.36 * 13.23mm）。ただし偏りは区数が増えるほど効くので、
`n` 区のうち 1 区だけが別の行にある配置では spread ≈ `sqrt((n-1)/n²) * region_mm`
（n=12 で 0.28、n=20 で 0.22 倍）。`region_size_px=400`（13.2mm）なら 2.6mm 以上を保つが、
`region_size_px=200`（6.6mm）で極端に偏ると 1.3mm まで落ちて並進へ縮退する
— 上表のとおり spread 1.5mm 以下ではアフィンが並進に負けるので、縮退が正しい挙動。

### 5. 残差の解釈（`exp_fit2.py`）

| 真の場 | n=6 | n=11 | n=18 |
|---|---|---|---|
| 純アフィン + ノイズ 5um | 残差 RMS 5.3um | 5.4um | 6.5um |
| 上に 50um の 2 次ひずみを加算 | 16.0um (max 23) | 12.8um (max 19) | 14.0um (max 25) |

→ **残差 RMS が区あたり照合ノイズと同程度ならアフィンで足りている。2〜3 倍を超えたら
非線形なひずみが残っている**（局所補正の検討材料）。ログにこの解釈を 1 行添える。
注意: 区が狭い範囲に固まっていると非線形は局所的にアフィンに見えるので、
`TJ-56-67` の y 方向（26mm）の検出力は弱い。

### 6. スケール成分が塗布量・経路長に与える影響

`correction` に入るスケール偏差は実測で -2435 ppm（3 点法誤差の悪い引き）〜 数百 ppm。
`applicator._fill` は `total_amount = polygon.area * ul_per_mm2`（`pasting/applicator.py:434`）を
**board 座標のポリゴンで**計算し、`self._transform` は `_draw_polyline`（同 470 行）で経路点に
だけ掛かる。したがって吐出量は影響ゼロ、機械座標での経路長が 0.24% 変わるだけ。**対処不要**。

## 公開インターフェース案（確定。ここから逸脱しないこと）

### `src/pcbasm/posctrl/copper.py`

```python
class CopperProjector:
    def with_correction(self, machine_transform: Transform) -> "CopperProjector":
        """機械座標の補正を board 変換の後段へ挿した投影器を返す.

        ``board_transform`` を ``Compose([board_transform, machine_transform])`` に
        差し替えた同設定の投影器。反復計測（累積変位で投影を補正）と
        補正巡回（board_tour）の両方で使う。
        """
```

他は変更なし（`_fit_sharpness` / `_parabolic_subpixel` の数式は**変更禁止**）。

### `src/pcbasm/posctrl/region.py`

`AlignmentRegion` は変更なし（`index` / `anchor` / `roi` / `constraint` / `edge_point_count` /
`predicted_sharpness`）。

```python
def plan_alignment_regions(
    projector: CopperProjector,
    board_transform: Transform,
    pad_centers: Sequence[Point2d],
    *,
    safe_area: Polygon,
    region_size_px: int,
    min_sharpness: float,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """塗布対象 pad を含む区を重なりなしのタイルとして選び、巡回順に並べて返す.

    タイル格子は pixel 空間で一辺 region_size_px、**位相は pad 中心の重心に合わせる**
    （タイル位置はタイル数に依存しないので、格子の丸めで配置が跳ばない）。採否は
    3 条件だけ: (1) ROI 全体が safe_area に収まる、(2) 区内に pad 中心が 1 つ以上ある、
    (3) predicted_sharpness >= min_sharpness。

    Args:
        projector: 設計銅箔の投影器（ポリゴンとアフィンの供給元）
        board_transform: board 座標→機械座標の変換（anchor の算出に使う）
        pad_centers: 塗布対象 pad の中心（board 座標、mm）。空なら領域は 0 個
        safe_area: 照合を許す領域（board 座標、mm）。基板外形を外周マージンだけ
            内側へ縮めたもの。MultiPolygon でも同じに扱える
        region_size_px: 領域の一辺 [px]
        min_sharpness: 予測 sharpness の下限（これ未満の区は移動前に落とす）
        image_size: カメラ画像サイズ (width, height)
        tour_start: 巡回の起点（機械座標、mm）

    Returns:
        0 個以上の AlignmentRegion（巡回順、index は 0 始まりで振り直し。上限なし）。
        不足しても例外は投げない（min_regions の判定は呼び出し側の責務）

    Raises:
        ValueError: region_size_px < 1 / region_size_px > min(image_size)
    """
```

- `count` 引数を削除（上限なし）、`min_sharpness` と `pad_centers` を追加
- `_candidate_grid` と `_GRID_RATIO_TOLERANCE` を**削除**、`_projected_edge_points` はそのまま
- `_Candidate` は `constraint` / `edge_point_count` / `board_xy` のまま使う

### `src/pcbasm/posctrl/aligner.py`

```python
@attrs.frozen
class RegionAlignment:
    """1 領域の照合結果（最大 max_passes 回の反復計測の累積）.

    Attributes:
        region: 対象領域
        match: 最終パスの照合結果
        displacement: 累積変位（観測 − 設計）[mm]、機械座標。
            各パスの補正は純並進なので単純和になる
        passes: 実施したパス数（1 以上 max_passes 以下）
        converged: 最終パスの増分が converge_tolerance 以下だった
    """

    region: AlignmentRegion
    match: EdgeMatch
    displacement: Point2d
    passes: int
    converged: bool


class RegionAligner:
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
        max_passes: int = 2,
        converge_tolerance_mm: float = 0.01,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None: ...

    def measure(self, region: AlignmentRegion) -> RegionAlignment:
        """累積変位を当てたアンカーで最大 max_passes 回まで計測する.

        Raises:
            RuntimeError: いずれかのパスで照合に失敗した場合、または累積変位が
                max_correction_mm を超えた場合
        """
```

- `machine_transform` / `translation` を削除し `displacement` に一本化（アフィン当てはめが
  必要なのは (anchor, displacement) の対だけ。`to_machine_transform` は `measure` の内部で使う）

### `src/pcbasm/posctrl/alignment.py`

```python
type DisplacementModel = Literal["affine", "translation"]

# アンカーの最小主軸方向 RMS 広がりの下限 [mm]。これ未満はアフィンを諦める。
# 実測: spread 1.5mm 付近で並進のみに負け、0.16mm では p95 853um まで暴れる
_MIN_ANCHOR_SPREAD_MM = 2.0


@attrs.frozen
class DisplacementFit:
    """機械座標の変位場 d(p) = L (p − c) + t のアフィン最小二乗当てはめ.

    Attributes:
        model: 使用した模型。アンカーが 3 点未満または準共線なら "translation"
        centroid: アンカーの重心 c（機械座標、mm）
        translation: 重心での変位 t = mean(d_i)（mm）
        anchor_spread_mm: アンカーの最小主軸方向 RMS 広がり [mm]
        machine_transform: 補正 p ↦ p + d(p)（Compose、アフィン）
    """

    model: DisplacementModel
    centroid: Point2d
    translation: Point2d
    anchor_spread_mm: float
    machine_transform: Transform

    def predict(self, machine_point: Point2d) -> Point2d:
        """その点での変位 d(p) を返す（= machine_transform.apply(p) − p）."""


def fit_displacement(
    anchors: Sequence[Point2d], displacements: Sequence[Point2d]
) -> DisplacementFit:
    """区ごとの (アンカー, 変位) からアフィン変位場を最小二乗で当てはめる.

    Raises:
        ValueError: anchors が空、または長さが displacements と違う場合
    """


@attrs.frozen
class BoardAlignment:
    """複数領域の計測から得た基板全体のアフィン補正.

    Attributes:
        results: 成功した領域計測（1 件以上）
        fit: results から当てはめた変位場（__attrs_post_init__ で計算）
    """

    results: tuple[RegionAlignment, ...]
    fit: DisplacementFit = attrs.field(init=False, eq=False)

    def __attrs_post_init__(self) -> None:
        """Raises: ValueError: results が空の場合"""

    @property
    def machine_transform(self) -> Transform:
        """補正 Transform（fit.machine_transform）."""

    @property
    def model(self) -> DisplacementModel: ...

    @property
    def translation(self) -> Point2d:
        """重心での変位 [mm]（ログ・summary 用）."""

    @property
    def residuals(self) -> tuple[Point2d, ...]:
        """区ごとの残差 r_i = d_i − d̂(anchor_i) [mm]（results と同順）."""

    @property
    def residual_rms(self) -> float:
        """残差の RMS ノルム [mm] = sqrt(mean(|r_i|²))."""

    @property
    def residual_max(self) -> float:
        """残差ノルムの最大 [mm]."""
```

- `spread` は**削除**（残差に置き換える。理由: `spread` は変位のばらつきなので、真にアフィンな
  場では「信号」（レバー腕）が支配し、誤差の指標にならない。残差 RMS は誤差だけを見る）
- `machine_transform` の型は `Transform`（実体は `Compose`）

```python
class RegionAlignmentSession:
    def plan_regions(self, pad_centers: Sequence[Point2d]) -> list[AlignmentRegion]:
        """塗布対象 pad の分布から照合領域を計画する（撮像・移動なし）.

        Args:
            pad_centers: 塗布対象 pad の中心（board 座標、mm）。区内にこれが
                1 つも無い区はスキップする
        """
```

- `corrected_projector` を**削除**（`CopperProjector.with_correction` に一本化）
- `__init__` / `from_calibration` / `measure` / `projector` / `edge_detector` / `region_roi` は
  シグネチャ変更なし。`__init__` は `max_passes` / `converge_tolerance` を `RegionAligner` へ渡す

### `src/pcbasm/config.py` — `PadAlign`

```python
@attrs.frozen
class PadAlign:
    max_correction: float = 1.0       # 1 領域で許容する累積ずれ [mm]。超過は照合失敗
    search_window: float = 2.0        # 照合の探索窓 片側幅 [mm]
    region_size_px: int = 400         # 照合領域の一辺 [px]
    min_regions: int = 4              # 成功が必要な最小領域数。下回ると塗布を中止
    max_passes: int = 2               # 1 領域あたりの再計測回数の上限
    converge_tolerance: float = 0.01  # このパス増分以下で収束とみなす [mm]
    board_edge_margin: float = 2.0    # 照合 ROI が基板外形から確保する最小距離 [mm]
    min_sharpness: float = 0.15       # 拘束不足として棄却する sharpness 閾値
    canny_low: float = 100.0
    canny_high: float = 200.0
    blur_ksize: int = 5

    def __attrs_post_init__(self) -> None:
        """region_size_px / min_regions / max_passes が 1 以上の整数、
        converge_tolerance が正、board_edge_margin / min_sharpness が 0 以上
        （いずれも ValueError）."""
```

- 削除: `region_count`
- `min_regions` の既定 3 → **4**（アフィンは非共線 3 区が下限。4 なら残差の自由度が 2 残り、
  当てはめが自動的に厳密解にならない。実 PCB は 6 区計画されるので 2 区の失敗を許容できる）
- `converge_tolerance = 0.01`（= 0.3px @ 30.2px/mm。理想モデルの pass2 増分 0.4〜4.4um と
  サブピクセル再現性 3.6um の上、追う誤差 50um の 1/5）

### `src/webui/config_store.py`

`FieldSpec`（103 行付近）: `region_count` を削除し 2 本追加。

```python
    FieldSpec("paste_dispenser.pad_align.max_passes", "再計測の上限回数", "int"),
    FieldSpec(
        "paste_dispenser.pad_align.converge_tolerance", "収束判定の増分", "float", "mm"
    ),
```

`_coerce`: int の per-key 検証（221 行付近）を `region_size_px` / `min_regions` / `max_passes` に、
float の非負検証（199 行付近）はそのまま、`converge_tolerance` は `<= 0.0` で
`UnknownFieldError(f"{spec.key}: 正の値が必要です")`。

### `src/webui/jobs/board_ops.py`

```python
def measure_regions(
    ctx: JobContext,
    session: RegionAlignmentSession,
    regions: Sequence[AlignmentRegion],
    *,
    min_regions: int,
    on_success: Callable[[RegionAlignment], None] | None = None,
    on_failure: Callable[[AlignmentRegion], None] | None = None,
) -> BoardAlignment:
```

シグネチャ変更なし。ログと中止条件を新しい失敗モードへ合わせる（下記実装ステップ）。

### `src/pcbasm/posctrl/__init__.py`

| 操作 | シンボル |
|---|---|
| 追加 | `DisplacementFit`, `DisplacementModel`, `fit_displacement` |
| 維持 | 他すべて（`plan_alignment_regions` / `BoardAlignment` / `RegionAligner` ...） |

`__all__` はアルファベット順を維持（`CopperProjector` < `DisplacementFit` < `DisplacementModel` <
`EdgeMatch`、小文字群は `display_at_point` < `fit_displacement` < `interactive_display_at_point`）。

## 実装ステップ

### 1. `copper.py`: `with_correction`

`Compose` を import し、`self._polygons` / `_offset_transform` / `_pixel_per_mm` / `_image_size` を
そのまま引き継いだ新インスタンスを返すだけ。他は 1 行も触らない。

### 2. `region.py`: タイル張り

```
0. 検証: region_size_px < 1 / region_size_px > min(image_size) → ValueError
   pad_centers が空 or safe_area.is_empty → []
1. 参照アンカー a0 = board_transform.apply(safe_area bbox 中心)
   matrix, shift = projector.board_to_pixel_affine(a0);  inv = inv(matrix)
   half = region_size_px / 2
   corner_offsets = [[-h,-h],[h,-h],[h,h],[-h,h]] @ inv.T   # board 座標での ROI 4 隅相対（回転あり）
2. points, normals = _projected_edge_points(projector, matrix, shift)   # 既存関数のまま
   len(points) == 0 → []
3. pad_px = array([[p.x, p.y] for p in pad_centers]) @ matrix.T + shift
   base = pad_px.mean(axis=0)                 # 格子の位相
   # 中心が pad から half 以内にない区は条件 2 で必ず落ちるので、k の範囲はこれで十分
   k_lo = ceil((pad_px.min(axis=0) - half - base) / region_size_px)
   k_hi = floor((pad_px.max(axis=0) + half - base) / region_size_px)
4. 各 (i, j) in k_lo..k_hi:
   center = base + (i, j) * region_size_px
   board_xy = (center - shift) @ inv.T
   if not Polygon(corner_offsets + board_xy).within(safe_area): continue
   if not any(all(|pad_px - center| <= half, axis=1)): continue
   inside = all(|points - center| <= half, axis=1);  n = inside.sum();  n == 0 → continue
   constraint = eigvalsh(N_in.T @ N_in)[0]
   if sqrt(max(constraint, 0) / n) < min_sharpness: continue
   採用（constraint, n, board_xy）
5. roi = centered_roi(image_size, region_size_px)（全区共通）
   sort_by_nearest(anchor, tour_start) → index 0 から振り直して AlignmentRegion を構築
```

設計判断:

- **位相を pad 重心に合わせる**。safe_area bbox の端に合わせると回転で位相が動き、実測で
  計画区数が θ=0° 9 → 0.5° 4 → 2° 3 と暴れる。pad 重心位相なら 6 / 6 / 5 で安定し、
  タイル位置がタイル数に依存しない（丸めで格子が跳ばない）。これで `_GRID_RATIO_TOLERANCE` は
  不要になる — 端の 1 タイルが増減しても、そのタイルは `within` 判定で落ちるか、
  もともと pad を含まない位置だけ。
- **重なりなしは pixel 空間の格子で保証する**。ROI は画像上の軸平行正方形なので、
  格子も pixel 空間で張れば「一辺 region_size_px、間隔 region_size_px」で自明に重ならない。
  board 空間の距離判定（旧・最小離間）は回転を跨ぐと厳密でなかった。
- **pad 包含判定も pixel 空間で行う**。board 座標の四角形 vs 点と数学的に同一（同じアフィン写像）で、
  pixel 空間では軸平行になるため `all(|pad_px - center| <= half)` の 1 行で済む。
- **λ_min 採点は順位付けではなく閾値フィルタ**として残す。実測では pad を含む区はすべて
  予測 sharpness 0.50〜0.70 で通るので普段は効かないが、縮退区を移動前に落とす保険。

### 3. `aligner.py`: 反復計測

```
measure(region):
  cumulative = Point2d(0, 0)
  for pass_index in range(max_passes):
      target = region.anchor + cumulative
      klipper.send_gcode(stage.move(x=target.x, y=target.y, speed=Speed.rate(0.5))
                         + gcode.wait(settle_time) + gcode.wait_for_done())
      # 累積変位を board 変換の後段へ挿す。これをやらないと同じ変位を二重に測る
      projector = (self._projector if pass_index == 0
                   else self._projector.with_correction(Shift.from_point(cumulative)))
      projection = projector.project(target)
      image = camera.capture();  edges = edge_detector.detect_edges(image)
      frame_sink があれば render_edge_match(image, edges, projection.edge_mask, region.roi)
      match = matcher.match(edges, projection.edge_mask, region.roi)
      match is None → RuntimeError(f"領域 {region.index} の銅箔エッジ照合に失敗しました"
                                   f"（{pass_index + 1} パス目・拘束不足または観測エッジなし）")
      machine_transform = to_machine_transform(
          match.camera_transform, offset_transform,
          projection_anchor=target, observed_at=stage.get_position().to2d())
      step = machine_transform.apply(target) - target      # 純並進
      cumulative = cumulative + step
      if max_correction_mm is not None and cumulative.norm > max_correction_mm:
          RuntimeError(f"照合ずれ {cumulative.norm:.3f} mm が上限 ... を超過しました（誤マッチの疑い）")
      if step.norm <= converge_tolerance_mm:
          break
  return RegionAlignment(region=region, match=match, displacement=cumulative,
                         passes=pass_index + 1, converged=step.norm <= converge_tolerance_mm)
```

- **`correction.py` は呼ぶだけで 1 行も変えない**。`observed_at` には必ず
  「その撮像を撮った実ステージ位置」`stage.get_position().to2d()` を渡す
- **各パスの `step` を単純和にできる根拠**: `to_machine_transform` の展開は
  `M(p) = p + (observed_at − projection_anchor) − R(d)`（`R` は線形なので
  `R(R⁻¹(anchor − p) + d) = (anchor − p) + R(d)` が厳密）。`p` に依存しない = 純並進なので、
  `Compose([M1, M2])` の作用は `p + t1 + t2` と厳密に一致する。よって `Shift` の合成＝和として
  累積してよい（`transform.py:258` の `Shift.apply` が加算）。共役の `projection_anchor` /
  `observed_at` は**パスごとにそのパスの値**を渡す（前パスの anchor を使い回さない）
- `max_correction` は**累積**に対して判定する（1 パス目は従来と同値。2 パス目以降で
  上限が実質 2 倍になるのを防ぐ）
- 2 パス目で照合が `None` になったら `RuntimeError`（1 パス目の結果を拾わない）。模型と
  観測が食い違っている状態なので、区ごと落として `min_regions` に判定を委ねるのが単純
- `max_passes >= 1` は `PadAlign` が保証するが、pyright は `match` / `step` が
  ループ後に未束縛になり得ると見る。ループ前に `step = Point2d(0.0, 0.0)` を置くのではなく、
  最初のパスをループ内で必ず通す構造（`for` の後に `RegionAlignment` を返す）にして、
  必要なら `assert` ではなく `match` を `EdgeMatch | None` で持ち回らない書き方を選ぶ
  （`while` + 明示的なカウンタでもよい。読みやすい方を実装者が決める）

### 4. `alignment.py`: 推定器

`fit_displacement` の閉形式（`plan-implementer` はこのとおりに書く）:

```
n = len(anchors);  n == 0 → ValueError
P = array([[a.x, a.y] for a in anchors])          # (n, 2)
D = array([[d.x, d.y] for d in displacements])    # (n, 2)
c = P.mean(axis=0);  Pc = P - c
t = D.mean(axis=0)                                # Σ Pc = 0 なので t は L と分離して決まる
spread = (svd(Pc, compute_uv=False)[-1] / sqrt(n)) if n >= 2 else 0.0
if n < 3 or spread < _MIN_ANCHOR_SPREAD_MM:
    model, L = "translation", zeros((2, 2))
else:
    model = "affine"
    L = lstsq(Pc, D - t, rcond=None)[0].T         # Pc @ X ≈ D - t で X = Lᵀ
transform = Compose([Shift(-c[0], -c[1]), Matrix2d(eye(2) + L), Shift(c[0] + t[0], c[1] + t[1])])
```

導出: `d(p) = L (p − c) + t` を最小二乗すると、`Σ (p_i − c) = 0` から `t` と `L` が分離し
`t = d̄`、`L = D_cᵀ P (PᵀP)⁻¹`（`lstsq` は同じ解を数値安定に返し、階数落ちでは最小ノルム解になる）。
補正は `p ↦ p + d(p) = (I + L)(p − c) + (c + t)` なので上の `Compose`（`Compose` は
リスト順に適用: `transform.py:415`）。

- `predict(p)` は `machine_transform.apply(p) - p`（式を二重に持たない）
- `BoardAlignment.residuals` は `d_i − fit.predict(anchor_i)`
- `Matrix2d` は `Point3d` に対して z を保つ（`transform.py:330`）ので、
  `Compose([board_transform, correction, toolhead_offset, height_plane])` の 3D 経路は壊れない
- 縮退の警告は `fit` の中では出さない（純関数のまま）。ログは `measure_regions` が
  `model` / `anchor_spread_mm` を見て出す

`RegionAlignmentSession`:

- `plan_regions(pad_centers)` に引数追加。`plan_alignment_regions` へ `pad_centers` と
  `min_sharpness=self._pad_align.min_sharpness` を渡し、`count` を渡さない
- `corrected_projector` を削除
- `RegionAligner(... max_passes=pad_align.max_passes,
  converge_tolerance_mm=pad_align.converge_tolerance)`

### 5. `board_ops.measure_regions`

- 事前判定のメッセージから `region_count` を落とす:

```
f"照合領域を {len(regions)} 個しか計画できませんでした（必要 {min_regions}）。"
f"領域は塗布対象 pad を含み ROI 全体が基板外形の内側 {…} mm に収まるタイルだけを使います。"
f"region_size_px を小さくするか board_edge_margin を下げ、それでも足りなければ "
f"min_regions を下げてください"
```

- 区ごとの log に反復情報を足す:

```
f"{label}: dx={d.x:+.4f} dy={d.y:+.4f} mm, rms={match.rms_distance_px:.2f} px, "
f"sharpness={match.sharpness:.3f}, passes={alignment.passes}"
+ ("" if alignment.converged else " ※収束せず")
```

- 成功数不足のメッセージは現行を踏襲（`region_count` の言及だけ落とす）
- `BoardAlignment` 構築後に**判定材料をまとめて出す**:

```
model == "translation" なら警告 log（閾値の数値は webui 側に複製しない）:
  f"警告: アンカーの広がりが不足（最小主軸 {board.fit.anchor_spread_mm:.2f} mm）のため"
  f"アフィンを諦め並進のみで補正します。region_size_px を小さくして区の配置を広げてください"
補正 log:
  f"補正モデル: {board.model} / 並進 dx={t.x:+.4f} dy={t.y:+.4f} mm / "
  f"スケール x={(m.scale_x - 1) * 1e6:+.0f} y={(m.scale_y - 1) * 1e6:+.0f} ppm / "
  f"スキュー {m.axis_angle_error_deg:+.4f} deg"      # m = OrthogonalityMetrics.from_transform(board.machine_transform)
残差 log:
  f"残差 RMS={board.residual_rms * 1000:.1f} um 最大={board.residual_max * 1000:.1f} um"
  f"（照合ノイズは区あたり 5um 級。数倍を超える場合は非線形なひずみが残っている）"
  + 区ごとの 1 行: f"  領域 {i + 1}: rx={r.x * 1000:+.1f} ry={r.y * 1000:+.1f} um"
収束しなかった区があれば:
  f"警告: {k}/{len(results)} 領域が {max_passes} パスでも収束しませんでした（採用はしています）"
```

`OrthogonalityMetrics.from_transform` は補正 Transform そのものに当てる（`board_transform` を
持たなくてよく、`scale_x/scale_y ≈ 1` と `axis_angle_error_deg ≈ 0` からの差が「3 点法が
持っていたスケール・スキュー誤差」そのものになるので解釈が素直）。`measure_regions` の
`max_passes` は `session` からは見えないので、収束警告の文言には回数を入れず
「上限パス数でも収束せず」とする。

### 6. `posctrl.py`（board_tour）

- `session.plan_regions([p.center for p in result.pcb.pads if p.layer == Layer.TOP])`
- overlay の行は `dx/dy` / `rms` / `sharpness` に `passes` を足す（残差は全区の計測後にしか
  出せないので overlay には出さず log に出す）
- `_corrected_entries`: `session.corrected_projector(mt)` →
  `session.projector.with_correction(alignment.machine_transform)`。`Compose([board_transform, mt])`
  の巡回先計算はそのまま（`toolhead_offset` を掛けないのが正しい: カメラを合わせる巡回）
- summary: `spread` → `model` と残差

```
f"照合成功 {len(alignment.results)}/{len(regions)} 領域 / 補正 {alignment.model} "
f"dx={t.x:+.4f} dy={t.y:+.4f} mm（残差 RMS {alignment.residual_rms * 1000:.1f}um / "
f"最大 {alignment.residual_max * 1000:.1f}um）/ 補正巡回 {len(entries)} pads"
```

- `min_regions=1` の据え置きは変えない（診断ツール。区が 1〜2 個なら `fit` が並進へ縮退する）

### 7. `pasting.py`（paste_solder）

- `align_session.plan_regions([pad.center for pad in routed_pads])` —
  塗布対象（有効 pad の順路）をそのまま渡す。`routed_pads` は照合ブロックより前
  （806 行）で確定しているので順序変更は不要
- `correction = alignment.machine_transform` と
  `Compose([board_transform, correction, toolhead_offset, height_plane])` は**変更なし**
  （機械座標へ出る最後の 1 回だけ適用する到達点を維持）
- summary に `model` と残差を足す（board_tour と同文面）
- 高さ計測は補正前 XY のまま（前ラウンドのレビュー N12 と同じ。回帰ではない）

### 8. 設定テンプレート 2 本

`data/config-templates/kurousagi.paste/machine.toml` と `data/testing/config/machine.toml` の
`[paste_dispenser.pad_align]`:

```toml
[paste_dispenser.pad_align]
# 領域単位の銅箔照合 → 基板全体のアフィン補正（並進+回転+スケール+スキュー）
max_correction = 0.3      # 1 領域で許容する累積ずれ [mm]。超過は誤マッチとして照合失敗
search_window = 1.4       # 照合の探索窓 片側幅 [mm]
region_size_px = 400      # 照合領域の一辺 [px]（400 + 2*42 = 484 でフレームに収まる）
min_regions = 4           # 成功が必要な最小領域数。下回ると塗布ジョブを中止
max_passes = 2            # 1 領域あたりの再計測回数の上限（累積で投影を補正して再照合）
converge_tolerance = 0.01 # パス増分がこれ以下で収束とみなす [mm]（= 0.3px）
board_edge_margin = 2.0   # 照合 ROI が基板外形から確保する最小距離 [mm]
min_sharpness = 0.15      # 拘束不足の棄却閾値
canny_low = 55.0          # ← kurousagi は 55/104/3、testing は 81/192/5 を維持
canny_high = 104.0
blur_ksize = 3
```

`region_count` を削除。`config/` は gitignore なので触らない（削除キーは cattrs が無視、
新キーは既定値で動く）。

## テスト観点（`spec-test-author` 向け）

**変更禁止**: `tests/pcbasm/posctrl/test_correction.py`。

### `tests/pcbasm/posctrl/test_alignment.py`（`fit_displacement` / `BoardAlignment` を書き直し）

- **アフィン復元**: 既知の `L` / `t` で作った変位を非共線 4 点以上に与えると、
  `machine_transform` が任意の点で `p + L(p − c) + t` を再現する（`abs=1e-9`）。
  `model == "affine"`
- **並進の復元**: 全区が同じ変位なら `translation` がその値、`machine_transform` がどの点も
  同じだけ動かす（アフィンでも並進が出ることのピン）
- **共線縮退フォールバック**: y が全て同じアンカー（spread = 0）で `model == "translation"`、
  `machine_transform` がどの点も `mean(d)` だけ動かす。`anchor_spread_mm == 0`
- **準共線フォールバック**: spread が 2.0mm 未満（例: y に ±1mm の 6 点）で `model == "translation"`。
  2.0mm を超える配置（例: y が 0 と 13.2mm の 2 行）では `model == "affine"`
- **2 区以下**: 1 区・2 区で `model == "translation"`（アフィンは非共線 3 点が下限）
- **残差**: 純アフィンな入力で `residual_rms == approx(0, abs=1e-9)`。
  非線形な入力（例: `d = (k x², 0)`）で `residual_rms > 0` かつ `residuals` が
  `results` と同順・同数
- `fit_displacement([], [])` / 長さ不一致で `ValueError`
- `BoardAlignment(results=())` で `ValueError`
- `RegionAlignmentSession.__init__` の解像度検証（現行維持）、`measure` が `RuntimeError` を
  握って `None` を返す（現行維持）、`region_roi`（現行維持）
- `corrected_projector` のテストは `CopperProjector.with_correction` のテストへ移す

### `tests/pcbasm/posctrl/test_region.py`（タイル張りへ書き直し）

- **重なりなし**: どの 2 区も一辺 `region_size_px` の正方形として重ならない。テストの
  `board_transform` は Identity / 回転（相似写像）なので、board 座標の中心間距離が
  `region_size_px / pixel_per_mm` 以上あることで確認できる
- **safe_area 内**: ROI 4 隅を board 座標へ戻した四角形が `safe_area` に収まる。
  外周ぎりぎりの区が落ちる（`board_edge_margin` のピン = ユーザー要求「外周部で
  マッチしてはいけない」）
- **pad なし区スキップ**: 銅箔が広く分布していても、pad 中心が無い区は返らない
  （pad 1 個だけ渡すと、その pad を含む区だけが返る）
- **低 sharpness 区スキップ**: 水平エッジだけの銅箔 + そこにある pad を渡しても、
  `min_sharpness=0.15` では返らず、`min_sharpness=0.0` では返る
- **上限なし**: 条件を満たす区が 10 個以上ある配置で全部返る（`count` が無いことのピン）
- **pad_centers 空 / safe_area 空 / 銅箔ゼロ** で空リスト（例外を投げない）
- `region_size_px < 1` / `> min(image_size)` で `ValueError`
- `board_transform` に回転があっても ROI は pixel 空間の正方形で、区数が回転で崩れない
  （θ=0 と θ=2° で同数になること。実測は 6 / 5 なので「±1 個」で書く）
- 予測 sharpness と実測 sharpness が同尺度（前ラウンドのレビュー S2。等方リングで
  `abs(measured − predicted) < 0.1`）

### `tests/pcbasm/posctrl/test_aligner.py`（反復へ書き直し）

- **1 パスで収束**: 観測が設計と一致（変位 0）なら `passes == 1` / `converged is True`、
  移動は 1 回だけ（早期打ち切りのピン）
- **2 パスの累積**: 既知の変位を与えると `displacement` がその値（`abs=0.02mm`）で
  `passes == 2`。**投影を補正しない実装では約 2 倍になる**ので、期待値は真値であり
  2 倍でないことを明示的にピンする（今回の最重要テスト）
- **`max_passes=1`** では `passes == 1` で、`displacement` は 1 パス目の値
- **収束しない場合**: `converge_tolerance` を極小（例 1e-9）にすると
  `converged is False` かつ `passes == max_passes`、それでも `RegionAlignment` を返す
- **照合失敗**（matcher が `None`）で `RuntimeError`。2 パス目の失敗でも `RuntimeError`
- **`max_correction` 超過**で `RuntimeError`（累積に対する判定）
- **`displacement` がレバー腕に依存しない**: 異なる `region.anchor` で同じ観測ずれなら
  同じ `displacement`（MR !149 のピンの移植。`to_machine_transform` を直接呼ぶ
  `TestMachineTransformIsPureTranslation` は `RegionAlignment` を作らない形へ調整）
- `frame_sink` にパスごとに 1 フレーム届く（2 パスなら 2 枚）

### `tests/pcbasm/posctrl/test_copper.py`（追加のみ）

- `with_correction(mt)` の `pixel_of` が `Compose([board_transform, mt])` 相当になり、
  元の投影器は変わらない（不変性）。`polygons` / `pixel_per_mm` が引き継がれる
- 既存の照合テスト（開口問題の棄却・サブピクセル `abs=0.15px`・窓端ガード・
  `centered_roi`）は**そのまま維持**。前ラウンドのレビュー S1（交差項 `hxy = a5` の穴）を
  埋める 45° 一方向エッジのテストを足せるなら足す

### `tests/webui/jobs/test_board_ops.py`

- 全成功で `BoardAlignment` が返り、log に `passes` / 補正モデル / スケール ppm /
  残差 RMS・最大 / 区ごとの残差が出る
- 失敗区で `on_failure` が呼ばれ続行、成功数 < `min_regions` で `ValueError`、
  計画数 < `min_regions` なら 1 区も計測せず `ValueError`（メッセージに `region_count` を
  含まないこと）
- `model == "translation"` になる入力（アンカーが 1 行）で警告 log が出る
- 収束しなかった区があるとき警告 log が出る

### 設定まわりの同期

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/test_config.py` | `region_count` の参照を撤去。既定値 `min_regions == 4` / `max_passes == 2` / `converge_tolerance == 0.01`。1 未満・負値・`converge_tolerance <= 0` で `ValueError` |
| `tests/webui/test_config_store.py` | `region_count` の読み書きを `max_passes`（int・1 未満で拒否）と `converge_tolerance`（float・0 以下で拒否）へ |
| `tests/webui/routers/test_settings_api.py:200` | 同上（クラス名も `max_passes` 版へ） |
| `tests/e2e/test_webui_e2e.py:368` | `region_count` の実 HTTP 往復を `max_passes` へ（キー存在アサートの一覧も更新） |
| `tests/pcbasm/posctrl/test_render.py` | 変更不要（`PadResultRenderer` の IF は維持できる。確認だけ） |

## 想定リスク・トレードオフ

- **`region_size_px=400` では TJ-56-67 の計画区数が 6 で、y 方向のレバー腕を担う区が 1 つだけ**。
  その 1 区が照合に失敗すると残り 5 区が一直線になり `spread = 0` → 並進へ縮退（残存誤差
  76um）。`region_size_px=300` なら 9 区・spread 6.26mm・残存 3.7um まで改善する。→ 確認事項 1
- **反復の 2 パス目は理想モデルでは 2 倍の改善しかない**（1.8〜6.9um → 0.7〜4.4um）。
  計測時間はほぼ倍（6 区 × 2 パス ≈ 25 秒）。実機で `passes=2` の増分ログが常に
  `converge_tolerance` すれすれなら `max_passes=1` に落とせる設計にしてある
- **アフィンは基板の反り・機械の曲がりを吸収できない**。それが残るかどうかは残差 RMS で見える
  （ノイズ 5um 級 vs 非線形 50um で 13〜16um）。ただし区が狭い範囲に固まると非線形は
  局所的にアフィンに見えるので、`TJ-56-67` の y 方向（26mm）の検出力は弱い
- **`max_correction` はアフィン後の全域チェックを足さない**。各区が個別に上限を持ち、
  最小二乗はその範囲の内挿になるうえ、基板全体の広がりを知るには新しい結合が必要になる
  （CLAUDE.md 開発原則 2）。異常は残差とスケール ppm のログに出る
- **`spread`（旧・領域間ばらつき）を廃止する**。真にアフィンな場ではレバー腕の「信号」が
  支配して誤差の指標にならない（実測: 3 点法誤差の場で変位が 8〜206um に散る）。
  同じ役割は残差が担う
- **`corrected_projector` を削って `CopperProjector.with_correction` に移す**。公開面の総量は
  変わらず（session −1 / projector +1）、反復計測と補正巡回が同じ 1 本を使う
- **`RegionAlignment` から `machine_transform` を落とす**。アフィン当てはめが必要なのは
  (anchor, displacement) の対だけで、区ごとの Transform は使われていない
  （現行の唯一の用途が `translation` の導出）
- **タイル位相を pad 重心に合わせる副作用**: 有効/無効 pad の設定を変えると区の位置も動く。
  決定的なので再現性はあるが、「設定を変えたら区が変わった」という混乱はあり得る

## 確認事項（orchestrator 経由）

1. **`region_size_px` の既定を 400 のまま維持するか、300 へ下げるか。** 実測（実 PCB・合成観測、
   全 48 pad の残存誤差）は 400px → 6 区・最大 13.3um、300px → 9 区・最大 3.7um。
   400px では y 方向のレバー腕が 1 区しかなく、その区が失敗すると並進へ縮退する。
   時間は 6 区 → 9 区（2 パスで約 25 秒 → 38 秒）。
   **暫定案: 既定は 400 のまま（タスクの「維持」指示に従う）**、テンプレートのコメントに
   「区数が 6 で足りなければ 300 へ」と書く。300 にするなら
   `data/config-templates/kurousagi.paste/machine.toml` と `data/testing/config/machine.toml` を
   同時に変える（`config/` は gitignore なのでユーザーが WebUI で調整）。
2. **`min_regions` の既定を 4 にしてよいか。** アフィンの下限は非共線 3 区。4 なら残差の
   自由度が 2 残る。実 PCB は 400px で 6 区計画されるので 2 区の失敗まで許容できる。
   **暫定案: 4**（現行 3 から引き上げ）。5 以上にすると 400px の実基板で余裕が 1 区しかない。
3. **収束しなかった区（`max_passes` でも増分 > `converge_tolerance`）を採用するか棄却するか。**
   **暫定案: 採用して警告ログ + summary に件数**。棄却すると `min_regions` を割って
   ジョブが止まりやすくなるうえ、外れ値は残差ログに出るので判断材料は残る。

## 参照

- 前ラウンドの計画とレビュー: `memory/agents/implementation-planner/region-alignment-average.md`、
  `memory/agents/code-reviewer/region-alignment-average.md`
- 符号規約（**変更禁止**）: `src/pcbasm/posctrl/correction.py`、`tests/pcbasm/posctrl/test_correction.py`
- 数式（**変更禁止**）: `copper.py` の `_fit_sharpness` / `_parabolic_subpixel`
- 実測スクリプト: `scratchpad/exp_{tile,tile2,iter,iter2,fit,fit2,e2e}.py`（上記パス）
- skill: `testing-strategy`、`refactor-conventions`
- ドキュメント同期（`code-simplifier`）: `src/pcbasm/posctrl/README.md`（前ラウンドの
  レビュー S7 が未着手のまま。旧 `PadAligner` / `PadAlignmentSession` の記述が残っている）

## 並列実行の分担

| agent | 触るファイル |
|---|---|
| `plan-implementer` | `src/pcbasm/posctrl/{copper,region,aligner,alignment,__init__}.py`, `src/pcbasm/config.py`, `src/webui/config_store.py`, `src/webui/jobs/{board_ops,posctrl,pasting}.py`, `data/config-templates/kurousagi.paste/machine.toml`, `data/testing/config/machine.toml` |
| `spec-test-author` | `tests/pcbasm/posctrl/{test_copper,test_region,test_aligner,test_alignment}.py`, `tests/pcbasm/test_config.py`, `tests/webui/test_config_store.py`, `tests/webui/jobs/test_board_ops.py`, `tests/webui/routers/test_settings_api.py`, `tests/e2e/test_webui_e2e.py` |
| どちらも触らない | `src/pcbasm/posctrl/{correction,position,render}.py`, `tests/pcbasm/posctrl/test_correction.py`, `src/pcbasm/posctrl/README.md` |

## 検証

```
make format && make type && make test-no-hardware
make test-e2e
grep -rn 'region_count\|corrected_projector\|\.spread\|machine_transform=' src tests --include=*.py
grep -rn '</content>' src tests
```

実機テスト（`make test` / `@mark_hardware`）は**実行しない**。実機でユーザーが見るべきもの:

1. ログの `補正モデル`（`affine` で出ているか。`translation` なら区の配置が足りない）
2. `残差 RMS` / `最大`（区あたり 5um 級に収まればアフィンで足りている。数倍なら非線形が残る）
3. `スケール x/y ppm` と `スキュー deg`（3 点法が持っていた誤差の大きさ。数百 ppm が想定）
4. 区ごとの `passes` と `※収束せず`（常に 2 パス目が大きく動くなら反復に意味がある。
   逆に増分が常に微小なら `max_passes=1` へ）
5. 塗布結果: 全 pad が均等に合うか（前回の「合う pad と合わない pad の混在」が消えるか）
