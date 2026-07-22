# 計画: 銅箔位置合わせを「部品ごと」から「関心領域(ROI)ごと」へ変更

## Context

paste_solder は現在、TOP 層の **部品(Component)ごと** に銅箔エッジ照合で塗布位置を精緻化している。問題が 2 つ:

1. **ROI 超過**: 部品の pad パターンが視野より大きいと収まらない。現行実装は ROI（部品の全 pad copper bbox）をフレーム端で**静かにクランプ**して部分照合するため、精度劣化が可視化されない
2. **冗長照合**: 小部品が密集した箇所では部品ごとに撮像・照合するため無駄が多い

対応: 位置合わせ単位を「部品」→「**関心領域サイズで分割した領域**」に変更する（ユーザー提案）。

調査で確定した前提事実:
- 照合テンプレートは既に board 全体の TOP 銅箔（`pcb.copper` 連結島）を CopperProjector が持ち、部品の pad 群は **ROI 矩形の算出にしか使われていない** → 投影・照合・収束系（CopperProjector / CopperEdgeMatcher / CopperPadObserver / XYPositionAdjustor / to_machine_transform）は無変更で流用できる
- 銅箔照合はフルフレーム撮像（1280x720）で動作し `camera.crop` は未使用（crop は円検出等の別用途）
- 実機 FOV ≈ 42.2×23.8mm @30.31px/mm、crop 300px ≈ 9.9mm
- pcbasm の破壊的変更は許可（webui 主力）

## 設計（確定）

- **分割方式**: board 原点 (0,0) 固定のグリッド（セル = 領域サイズ w×h）。**pad を持つセルのみ実体化**（空タイルは生成されない）
- **領域サイズ**: **`camera.crop.size ÷ calibration.pixel_per_mm` から導出**（ユーザー確定: crop はレンズ歪みの起こらない信頼範囲を捉えた設定であり、位置合わせ ROI もその範囲に収めるのが本質。新設定は追加しない）。実機では 300px ≈ 9.9mm。アンカーへステージ移動してから照合するため、領域矩形は常に画像中心 = crop の歪みフリー領域内に収まる。撮像はフルフレーム維持、**照合 ROI = 領域矩形の投影**。`min_roi` は無意味化するため削除
- **pad→領域割当**: `pad.center` の floor 除算。例外: copper exterior が中心セルと交差しない巨大 pad（サーマルパッド等、セルが銅箔内部に沈むケース）は「exterior∩セル矩形の交差長が最大のセル」へ再割当て（タイブレーク (col,row) 昇順）→ エッジ皆無セルでの照合失敗を回避
- **アンカー**: `board_transform.apply(region.center)`（セル矩形中心）へステージ移動。投影・ROI はアンカー固定（収束ループ中の再投影禁止は現行踏襲）
- **収容制約の仕様化**: セッション構築時に `領域サイズ×ρ + 2×(roi_margin+search_window) ≤ FOV`（ρ=|cosθ|+|sinθ|、board_transform の実回転）を検証、違反は **ValueError（設定エラー、失敗数にカウントせずジョブ中止）**。crop 由来でも大きな crop 設定では違反し得る（test-fixture 600px 等）ため検証は必須。align 内でも ROI 非クランプを検証 → 現行の「静かなクランプ」を排除
- **補正 lookup**: designator キー廃止 → **pad 単位**。`RegionAlignments.board_correction(pad)`（Pad の attrs 同値比較で所属領域を線形探索。pasting フローは同一オブジェクトなので確実に一致。(designator, pad_number) は KiCAD 上一意でないため不採用）。同一部品でも pad ごとに所属領域の局所補正が付く = 問題 1 の解そのもの。失敗領域・未割当 pad は None → 無補正フォールバック（現行踏襲）
- **max_failures**: 失敗**領域**数に読み替え、既定 0 維持。abort メッセージは領域ラベル列挙
- **board_tour**: 同じ領域単位へ統一（同一コードパス、overlay ラベル = 領域ラベル + 収容 designator 先頭数件）
- **paste_solder の対象 pad**: `有効 pad ∪ 初回パージ pad` から領域を実体化（現行の「有効 pad を持つ部品の全 pad」より撮像が減る）。board_tour は全 TOP pad
- **rename は最小限**: `PadAligner` / `PadAlignmentSession` / `PadAlignmentResult` は名前維持。`ComponentPads`→`PadRegion`、`group_pads_by_component`→`plan_pad_regions`、`sorted_top_component_pads`→`sorted_top_pad_regions`、`ComponentAlignments`→`RegionAlignments`、`align_component_groups`→`align_pad_regions`

## 凍結する公開 IF（spec-test-author ∥ plan-implementer 並列の同期点）

```python
# pcbasm.posctrl.pad
@attrs.frozen
class PadRegion:
    key: tuple[int, int]                       # グリッド (col, row)。board 原点固定
    bounds: tuple[float, float, float, float]  # セル矩形 (minx, miny, maxx, maxy) [mm]
    pads: tuple[Pad, ...]                      # 割り当てられた pad（常に 1 つ以上）
    @property
    def center(self) -> Point2d: ...           # セル矩形中心（board 座標）= 照合アンカー
    @property
    def box(self) -> Polygon: ...              # shapely.box(*bounds)。roi_of / renderer 用
    @property
    def label(self) -> str: ...                # 例 "C3R5"。ログ・overlay・失敗メッセージ用
    @property
    def designators(self) -> tuple[str, ...]: ...  # 重複除去・ソート済み（表示用）

def plan_pad_regions(
    pads: Sequence[Pad], region_size: tuple[float, float]
) -> list[PadRegion]: ...
# 契約: board 原点固定グリッド（セル = region_size (w,h) [mm]）に pad.center で割当
#       （巨大 pad は境界セル再割当て）、pad を持つセルのみ (col,row) 昇順で返す。
#       空入力は []。純関数（装置非依存、crop 由来のサイズは呼び出し側が渡す）

class PadAligner:
    def align(self, target: PadRegion) -> PadAlignmentResult: ...
    # anchor = board_transform.apply(target.center)、roi = roi_of([target.box], anchor,
    # margin_mm=roi_margin)。min_size_mm 引数・min_roi_mm パラメータは削除。
    # ROI がフレーム(search_window inset)に収まらなければ ValueError。
    # PadAlignmentResult のフィールドは無変更

# pcbasm.posctrl.alignment
def sorted_top_pad_regions(
    result: BoardCalibrationResult, pads: Sequence[Pad] | None = None
) -> list[PadRegion]: ...
# 契約: TOP 層フィルタ（pads 省略時は result.pcb.pads 全 TOP）、
#       領域サイズ = machine.camera.crop.size ÷ calibration.pixel_per_mm、
#       巡回順 = stage 現在位置起点 nearest+2opt（領域 center を board_transform で
#       機械座標化して比較 — 現行の board/machine 座標混在も修正）

@attrs.frozen
class RegionAlignments:
    board_transform: Transform
    results: tuple[tuple[PadRegion, PadAlignmentResult], ...]
    def result_for(self, pad: Pad) -> PadAlignmentResult | None: ...
    def board_correction(self, pad: Pad) -> Transform | None: ...  # C = T_b⁻¹∘M∘T_b

class PadAlignmentSession:
    def align(self, target: PadRegion) -> PadAlignmentResult | None: ...
    # RuntimeError（照合失敗）→ None は現行踏襲。ValueError（設定エラー）は透過。
    # __init__ で crop 由来の領域サイズの収容制約を検証（違反 ValueError）。
    # from_calibration / corrected_projector / projector / edge_detector は無変更

# webui.jobs.board_ops
def pad_align_abort_message(
    failed_regions: Sequence[str], max_failures: int | None
) -> str | None: ...  # 境界意味論は現行同一（<= 許容で None、None は無制限）。文言は領域

def align_pad_regions(
    ctx: JobContext, session: PadAlignmentSession, regions: Sequence[PadRegion], *,
    on_failure: Callable[[PadRegion, int], None] | None = None,
    max_failures: int | None = None,
) -> list[tuple[PadRegion, PadAlignmentResult]]: ...
```

削除（テスト側も削除）: `ComponentPads` / `group_pads_by_component` / `ComponentAlignments`（`result_of` / `corrected_board_transform` は src 内未使用を確認済み）/ `sorted_top_component_pads` / `align_component_groups`。

## ファイル別変更計画

### コア（src/pcbasm/）

- **posctrl/pad.py**: `ComponentPads`(L27)/`group_pads_by_component`(L42) を削除し `PadRegion` + `plan_pad_regions` を新設。`PadAligner.align`(L238) の anchor を `target.center` 由来へ、ROI を `roi_of([target.box], ...)` へ、`min_roi_mm` パラメータ削除、ROI 非クランプ検証追加。log/docstring「部品単位」→「領域単位」
- **posctrl/alignment.py**: `sorted_top_component_pads`(L23)→`sorted_top_pad_regions`（機械座標で巡回順統一）、`ComponentAlignments`(L42)→`RegionAlignments`（board_correction の共役合成 L96-102 は移植）、`PadAlignmentSession`(L105) は `min_roi` 配線削除 + region_size 収容制約検証 + align の型/log 変更
- **posctrl/__init__.py**: re-export 差し替え（L21,25,63,69）
- **config.py**: `PadAlign`(L45-64) から `min_roi` 削除、`max_failures` docstring を「許容失敗領域数」に（新設定の追加はなし。領域サイズは `camera.crop` 由来）

### WebUI ジョブ（src/webui/）

- **jobs/board_ops.py**: `align_component_groups`(L71-120)→`align_pad_regions`（ループ骨格・progress("銅箔照合")・checkpoint・即中止はそのまま、ラベルを `region.label [designators]` に）、`pad_align_abort_message`(L49-68) 文言を失敗領域数+ラベル列挙に
- **jobs/pasting.py**: `_run_paste_solder`(L824-870) の designator フィルタを `align_pads = 有効 pad ∪ 初回パージ pad` に置換 → `sorted_top_pad_regions(result, align_pads)`。補正適用は `alignments.board_correction(pad)`。`_initial_purge_point`(L927) も pad 渡しに。log/summary「N/M 部品」→「N/M 領域（K pads）」。`_pad_renderer` の roi_polygons は region.box へ
- **jobs/posctrl.py**: `_run_board_tour`(L403-463) を領域ループへ、`render_failed` ラベル = `f"{region.label} [{designators先頭数件}] {i+1}/{len} FAILED"`、`_corrected_entries`(L466-486) は型注釈のみ
- **config_store.py**: pad_align 系 FieldSpec に min_roi があれば削除（要 grep 確認）

### 設定（configs/）

- `configs/test-fixture/machine.toml`・`configs/kurousagi/machine.toml`（+ `data/testing/machine.toml` にあれば）: `[paste_dispenser.pad_align]` の `min_roi` 行削除（ローダが未知キーを拒否する場合ロード失敗するため必須）。`[camera.crop]` は変更しない

### テスト（tests/、spec-test-author 担当）

- **tests/pcbasm/posctrl/test_pad.py**: `TestGroupPadsByComponent`→`TestPlanPadRegions`: 単一 pad→1 領域 / region_size 内の近接 pad 併合（密集冗長解消の検証）/ 離れた pad は別 key / **原点固定の検証**（pad 部分集合を変えても他 pad の region key が不変）/ 巨大 pad 再割当て（copper が中心セルを完全に覆うケース）/ label・designators 契約 / 空入力 []。`TestCopperPadObserver`/`TestPadAlignmentResult` は無変更
- **tests/pcbasm/posctrl/test_alignment.py**: `TestRegionAlignments`（result_for/board_correction の pad lookup、未登録 None、共役の数学検証は移植、**同値だが別オブジェクトの Pad でも当たる**契約ピン）、`TestPadAlignmentSession`（target を PadRegion に差し替え、translation/frame_sink/失敗 None のアサーション維持、収容制約違反 ValueError）、`TestSortedTopPadRegions`（TOP のみ / pads 指定 / **crop÷pixel_per_mm 由来の領域サイズ結線**（test-fixture: crop 600px ÷ ppm10 = 60mm、分割検証は pad 間隔 > 60mm か plan_pad_regions 単体で小さいサイズを直接渡す）/ 機械座標基準の巡回順）
- **tests/webui/jobs/test_board_ops.py**: abort_message 境界パラメトリズ維持+文言更新、`TestAlignPadRegions` 追加（成功収集 / on_failure / max_failures 超過即中止 / None で完走）
- test_pasting.py / test_posctrl.py: catalog 系は影響なし、summary 文言ピンがあれば追従

## エージェントチーム実行手順（skill agent-team-startup 準拠）

0. **orchestrator（メイン）**: ブランチ `feature/20260722/region-pad-align` 作成。本計画を 計画書として `memory/agents/orchestrator/region-pad-align.md` に保存（計画は plan mode で Plan agent 2 本+ユーザー確認済みのため implementation-planner は省略 — 判断をノートに記録）
1. **spec-test-author ∥ plan-implementer を 1 メッセージで並列起動**（パターン A、公開 IF は上記で凍結済み）
   - spec-test-author: tests/ のみ。計画書パスと凍結 IF を prompt に明記
   - plan-implementer: src/ + configs/ のみ。コア→WebUI ジョブの順
2. **合流検証**: `make format && make type && make test-no-hardware` + サブエージェント Write 事故の `grep -rn '</content>' src tests configs`（memory: feedback-agent-content-artifact）
3. **code-reviewer** → orchestrator 裁定（must-fix→implementer 差し戻し / should-fix→code-simplifier / 誤検出→却下記録）
4. **code-simplifier**（必要時）→ 再レビュー
5. **docs-keeper**: docstring 整合（pad.py/alignment.py のモジュール docstring「部品単位」残骸掃除等）
6. **最終検証** → コミット（`feat(posctrl): ...` 等、1 コミット 1 関心事で分割）→ **skill gitlab-mr で main への MR 作成**（マージはしない）

## 検証

- `make format && make type && make test-no-hardware`（実機テスト `make test` / `@mark_hardware` は**絶対に実行しない** — 実機確認はユーザー）
- 対象 pytest: `tests/pcbasm/posctrl/test_pad.py` `test_alignment.py` `tests/webui/jobs/test_board_ops.py` `test_pasting.py` `test_posctrl.py`
- `make test-e2e`（WebUI ジョブの文言・フロー変更の通し確認）
- MR 提出後のユーザー残: 実機での paste_solder 通し・board_tour・巨大 pad 基板での領域分割挙動・max_failures（領域単位）の運用値調整

## リスク（実機確認時の注目点）

- 開口問題: 領域内エッジが単一方向のみだと沿い方向が不定（max_correction=0.3mm で bound、テンプレートに周辺銅箔が入るため稀）
- 同一部品内の補正不連続: pad ごとに領域が異なると微小に食い違う（θ0.1°×~7mm≈12µm、tolerance 25µm 内で実害小）
- 巡回順・撮像回数の変化（意図した改善だが実機での見え方が変わる）
- 大型銅箔 pour 内部にしか pad がない領域はエッジ僅少で失敗し得る → max_failures で運用調整
- crop 由来のため、キャリブレーションごとの pixel_per_mm の揺れ・crop 変更で領域グリッドが変わり得る（run 間の分割再現性は保証されない。crop=歪みフリー範囲に ROI を収める意図を優先したユーザー確定のトレードオフ）
