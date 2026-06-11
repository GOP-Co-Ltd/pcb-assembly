# copper_detection: board キャリブレーション連携・設計銅箔照合・対話補正デモ

## Context

前段で `CopperEdgeDetector`（純粋エッジ検出、Canny）と demo スクリプトを実装済み（MR !65、main へ merge 済みを確認）。実機確認で `--canny-low 100 --canny-high 200` が良好。

本変更で demo を本来の目的（**posctrl 基板位置合わせ強化**）に接続する:

1. `--machine` / `--pcb-file` を受け取り `setup_board_calibration()` で board 位置キャリブレーション
2. 関心領域と現在位置から「**現在見えていると想定される銅箔部分**」を設計データ（`pcb.copper`、TOP layer）から抜き出し pixel 空間へ投影
3. 観測エッジ vs 想定エッジで位置ずれ **dx, dy** を算出
4. 想定銅箔を**半透明 overlay** + dx,dy 表示
5. **対話 CLI**（ライブ映像 + stdin デーモンスレッド並行）: `adjust` で 1 回分の dx,dy 補正をステージへ適用（収束は繰り返し打鍵）、`move <x> <y>` で board 座標へ移動、`quit` で終了。'm' キーのマスク切替は維持

ユーザー確定: ライブ映像+並行コマンド入力 / adjust は one-shot / コマンドは adjust・move・quit。
実行はエージェントチーム（`agent-team-startup`）、テストは `testing-strategy` 準拠、完了後 `gitlab-mr`。

## 座標変換チェーン（符号の根拠）

既存コードから確定したアンカー:

- **(A1)** 観測オフセット `o_px = 検出位置 − 画像中心`（x右・y下）、`o_mm = o_px / pixel_per_mm`（`vision/detection.py:153-156`, `Offset.mm`）
- **(A2)** `offset_transform` R: ステージを Δs 動かすと観測オフセットは `R(Δs)` 変化（`posctrl/offset.py:111` `Rotation.from_points(move_vector, o2 - o1)`）
- **(A3)** `board_transform.apply(b)` = board 点 b が画像中心に来るステージ machine 座標（`posctrl/board.py:106-152`）
- **(A4)** 補正は `target = pos − offset_transform.apply(o_mm)`（`posctrl/setup.py:167-168`, `posctrl/position.py:81`）

よって **投影公式**: `pixel(b, s) = image_center_px + pixel_per_mm · R(s − T_b(b))`（s=ステージ位置, T_b=board_transform）。独自の符号反転・y 反転は導入しない（鏡映は Matrix2d と計測済み R が吸収する既存前提）。

### 投影アンカーは「指令位置」に固定する（最重要の設計判断）

live のステージ位置で毎フレーム投影すると、観測と投影が同量動き **dx,dy がステージ移動に対して不変**になり adjust が永遠に収束しない。そこで:

- 起動時: `anchor = stage.get_position().to2d()`
- `move x y`: `anchor = board_transform.apply(Point2d(x,y))` に更新して移動、投影を再計算
- `adjust`: ステージのみ動かし **anchor は更新しない** → 観測が overlay に滑り込み dx,dy → 0 に収束

副産物: 投影は move 時のみ再計算。毎フレームはエッジ検出+マッチングのみ（Pi 5 性能対策）。

## 変更内容

### 1. 新規 `src/pcbasm/posctrl/copper.py`

```python
@attrs.frozen
class CopperProjection:
    """視野内の想定銅箔の pixel 空間表現."""
    fill_mask: ImageArray   # uint8 0/255、穴反映の塗り潰し（overlay 用）
    edge_mask: ImageArray   # uint8 0/255、polygon 境界 1px 線（マッチング用）

class CopperProjector:
    def __init__(self, polygons: Sequence[Polygon],  # TOP 抽出済み (shapely, mm, board座標)
                 board_transform: Transform, offset_transform: Transform,
                 pixel_per_mm: float, image_size: tuple[int, int]) -> None: ...
    def project(self, stage_xy: Point2d) -> CopperProjection: ...

@attrs.frozen
class EdgeMatch:
    offset: Offset            # vision.Offset 再利用: 観測 − 想定 (px)、.mm で mm
    mean_distance_px: float   # 平均 chamfer 距離（品質指標）

class CopperEdgeMatcher:
    def __init__(self, pixel_per_mm: float, search_window_mm: float = 2.0,
                 crop_size: tuple[int, int] | None = None) -> None: ...
    def match(self, observed_edges: ImageArray,
              expected_edges: ImageArray) -> EdgeMatch | None: ...  # 空マスクは None
```

- Observer クラス（`Callable[[], Point2d]`）化は**今回見送り**。将来 `XYPositionAdjustor` 注入時に薄いラッパで足りる（投機的抽象化の回避）
- Projector 実装: Transform はアフィンなので `(0,0),(1,0),(0,1)` に apply して 2×3 行列化 → shapely 座標配列へ numpy 一括適用（ゾーンの頂点数対策）。視野 bbox（pixel 4 隅の逆変換）× `polygon.bounds` で事前フィルタ
- fill_mask: `cv2.fillPoly` で exterior→255、interiors→0 の明示 2 パス
- edge_mask: `cv2.polylines(closed=True)` を exterior/interiors に描画。**fill の輪郭から作らない**（フレーム端のクリップ線が偽エッジになる）
- `posctrl/__init__.py` に 4 つ export 追加

### 2. dx,dy: distance transform + matchTemplate による chamfer

phaseCorrelate（疎エッジでピークが暴れる・窓制御不可）、エッジ×エッジ直接相関（数 px ずれで即ゼロ）を退け、chamfer を採用:

1. `w_px = round(search_window_mm * pixel_per_mm)`。テンプレート = expected_edges の crop 中心領域、探索領域 = DT の同中心 `crop + 2*w_px`（フレーム超過はクランプ）
2. `dt = cv2.distanceTransform(255 - observed_edges, cv2.DIST_L2, 3)` → `np.minimum(dt, w_px)` でキャップ
3. `cv2.matchTemplate(dt領域.float32, (template>0).float32, cv2.TM_CCORR)` の最小位置 → `offset_px`、`mean_distance_px = min_val / nonzero(template)`
4. サブピクセル化は入れない（1px ≈ 1/ppm mm で十分）

符号契約: `offset = 観測 − 想定`。adjust は既存式のミラー:
`pos_new = pos − offset_transform.apply(match.offset.mm)`（+ `Speed.absolute(30)` + `wait_for_done()`）

### 3. デモ `src/scripts/posctrl/copper_detection.py` 書き換え

- **`--image` とカメラ個別引数（--device 等）は廃止**（board キャリブレーション必須と両立しない。machine config に一本化。旧版は git 履歴に残る）
- 引数: `--machine/-m`（default は board_tour.py と同じ）、`--pcb-file/-p`（required）、`--tolerance/-t`（0.1）、`--canny-low`（50.0）、`--canny-high`（150.0）、`--blur-ksize`（5）、`--search-window`（mm、2.0）
- 構成（board_tour.py 踏襲）: `get_machine_config` → `setup_board_calibration` → `with machine_session(klipper):`
  - stdin デーモンスレッド: `for line in sys.stdin` → `queue.Queue`、EOF で "quit"
  - メインループ: capture → `detect_edges` → `matcher.match(edges, projection.edge_mask)`（直近値保持）→ 表示（緑エッジ + `cv2.addWeighted` で fill_mask を 0.35 で薄く着色 + dx,dy[px/mm]・mean_distance・board 座標・ヘルプの putText）→ `waitKey(1)`（'m' トグル / 'q'）→ `queue.get_nowait()` でコマンド処理
  - `adjust`: 直近 match で 1 回補正移動（None なら警告のみ）。anchor 不変
  - `move <x> <y>`: board 座標へ移動 + anchor 更新 + projection 再計算
  - 操作系の二重化（'m'/'q' はウィンドウ、adjust/move/quit はターミナル）を起動時ヘルプに明記

### 4. テスト `tests/pcbasm/posctrl/test_copper.py`（新規、testing-strategy 準拠）

全て合成データ unit（cv2/shapely は実物、モックなし、実機不要）。

`class TestCopperProjector`:
1. 恒等変換 + ppm=10 で既知正方形 → fill/edge が期待 pixel 位置
2. **符号ピン**: stage_xy +1mm(x) → 投影 +10px(x)（投影公式の sign 固定）
3. `Rotation(180)` → 中心点対称
4. 反転 Matrix2d + Shift の Compose → 正しく反映
5. 穴付き polygon → fill の穴 0、edge に内外 2 リング
6. 視野外 polygon → 全ゼロ（bbox フィルタ経路）
7. はみ出し polygon → フレーム縁に偽エッジが出ない（polylines 方式のピン）

`class TestCopperEdgeMatcher`:
1. (+7,−4)px ずらした矩形リング → `offset.px ≈ (7,−4)` ±1px
2. 完全一致 → (0,0)
3. 観測エッジ半分欠損でも復元
4. ノイズエッジ追加でも復元
5. 空マスク → None
6. 窓外シフト → `|offset| ≤ 窓` かつ mean_distance 大
7. `crop_size` 外の想定エッジが結果に影響しない

## 実装ステップ（エージェントチーム運用）

ブランチ: **`feature/20260611/copper-alignment`**（main から。旧ブランチは merge 済みのため）

1. 本計画を `memory/agents/implementation-planner/copper-alignment.md` へ配置
2. **spec-test-author ∥ plan-implementer を並列起動**（パターン A: 公開 IF はシグネチャ確定済み、tests/ と src/ は disjoint）
   - spec-test-author: 上記テスト 14 件を tests/pcbasm/posctrl/test_copper.py に
   - plan-implementer: posctrl/copper.py + `__init__.py` + デモ書き換え
3. 合流: `make format && make type && make test-no-hardware` で整合確認、不整合は該当 agent へ差し戻し
4. code-simplifier → docs-keeper
5. コミット分割:
   - `feat(posctrl): 設計銅箔のカメラ投影 CopperProjector を追加`
   - `feat(posctrl): chamfer マッチングの CopperEdgeMatcher を追加`
   - `feat(scripts): copper_detection を board キャリブレーション連携の対話デモに拡張`
6. **gitlab-mr skill** で push + 新 MR 作成（target: main）

## 検証

- 各コミット前に `make format && make type && make test-no-hardware` グリーン
- 実機検証は**ユーザー実施**: 初回は `move` でステージを +x に動かし、overlay が想定方向へ動くか（鏡映がないか）を必ず目視確認 → その後 adjust 連打で dx,dy → 0 へ収束するか確認

## 落とし穴（実装時の注意）

- **投影アンカー**: live 位置投影だと adjust が収束しない（上述）
- **ppm の crop 前提**: キャリブレーションは crop 領域で計測。マッチングは crop 限定、全画面 overlay は目視用と割り切る
- 画像中心は「全画面中心」基準に統一（`crop_center` の //2 丸めずれ回避）
- matchTemplate は探索領域 < テンプレートで例外 → 窓クランプ
- 遠方 polygon の int32 オーバーフロー → bbox フィルタ後に `np.round().astype(np.int32)`
- pyright × cv2（distanceTransform/matchTemplate/minMaxLoc）は `ImageArray` で受けタプル分解
- 振る舞いクラスは素のクラス + `_` private、データは `@attrs.frozen`（既存流儀）

## 参照ファイル

- `src/pcbasm/posctrl/setup.py` — setup_board_calibration / OffsetObserver / machine_session
- `src/pcbasm/posctrl/position.py:81`, `offset.py:111`, `board.py:106-152` — 符号根拠
- `src/scripts/posctrl/board_tour.py` — machine/pcb 引数・移動パターンの雛形
- `src/pcbasm/pcb/board.py:270` (Copper), `src/pcbasm/vision/detection.py:14` (Offset)
