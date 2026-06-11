# board_tour 統合: pad ROI 限定の銅箔照合・自動収束・微小回転推定・Observer 契約統一

## Context

MR !66 で `CopperProjector` / `CopperEdgeMatcher`（chamfer）と対話デモを実装・実機検証済み。本変更でこれを **board_tour の pad 巡回**に組み込み、ペースト対象 pad への精密位置合わせを実現する。

ユーザー確定要件（AskUserQuestion 回答済み）:
1. **pad ROI 限定照合**: 全画面ではなく対象 pad の投影 bbox + マージンに限定。想定エッジは **pcb.copper（トラック融合済み）の全画面 edge_mask から矩形切出し**（pad ポリゴンは ROI 決定のみに使用。shapely クリップによる偽エッジを作らない）
2. **微小回転 θ も推定**し、pad ごとの補正を**「並進+回転」の machine 空間 Transform** として返す（将来 pasting fill path へ合成。ステージは XY のみなので board_tour では θ は表示のみ）
3. **自動収束**: 各 pad へ移動 → tolerance まで反復補正 → overlay + dx,dy,θ 表示 → キー待ち → 次の pad
4. **Observer 契約統一**: `observe() -> Transform`（カメラ mm 空間、原点=画像中心、**想定→観測**）。既存 `OffsetObserver`（円検出）は `Shift` を返す形に改修

実行: エージェントチーム（パターン A 並列）、testing-strategy 準拠、完了後 gitlab-mr。
ブランチ: **`feature/20260611/pad-copper-alignment`**（main から）

## conjugation の厳密な式（符号規約の核心 — 再導出禁止）

実機検証済みアンカー: (A2) `R = Rotation.from_points(Δs, Δo)`（offset.py:111）、(A4) 補正式 `target = pos − R.apply(o_mm)`（position.py:81）。

**camera→machine 写像 ψ**（アンカー位置 a に対し）:

```
ψ_a(o) = a − R(o) = Compose([offset_transform, Scale.flip(x=True, y=True), Shift.from_point(a)])
```

(A4) を再現するよう R をそのまま使う（厳密幾何の R⁻¹ に「修正」しない。(A4) が公理）。

**machine 空間補正 Transform**（カメラ空間照合結果 G、投影アンカー s0、最終観測時位置 s_obs）:

```
M = ψ_{s_obs} ∘ G ∘ ψ_{s0}⁻¹ = Compose([ψ(s0).inverse(), G, ψ(s_obs)])
```

整合性（テストでピン留め）:
- G = Shift(d), s_obs = s0 = pos → `M(p) − p = −R(d)` = (A4) の変位と全点一致
- s_obs ≠ s0 → 並進 = `(s_obs − s0) − R(d_final)` = adjust() 累積補正と一致
- G = 中心 c 回りの回転 θ → R が純回転なら θ_machine = θ（固定点 ψ(c) ≒ pad 設計中心）。det<0 の R でも Compose 共役が自動処理（θ → −θ）
- board_transform の鏡映は ψ に登場しない（アンカー s0 算出の上流のみ）→ θ 符号に無影響

## 公開 IF（シグネチャ確定 — 並列起動の前提）

### `posctrl/copper.py`（変更）

```python
type PixelRect = tuple[int, int, int, int]  # (x0, y0, x1, y1) 半開区間、全画面px

@attrs.frozen
class RigidEdgeMatch:
    offset: Offset            # 並進（観測−想定、px。.mm で mm）
    rotation: Rotation        # θ（カメラ空間、ROI中心回り）
    center_mm: Point2d        # 回転中心（カメラmm、画像中心原点）
    mean_distance_px: float

    @property
    def camera_transform(self) -> Transform:
        """想定→観測: o ↦ Rot_θ(o−c) + c + d
        = Compose([Shift(-c.x, -c.y), rotation, Shift(c.x + d.x, c.y + d.y)])"""

class CopperProjector:
    # __init__ / project は不変。追加:
    def pixel_of(self, board_point: Point2d, stage_xy: Point2d) -> Point2d: ...
        # 既存 _pixel_of の公開化（符号ピンの公開面への移譲）
    def roi_of(self, polygon: Polygon, stage_xy: Point2d,
               margin_mm: float = 1.0, min_size_mm: float = 3.0) -> PixelRect: ...
        # polygon exterior 全頂点の投影 bbox + マージン、min_size 中心対称拡張、フレームクランプ

class CopperEdgeMatcher:
    def __init__(self, pixel_per_mm: float, search_window_mm: float = 2.0,
                 crop_size: tuple[int, int] | None = None,
                 theta_range_degrees: float = 2.0,
                 theta_coarse_step_degrees: float = 0.5,
                 theta_fine_step_degrees: float = 0.1) -> None: ...
    def match(self, observed_edges, expected_edges) -> EdgeMatch | None: ...  # 既存・完全不変
    def match_rigid(self, observed_edges: ImageArray, expected_edges: ImageArray,
                    roi: PixelRect | None = None) -> RigidEdgeMatch | None: ...
        # roi=None は既存 _template_rect (central crop) にフォールバック
```

### `posctrl/correction.py`（新規）

```python
def to_machine_transform(camera_transform: Transform, offset_transform: Transform,
                         projection_anchor: Point2d, observed_at: Point2d) -> Transform:
    """M = ψ_{observed_at} ∘ camera_transform ∘ ψ_{projection_anchor}⁻¹（上式そのまま）"""
```

### `posctrl/pad.py`（新規）

```python
class CopperPadObserver:
    """固定アンカー投影に対する pad ROI 照合 observer（observe() -> Transform 契約）."""
    def __init__(self, camera: Camera, edge_detector: CopperEdgeDetector,
                 matcher: CopperEdgeMatcher, projection: CopperProjection,
                 roi: PixelRect) -> None: ...
    def observe(self) -> Transform: ...
        # capture → detect_edges → match_rigid(roi) → camera_transform。失敗時 RuntimeError
    @property
    def last_match(self) -> RigidEdgeMatch | None: ...

@attrs.frozen
class PadAlignmentResult:
    machine_transform: Transform   # 設計machine点 → 観測machine点（fill path 合成用）
    match: RigidEdgeMatch          # 最終照合（表示用）
    anchor: Point2d                # s0 = board_transform.apply(pad.center)
    adjusted_position: Point2d     # adjust() 戻り値
    roi: PixelRect

    @property
    def translation(self) -> Point2d: ...   # machine_transform.apply(anchor) − anchor
    @property
    def rotation(self) -> Rotation: ...     # from_points(ex, M(anchor+ex)−M(anchor))。鏡映も自動処理

class PadAligner:
    def __init__(self, *, camera: Camera, klipper: Klipper, stage: XYZStage,
                 projector: CopperProjector, matcher: CopperEdgeMatcher,
                 edge_detector: CopperEdgeDetector,
                 board_transform: Transform, offset_transform: Transform,
                 roi_margin_mm: float = 1.0, min_roi_mm: float = 3.0,
                 tolerance: float = 0.05, max_iterations: int = 10,
                 settle_time: float = 0.5) -> None: ...
    def align(self, pad: Pad) -> PadAlignmentResult: ...
        # pad へ移動 → 投影をアンカー s0 で固定 → XYPositionAdjustor で収束 → M 構築
        # 照合失敗/非収束は RuntimeError
```

### `posctrl/position.py`（変更 — 破壊変更）

```python
class XYPositionAdjustor:
    def __init__(self, observe: Callable[[], Transform],   # 旧 observe_offset
                 klipper: Klipper, stage: XYZStage,
                 offset_transform: Transform,               # 新規必須（camera→machine 変位の R）
                 tolerance: float = 0.1, max_iterations: int = 10,
                 move_velocity_ratio: float = 0.5, settle_time: float = 0.5) -> None: ...
    def adjust(self) -> Point2d:  # 不変
```

ループ内: `offset = self._offset_transform.apply(self._observe().apply(Point2d(0.0, 0.0)))`。
以降の `target = pos − offset` は**1文字も変えない**。回転成分は原点適用で自然に並進へ縮約（ステージは回転補正不可）。

### `posctrl/offset.py`（変更）

`OffsetTransformMeasurer.__init__(observe: Callable[[], Transform], ...)`。内部 `o_i = observe().apply(Point2d(0.0, 0.0))`。`measure()` は不変。

### `posctrl/setup.py`（変更）

- `OffsetObserver.__call__` → `observe(self) -> Transform`（円検出 → `Shift(mean_mm.x, mean_mm.y)`）
- `corrected_offset` クロージャ削除。`OffsetTransformMeasurer(observe=observer.observe, ...)`、`XYPositionAdjustor(observe=observer.observe, offset_transform=offset_transform, ...)` へ
- `BoardCalibrationResult` / `setup_board_calibration` シグネチャ不変

### `posctrl/__init__.py`

export 追加: `RigidEdgeMatch`, `PixelRect`, `to_machine_transform`, `CopperPadObserver`, `PadAligner`, `PadAlignmentResult`

### `src/scripts/posctrl/board_tour.py`（変更）

引数追加: `--copper-tolerance`(0.05) `--search-window`(2.0) `--roi-margin`(1.0) `--min-roi`(3.0) `--theta-range`(2.0) `--canny-low`(100) `--canny-high`(200) `--blur-ksize`(5)（Canny default は実機実績値 100/200）

pad 巡回部を置換: Pad オブジェクト単位の nearest ソート（components と同じ dict 逆引きパターン）→ 各 pad で `aligner.align(pad)` → 成功時: 収束位置で再投影した fill overlay + `designator.pad_number / dx,dy(mm) / θ(deg) / mean_distance` を putText して**キー待ち**（Esc で巡回中断、他キーで次へ）→ `RuntimeError` は警告表示してキー待ち後 continue。最後に全 pad の translation / θ のサマリを print。

### `src/scripts/pasting/toolhead_offset.py`（変更・追随のみ）

`corrected_paste_offset` 削除 → `XYPositionAdjustor(observe=paste_observer.observe, offset_transform=cal_result.offset_transform, ...)`

## 実装の要点

- **θ スイープ**: 観測 DT は match_rigid 1 回につき 1 回だけ計算し全 θ 候補で使い回す。template 回転は float32 化した ROI を ROI 中心回りの 2x3 行列（自前 Rotation 規約と同一の数式で構成、`getRotationMatrix2D` の角度符号規約に依存しない）で `warpAffine(INTER_LINEAR)` → `>0` 再二値化。線太り対策はスコアを「min_val / その候補の template 画素数」で正規化。coarse ±2.0°/0.5° 刻み → fine 最良±0.5°/0.1° 刻み（計 20 回 matchTemplate、Pi 5 で反復あたり数十 ms）
- **ROI 限定**: template 矩形 = roi、探索矩形 = roi ± window_px（フレームクランプ）。エッジ線マスクの矩形切出しは新規画素を生まない＝偽エッジゼロ
- **収束ループの投影アンカー**: pad の指令位置 s0 に固定し**ループ中は再投影しない**（毎反復同位置で再投影すると d が定数化して発散する。アンカー固定なら既存 adjust と同じ縮約で収束）。副次効果で投影は pad ごと 1 回
- **M の構築**: 収束後 `to_machine_transform(observer.last_match.camera_transform, offset_transform, projection_anchor=s0, observed_at=stage.get_position().to2d())`
- `copper_detection.py` は `match()` と `setup_board_calibration` のみに依存 → **無変更で動作継続**

## 既存テストの破壊範囲

- `tests/pcbasm/posctrl/test_setup.py` — `observer()` 2 箇所 → `observer.observe()` + 戻り値 Transform を `apply(Point2d(0,0)) ≈ mean_mm` で検証
- 他は破壊なし（position/offset の既存専用テストは存在しない）

## テスト計画（testing-strategy 準拠: 合成データ・実 cv2/shapely・mocker.Mock は自前 HAL のみ・class TestXxx・submodule import）

新規/追記（import は既存慣例どおり `from pcbasm.posctrl.<module> import ...`）:

**test_copper.py 追記 — `TestCopperEdgeMatcherRigid`**: 純並進(+7,−4)px θ≈0 / **回転復元符号ピン**（想定リングを自前 Rotation 規約で +1.2° 回した頂点列を polylines 描画 → θ≈1.2±fine 刻み）/ 並進+回転同時 / ROI 限定ピン（ROI 外の逆ずれ構造が結果に影響しない）/ ROI 境界横断エッジで乱れない / ROI 内 template 空 → None / camera_transform ラウンドトリップ。
**TestCopperProjector 追記**: `pixel_of` 投影公式一致 / `roi_of` bbox+マージン・min_size 拡張（0.5mm 角 pad）・フレームクランプ・回転 board_transform で全頂点 bbox

**test_correction.py（新規）— `TestToMachineTransform`**: G=Shift(d), R=Rotation(α∈{0,+7,−30}) → 任意点で `M(p)−p == −R(d)`（**最重要符号ピン**）/ anchor≠observed_at → 並進=(s_obs−s0)−R(d) / 回転共役（固定点 ψ(c)、θ_machine=θ）/ det<0 R → θ→−θ / ψ⁻¹∘M∘ψ ≈ G

**test_position.py（新規）— `TestXYPositionAdjustor`**（klipper/stage は mocker.Mock=自前 HAL）: 縮小 Shift 列で収束・戻り値検証 / `offset_transform=Rotation(90)` 符号ピン / 回転成分付き Transform で並進のみ使用 / 非収束 RuntimeError（substring）

**test_offset.py（新規）— `TestOffsetTransformMeasurer`**: Shift o1/o2 列 → `measure() == Rotation.from_points(Δs, o2−o1)`（90°/45° parametrize）/ 元位置復帰

**test_setup.py 改修**: 上記契約追随のみ

**test_pad.py（新規）— `TestCopperPadObserver` / `TestPadAlignmentResult`**: FakeCamera（`tests/helpers.py` に追加、固定 Image 列を返す自前 HAL Camera Impl）+ 実 projector/matcher で `observe()` の並進が既知ずれと一致・失敗 RuntimeError / `translation`・`rotation` 導出（合成 M、鏡映ケース含む）。PadAligner の収束ループ自体は XYPositionAdjustor テスト + 実機検証でカバー（gcode 解釈の再現はしない）

## 実装ステップ（エージェントチーム）

1. 計画を `memory/agents/implementation-planner/pad-alignment.md` へ配置、ブランチ作成
2. **spec-test-author ∥ plan-implementer 並列起動**（IF 確定済み、tests/ と src/ disjoint。helpers.py は spec-test-author 側）
3. 合流: `make format && make type && make test-no-hardware` green。不整合は該当 agent へ差し戻し
4. code-simplifier → docs-keeper（posctrl/README に 1 行）
5. コミット分割（ファイル群が disjoint なので post-hoc 分割可能。`__init__.py` は commit 4 に同梱）:
   1. `refactor(posctrl): Observer契約を observe() -> Transform に統一` — position/offset/setup/toolhead_offset + test_setup/test_position/test_offset
   2. `feat(posctrl): camera→machine共役の to_machine_transform を追加` — correction.py + test_correction.py
   3. `feat(posctrl): θスイープ+ROI限定の match_rigid を追加` — copper.py + test_copper.py
   4. `feat(posctrl): PadAligner による pad 単位の自動位置合わせを追加` — pad.py + helpers.py + test_pad.py + `__init__.py`
   5. `feat(scripts): board_tour の pad 巡回を銅箔照合の自動収束に置換` — board_tour.py
   分割が破綻したら統合し理由をコミット本文に記録（前回 MR の前例）
6. gitlab-mr skill で push + MR 作成（target: main、マージはユーザー判断）

## 検証

- 各コミット時点で import 自己完結（テストは submodule import）。最終状態で `make format && make type && make test-no-hardware` green
- 実機検証（**ユーザー実施**）: ① 1 pad で align 収束と overlay 一致を目視 ② 既知の傾きを与えた基板で θ 表示の符号確認 ③ 円形 pad 等で θ 不定時の mean_distance 表示確認

## リスク

- 対称形状 pad（円等）で θ 不定 → `min_roi_mm` で周辺銅箔を取り込み拘束を稼ぐ + mean_distance 品質表示（補正は並進のみなので実害なし）
- 並進と θ の弱い結合 → 回転中心を ROI 中心固定で緩和。fine 0.1° = pad 端 1.5mm で ~2.6µm、tolerance 0.05mm に対し十分
- 並進分解能 1px（≈1/ppm mm）→ tolerance はスクリプト引数で調整可能
- Pi 5 性能 → 遅ければ `--theta-range`/coarse 刻みを引数調整

## 参照ファイル

- `src/pcbasm/posctrl/copper.py` — 既存 projector/matcher（`_pixel_of`:130, `_template_rect`:243）
- `src/pcbasm/posctrl/position.py:81`, `offset.py:111`, `setup.py:146-176` — 契約変更点と符号アンカー
- `src/pcbasm/geometry/transform.py` — Scale.flip:161, Rotation.from_points:221, Identity:281, Compose:412
- `src/scripts/posctrl/board_tour.py:131-154` — 置換対象の pad 巡回
- `src/scripts/pasting/toolhead_offset.py:271-283` — 追随箇所
- `tests/pcbasm/posctrl/test_setup.py` — 破壊される既存テスト
