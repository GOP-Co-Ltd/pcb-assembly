# region-alignment-average — spec-test-author ノート

ブランチ `feature/20260729/region-alignment-average`。計画書
`memory/agents/implementation-planner/region-alignment-average.md` を正典として `tests/` のみを書いた。
`src/` は一切触っていない（`plan-implementer` が並列実装）。

## 検証状況

`make format` / `make type`（0 errors）/ `make test-no-hardware`（1655 passed）/ `make test-e2e`（51 passed）
すべて通過。`grep -rn '</content>' src tests` 該当なし。実機テスト（`make test` / `@mark_hardware`）は未実行。

**期待される失敗は残っていない。** 書き終えた時点で実装が既に着地していたため、仕様 first で書いたテストは
全て緑になっている（下記の期待値は実装を読んで合わせたのではなく、計画書の実測値と
`scratchpad/validate_fixtures.py` で planned アルゴリズムを再実装した独立検証から決めた）。

## 今回のバグの直接ピン（最重要 5 点の所在）

| ピン | テスト |
|---|---|
| 開口問題の棄却（真の平坦域） | `tests/pcbasm/posctrl/test_copper.py::TestCopperEdgeMatcherRejection::test_flat_cost_surface_from_one_directional_edges_returns_none` — 探索領域を縦断する水平線 1 本で `match` が `None`。棄却の根拠が「窓端張り付き」ではなく拘束不足であることは `test_flat_cost_surface_is_reported_as_zero_sharpness`（`min_sharpness=0.0` にすると `sharpness == approx(0.0, abs=1e-6)` で、拘束のある y 軸だけ正しく +2px を返す）が別に固定 |
| 弱い拘束（端点だけが x を拘束） | 同クラス `test_weakly_constrained_edges_are_rejected_by_default_threshold`（実測 sharpness 0.072 < 0.1、既定 0.15 で棄却） |
| サブピクセル復元 | `TestCopperEdgeMatcherTranslation::test_match_recovers_subpixel_shift` — `cv2.circle(shift=4)` の円 4 個を (+2.4,−1.2) / (−1.6,+0.8) / (+0.5,+0.5) ずらし `abs=0.15` px で復元。実測誤差は 0.087 / 0.087 / 0.038 px。**0.1px を切る期待値は書いていない** |
| 0 飽和の消滅 | `test_subpixel_shift_reports_nonzero_rms_distance`（rms > 0.2、実測 0.49）と `test_exact_match_reports_zero_but_non_negative_rms`（整数完全一致で `approx(0.0, abs=1e-3)` かつ `>= 0.0`。FFT 丸めの負値対策 `max(c*,0)` のピン） |
| レバー腕非依存（MR !149 の移植） | `tests/pcbasm/posctrl/test_aligner.py::TestRegionAlignmentIsPureTranslation::test_displacement_is_independent_of_lever_arm` — `offset_transform` を Identity / Rotation(30) / `Compose([Rotation(90), Shift])` / **`Compose([Rotation(30), Scale.flip(y=True)])`（det<0 の鏡映）** で parametrize。鏡映ケースは落としていない |
| エッジ検出が 1px 細線 | `tests/pcbasm/vision/test_copper.py::TestCopperEdgeDetector::test_detected_edges_are_one_pixel_thin` — 2×2 が全部エッジになるブロックが 0 個。将来 `cv2.dilate` が 1 行入ると赤くなる |

## 書いたテストと固定した仕様

### `tests/pcbasm/posctrl/test_copper.py`（書き直し）

- `TestCenteredRoi` — 画像中心配置 / 偶奇どちらも一辺ちょうど / 短辺クランプ / `size_px < 1` の ValueError
- `TestCopperProjector` — 既存の投影公式・符号・穴・クリップ偽エッジのピンを維持。追加で
  `board_to_pixel_affine` が `pixel_of` と一致すること、matrix が `stage_xy` に依存しないこと
  （参照アンカー 1 点で全候補を採点できる根拠）、`polygons` プロパティ
- `TestCopperEdgeMatcherTranslation` — `window_px = round(sw*ppm)` / 整数ずれ / サブピクセルずれ /
  rms の非飽和と非負 / 観測欠損・ノイズ耐性 / ROI 外構造の非影響 / ROI 境界横断エッジ
- `TestCopperEdgeMatcherRejection` — 観測空・ROI 内想定エッジ空・平坦域・弱拘束・窓端張り付き
  （`search_window` 20px に対し 30px ずれ）・`min_sharpness` の閾値効果（0.0/0.5 で採択、0.9 で棄却）・
  sharpness の順序（正方リング > 0.5 実測 0.7059 ＞ 水平線 < 0.1 実測 0.072）
- `TestEdgeMatch` — `camera_transform` が純並進

### `tests/pcbasm/posctrl/test_region.py`（新規）

- λ_min 採点: 等方リングの領域は予測 sharpness > 0.5 / 水平エッジのみの銅箔は候補全滅（空リスト）/
  count=1 なら一方向側でなく等方側を選ぶ / 銅箔ゼロで空リスト
- 貪欲選択: 選択領域が board 上で `region_size_px / ppm` 以上離れる / count 上限 / 候補不足なら候補数まで
- 巡回順: `tour_start` に近い領域が先頭、`index` は巡回順に 0 から振り直し（起点を反転する parametrize）
- ROI/anchor: 全 region で同一の画像中心正方形 / `anchor = board_transform.apply(領域中心)` /
  **回転 board_transform でも ROI は pixel 空間の正方形**
- 引数検証 4 種の ValueError

### `tests/pcbasm/posctrl/test_aligner.py`（`git mv test_pad.py` 後に全面書き直し）

- `TestRegionAlignerMeasure` — アンカーへ移動し **撮像 1 回**（`FakeCamera.capture_count == 1`）/
  既知ずれ (+6,−4)px → translation ≈ (−0.6,+0.4) mm / rms・sharpness が結果に載る /
  frame_sink に 1 枚 / 照合失敗で RuntimeError / `max_correction_mm` 超過で RuntimeError / 上限内は成功
- `TestRegionAlignmentTranslation` — `translation = M(anchor) − anchor`
- `TestRegionAlignmentIsPureTranslation` — 上表のレバー腕ピン + 40mm 離れた点でも translation が一致

### `tests/pcbasm/posctrl/test_alignment.py`（書き直し）

- `TestBoardAlignment` — 平均 / `Shift(translation)` / 母標準偏差（ddof=0）/ 1 領域で spread=(0,0) /
  `results=()` の ValueError
- `TestRegionAlignmentSession` — `region_roi == centered_roi(解像度, region_size_px)` /
  `region_size_px + 2*window_px > 解像度` で構築時 ValueError（解像度 (400,300) で検証）/
  `plan_regions` が TOP pad のみを使い roi を共有 / `measure` の成功（撮像 1 回）/
  失敗時は警告 log の後 None / frame_sink / `corrected_projector` の合成順（T_b 先、M 後）

### `tests/pcbasm/posctrl/test_render.py`（更新）

`PadResultRenderer` を新シグネチャ（`roi: PixelRect` 直接指定、`pad_align` / `roi_polygons` 削除）へ。

### `tests/pcbasm/test_config.py` / `tests/webui/test_config_store.py` / `tests/webui/routers/test_settings_api.py` / `tests/e2e/test_webui_e2e.py`

`max_failures` / `tolerance` / `min_roi` の参照を `region_size_px` / `region_count` / `min_regions` /
`min_sharpness` へ。1 未満の int と負の `min_sharpness` の拒否、`min_sharpness = 0.0` は有効値（境界）。

### `tests/webui/jobs/test_board_ops.py`

`measure_regions` を実 `JobManager` + 実 `JobContext`（合成ジョブ経由）で回す:
全成功で平均 / ログに毎領域 dx/dy/rms/sharpness と平均・ばらつき / 失敗領域で `on_failure` を呼んで続行 /
成功数不足の ValueError（成功 1・必要 2・計画 2 を部分一致で）/ 計画数不足なら `session.measure`
呼び出し 0 回で ValueError。

### `tests/helpers.py`

`FakeCamera.capture_count` を追加（撮像回数のピン用。既存挙動は変えていない）。

## 削除した既存テストと引き継ぎ先（カバレッジの取りこぼしなし）

| 削除したテスト | 引き継ぎ先 |
|---|---|
| `test_copper.py` の `roi_of` 5 テスト | `roi_of` 自体が削除。ROI 決定の責務は `TestCenteredRoi` 4 テストへ |
| `test_expected_edges_outside_crop_do_not_affect_match`（`crop_size`） | `test_match_uses_only_template_inside_roi`（ROI 外構造の非影響） |
| `test_shift_beyond_window_stays_in_window_with_large_distance` | `test_shift_beyond_search_window_returns_none`（窓端は None が新契約） |
| `test_identical_masks_match_with_zero_offset` の `mean_distance_px` | `test_exact_match_reports_zero_but_non_negative_rms` |
| `TestCopperPadObserver` 4 テスト | `TestRegionAlignerMeasure`（observe→measure に契約が移動） |
| `TestPadAlignmentResult` 2 テスト | `TestRegionAlignmentTranslation` |
| `TestPadAlignmentIsPureTranslation`（det<0 含む） | `TestRegionAlignmentIsPureTranslation` に丸ごと移植（**鏡映ケース維持**） |
| `TestGroupPadsByComponent` 4 テスト | `group_pads_by_component` ごと削除。対応する振る舞いが存在しない |
| `TestComponentAlignments::test_result_of…` / `test_unregistered_designator…` | designator lookup 自体が消滅（補正は基板全体で 1 つ） |
| `test_corrected_board_transform_composes_board_then_machine` | `test_corrected_projector_projects_with_composed_board_transform` が同じ合成順（T_b 先・M 後）を固定 |
| `test_board_correction_is_conjugation_of_machine_transform` | **引き継ぎ先なし（意図的）**。board 座標系での共役補正 `T_b⁻¹∘M∘T_b` は上位計画で廃止され、補正は機械座標へ出る瞬間に 1 回だけ掛ける。共役の代数は `src/` から消えているので、テストを残すと存在しない契約を固定してしまう |
| `TestSortedTopComponentPads` | TOP フィルタは `test_plan_regions_uses_top_pads_and_the_shared_roi`（BOTTOM の pad/銅箔を 300mm 離して置き影響しないことを見る）へ |
| `test_warns_when_tolerance_below_diagonal_pixel` 3 ケース | `tolerance` ごと削除（1 ショット化で収束閾値の概念が消えた） |
| `test_align_delivers_edge_match_frames_to_frame_sink` | `test_measure_delivers_edge_match_frames_to_frame_sink`（枚数が 1 に確定） |
| `test_empty_roi_polygons_falls_back_to_min_roi` | `test_expected_contour_outside_the_given_roi_is_not_drawn`（小さな ROI 外の輪郭は描かない） |
| `TestPadAlignAbortMessage` 4 テスト | `TestMeasureRegions` の中止 2 テスト（メッセージは部分一致） |
| `max_failures` の config / config_store / settings_api / e2e テスト | 同ファイルの `region_count` / `min_regions` / `region_size_px` / `min_sharpness` 版 |

## 変更していないファイル

`tests/pcbasm/posctrl/test_correction.py`（符号規約 A2/A4 の安全網、1 行も触っていない）。
`tests/webui/test_preview.py` / `tests/webui/routers/test_pages.py` は canny 系のみ参照で変更不要（確認済み）。
`tests/e2e/test_browser_ui.py` / `test_paste_solder_browser.py` は削除キーを参照していない（grep 確認済み）。

## 計画書で曖昧だった点・判断したこと

1. **平坦域ケースの棄却経路**。計画書は「真の平坦域 → sharpness 0.0000 で棄却」「水平ストライプ →
   `min_loc` が窓端で `None`」と別扱いだが、平坦域のタイブレークは FFT 丸め誤差が決めるので
   どちらの guard が先に効くかは合成入力に依存する（自分の fixture では窓端が先に効く配置もあった）。
   そこで **観測可能な契約（`match` が `None`）を主テストにし、sharpness ≈ 0 は
   `min_sharpness=0.0` を渡した別テストで固定**した。デタラメな dx の値自体は非決定なのでアサートしていない。
2. **棄却側の sharpness 実測値**。計画書は「棄却ケースは 0.06 以下を使え」としているが、
   端点が探索領域内に入る水平線では 0.072（ROI 内に収めると 0.081）にしかならなかった。
   閾値 0.15 との比は 2 倍あるので `< 0.1` で切った。厳密に 0.0000 が出るのは
   「観測が探索領域を縦断する」配置（`_horizontal_line(85, 315, ...)`、探索窓 ±20px の外まで伸ばす）で、
   そちらを平坦域テストに採用している。
3. **`measure_regions` の `session` 引数**。`RegionAlignmentSession` は HAL ABC ではないが、
   このループの契約は `measure(region) -> RegionAlignment | None` だけに依存する。実 session を
   組むと領域ごとの成功/失敗を画像で作り分ける必要があり本質から外れるため、
   `mocker.Mock(spec=RegionAlignmentSession)` を使った（pyright 対策で `MockType` + `cast`）。
   session 自身の HAL 結合は `test_alignment.py` が実 projector / 実 Canny / FakeCamera で押さえている。
4. **`--strict-markers` により `@pytest.mark.api_contract` は使っていない**（`pyproject.toml` の
   `markers` に未登録。登録するかは orchestrator 判断。今回は公開 API 名の契約ピンを別途置く必要は
   薄いと判断した）。
5. **`board_tour` の `min_regions=1` 固定**（orchestrator 裁定）は `src/webui/jobs/posctrl.py` 側の
   呼び出しなので `measure_regions` のテストでは `min_regions` を引数で振るだけにした。
   `board_tour` 経由の通しは実機確認（ユーザー）に委ねる。

## 実装側への修正要求

なし。計画書のシグネチャ・アルゴリズム・実測値と実装が一致していた。

---

## code-reviewer 指摘への対応（追記）

レビュー全文は `memory/agents/code-reviewer/region-alignment-average.md`。3 件すべて対応済み。
再検証: `make format` / `make type`（0 errors）/ `make test-no-hardware`（**1660 passed**、+5）。
`grep -rn '</content>' tests` 該当なし。

### 1. must-fix — `_fit_sharpness` の交差項（Sxy）が未検証だった

`tests/pcbasm/posctrl/test_copper.py::TestCopperEdgeMatcherRejection` に 2 テスト追加。

- `test_diagonal_one_directional_edges_are_rejected`（`angle` 45 / 135 で parametrize）—
  **斜め 45° の一方向エッジ**だけの領域で `match` が `None`、かつ `min_sharpness=0.0` を渡したときの
  `sharpness < 0.05`（実測 0.0000）。軸平行では `Sxy = 0` になるので交差項を通らない。
- `test_diagonal_ring_is_accepted` — 45° 回した正方リング（`_diamond`、斜めエッジだけで等方）は
  **採択**される（実測 sharpness 0.577）。交差項が効くケースを一律に落としていないことの対偶ピン。

**変異テストの確認結果**: `src/pcbasm/posctrl/copper.py:239` の `hxy = a5` を一時的に `a5 / 2` へ改変
（sed で 1 行のみ）して `pytest -k diagonal` を実行 →

```
test_diagonal_one_directional_edges_are_rejected[45]  FAILED   (sharpness 0.0 → 0.298 で採択されてしまう)
test_diagonal_one_directional_edges_are_rejected[135] FAILED
test_diagonal_ring_is_accepted                        PASSED   (過敏になっていない)
→ 2 failed, 1 passed
```

逆 sed で復元し、**改変前後の sha256 一致を確認**（`restored: YES`）、`grep -rn "a5 / 2" src/` 該当なし、
`git diff src/pcbasm/posctrl/copper.py` に `a5` の変更行が無いことも確認済み。`src/` に恒久変更なし。

**フィクスチャ設計の注意**: レビュー指示は「探索領域を斜めに縦断させて真の平坦域を作る」だったが、
縦断させると最小コスト位置が探索窓の端に張り付き **窓端 guard が先に効いて改変版でも `None` になる**
（変異を検出できない）。実測:

| 斜め線の配置 | 正しい実装 | `hxy = a5/2` 改変版 |
|---|---|---|
| 探索領域を縦断 | None | None ← **識別できない** |
| 両端が ROI 内（120..280） | None（sharpness 0.0000） | **採択**（sharpness 0.2984） |

そのため両端を ROI 内に収める配置を採用し、理由をヘルパ `_diagonal_line` の docstring に書いた。

### 2. should-fix — 予測 sharpness と実測 sharpness の同一スケール

`tests/pcbasm/posctrl/test_region.py::TestPredictedSharpnessMatchesMeasured` を新設。
領域を計画 → その `anchor` で投影した想定エッジ自身を `CopperEdgeMatcher.match`（`min_sharpness=0.0`）に
かけ、`sqrt(constraint / edge_length_px)` と実測 `sharpness` を比較する。

| フィクスチャ | 予測 | 実測 | 乖離 | 許容 |
|---|---|---|---|---|
| 軸平行の正方リング（等方の上限 sqrt(1/2)） | 0.7071 | 0.7051 | +0.3% | `rel=0.05` |
| 向きの異なる矩形 4 個（0°/30°/60°、実 PCB に近い雑多さ） | 0.7067 | 0.6678 | +5.8% | `rel=0.15` |

レビュアーの実 PCB 実測（予測 0.633〜0.682 / 実測 0.595〜0.662、5〜7% 楽観側）と同じ帯に入る合成
フィクスチャを選んだ。**楽観側にずれる向き**も `predicted >= measured` で固定した。
正規化を片方だけ変えると 1.4〜2 倍ずれてこの許容を外れる（照合側の `2n` を `n` にすると実測が ×1.414、
予測側の `sqrt` を落とすと 0.50 になる）。

なお 45° 回した正方リング単体は予測 0.7071 / 実測 0.5774（+22%、比が sqrt(3/2)）になる。
Bresenham の階段状ラスタライズで斜め線への chamfer 距離が理想線分からずれるためで、
**予測と実測の一致度はジオメトリの向きに依存する**（斜めが支配的な領域では予測が 2 割楽観になる）。
実機で `sharpness` が予測より低く出た場合の説明としてノートに残す。

### 3. should-fix — `mocker.Mock(spec=RegionAlignmentSession)` を手書き stub へ

`tests/webui/jobs/test_board_ops.py` に `_StubSession`（`measure(region)` のみを持ち、
与えた列を順に返し、呼ばれた領域を `measured` に記録する 15 行のクラス）を置き、mock fixture と
`pytest_mock` の import を削除。呼び出し回数の検証は `session.measure.call_count` から
`[r.index for r in session.measured]` へ変え、**どの領域が実際に計測されたか**まで見るようになった。
`measure_regions` のループ契約（progress / checkpoint / log / on_failure / min_regions 判定）の
テストは 5 本すべて維持（実 `JobManager` + 実 `JobContext` の合成ジョブ経由も維持）。
`cast(RegionAlignmentSession, session)` は pyright 用に残している。

---

## 追記（2026-07-29）— MR !151 ユーザー要求 2 件への `tests/` 追随

`src/` 側は着地済み（`PadAlign.board_edge_margin` 追加 / `plan_alignment_regions` の
`pad_centers` → `safe_area` 置換 / `edge_length_px` → `edge_point_count` / `_clipped_lengths` 削除）。
`tests/` のみを追随させた。`src/` は 1 行も触っていない。

### 1. 外周マージンの直接ピン（ユーザー要求 1 の生きた仕様）

**`tests/pcbasm/posctrl/test_region.py::TestBoardEdgeMargin`** を新設。60×40mm の外形
（`shapely.box(0, 0, 60, 40)`）に 5mm ピッチで小さな銅箔島を敷き詰め（隅から 1mm まで）、
「外周寄りの候補が *銅箔が無いから* ではなく *ROI が外周に掛かるから* 落ちる」地形を作った。

- `test_every_selected_roi_stays_inside_the_shrunk_outline` — `margin ∈ {0, 1, 2, 3}` ×
  `board_transform ∈ {Identity, Rotation(20°)}` の 8 通りで、**選ばれた全領域の ROI を board 座標へ
  写した多角形が `outline.buffer(-margin)` に収まる**ことを検証。回転があると ROI は board 空間で
  軸平行にならないので中心だけでは足りず 4 隅すべてを見る。空振り防止に `assert regions` も置いた。
- `test_margin_keeps_the_roi_away_from_the_board_edge` — margin 0 と 3mm の対比。
  margin 0 では外形線に接する領域が実際に選ばれる（最小距離 0.0mm）が、3mm では 7.7mm まで離れる。
  anchor 集合が一致しないことも合わせて assert（マージンが効いていることの対偶）。
- `test_margin_larger_than_the_board_yields_no_regions` — `buffer(-25)` が空 → **領域 0 個・例外なし**
  （中止判定は `min_regions` を持つ呼び出し側の責務）。
- `test_safe_area_smaller_than_the_roi_yields_no_regions` — ROI が収まる格子点が無ければ空リスト。

ROI の board 座標への写像は `_roi_in_board()` ヘルパで、実装の行列計算を複製せず
**投影公式そのもの**から導いた：`offset_transform` が Identity なので anchor へ移動したときの ROI は
機械座標で anchor 中心・一辺 `region_size_px / pixel_per_mm` の軸平行正方形。board へは
`board_transform.inverse()` で戻す。dense sampling（`projector.pixel_of` で 4 万点）と
一致することを確認済みで、解析式のほうを採用した（正確かつ 3 桁速い）。

**変異検出を確認**: `region.Polygon` を `within → True` の偽物に差し替えると（= ROI 包含ゲートの撤去）
上記 2 本がどちらも赤くなる。

セッション層のピンは **`test_alignment.py::TestRegionAlignmentSession`** に 1 本追加：
`test_plan_regions_shrinks_the_outline_by_board_edge_margin` — 外形 ±30mm 角なら ROI（400px = 40mm）が
収まる格子点は ±10mm に出るが、既定マージン 2mm を引いた ±28mm 角には 1 つも残らないので領域 0 個。
外形をそのまま使っていれば領域が選ばれるので、`outline.buffer(-board_edge_margin)` を
実際に使っていることのピンになる。

### 2. 落ちていた 41 件の修正内容

| ファイル | 件数 | 修正 |
|---|---|---|
| `test_region.py` | 18 | ヘルパ `_plan` の第 2 位置引数 `pad_centers` を削り `safe_area=` へ。候補集合を safe_area で設計し直した（下記） |
| `test_aligner.py` | 10 | `_region()` の `edge_length_px=240.0` → `edge_point_count=240`（1 箇所） |
| `test_alignment.py` | 8 | `_region()` を同上 + `pcb` fixture に `pcb.outline = Outline(_square(0, 0, 35))` を追加 |
| `test_board_ops.py` | 5 | `_region()` を同上（1 箇所） |

`test_region.py` の候補集合の作り直しが要点。`safe_area` は候補格子の定義域と ROI 包含判定を兼ねるので、
`pad_centers` 時代の「1 点だけ渡せば候補 1 個」が使えない。2 種類の safe_area ヘルパを用意した：

- **`_solo_area(cx, cy, region_mm)` = `Point(cx, cy).buffer(0.9 * region_mm)`** — 候補を中心 1 点に絞る。
  中心の ROI（対角半径 `0.707*region_mm`）は収まるが、格子間隔 `0.45*region_mm` 離れた隣接点の ROI は
  必ず隅が円外に出る。**`2r/step = 3.6` が整数から離れている**ことが重要で、
  `region_mm = region_size_px / hypot(matrix[0,0], matrix[1,0])` は回転があると
  `9.999999999999991` のように振れ、`_candidate_grid` の `math.ceil` が 1 段跳ねる
  （実測: `Rotation(30°)` で格子間隔が 5.0 → 4.0 に変わり中心が格子から外れた）。
  半径を `1.0 * region_mm` に取るとこの丸めで壊れるので 0.9 倍にしてある。
- **`WIDE_AREA = box(-25, -10, 25, 10)`** — 貪欲選択・巡回順・count 上限など複数領域が必要なテスト用。
  x は ±20mm・y は ±5mm まで候補になり、`pad_centers` 時代と同じ候補集合（±15/0mm）を再現する。

`test_anchor_is_the_board_point_mapped_to_machine_coordinates` は
`Shift(5, -1) / Rotation(30°) / Rotation(-45°)` の parametrize に**強化**した
（旧テストは `Rotation(30°)` 単体。`_solo_area` が回転下でも候補 1 点を保つので並進と線形部の両方を見られる）。

### 3. 削除したテストと引き継ぎ先

| 削除 | 引き継ぎ先 | 理由 |
|---|---|---|
| `test_empty_pad_centers_raises`（`pad_centers` が空 → `ValueError`） | `TestBoardEdgeMargin::test_margin_larger_than_the_board_yields_no_regions` と `::test_safe_area_smaller_than_the_roi_yields_no_regions` | `pad_centers` 引数自体が無くなった。「領域選定の定義域が空」の契約は *例外* から *空リスト* へ変わったので、同じ状況を新しい契約で固定し直した |
| `TestPlanAlignmentRegionsSelection.PADS` 定数 | `WIDE_AREA` | 候補格子の供給元が置き換わっただけ。候補集合（±15/0mm）は同一 |

`test_plan_regions_uses_top_pads_and_the_shared_roi` は
`test_plan_regions_uses_the_board_outline_and_the_shared_roi` へ改名（削除ではなく仕様追随）。
BOTTOM 層の銅箔（300mm 離れた位置）が候補にならないことの検証は維持している
（外形の外なので `safe_area` に入らない）。

`TestPredictedSharpnessMatchesMeasured` は許容（`rel=0.05` / `0.15`、`predicted >= measured`）を**据え置き**。
点サンプリング化で予測値はほぼ不変（軸平行リング 0.7071、混在 0.7067 → 0.7066）で実測も不変。
class docstring の実 PCB 数値だけ MR !151 の実測（margin 2mm で予測 0.649〜0.681、
予測/実測 1.030〜1.120、斜め支配で最大 1.225）へ更新した。点サンプリング密度を上げても比は縮まらない
（`sqrt(λmin/count)` は密度に不変）ことも書き添えてある。

### 4. 追加した新規テスト（設定系）

- `tests/pcbasm/test_config.py::TestPadAlignRegionSettings` — 既定 2.0 の assert 追加、
  `test_rejects_negative_board_edge_margin` / `test_zero_board_edge_margin_is_allowed`
- `tests/webui/test_config_store.py::TestPadAlignRegionFields` — 読み書き往復（1.5）を parametrize に追加、
  `test_board_edge_margin_is_a_float_field_in_millimetres`（`value_type == "float"` / `unit == "mm"`）、
  負値拒否、0 許容（`probe.board_edge_margin` が 0 を弾くのと**逆**なので明示的に分けてある）
- `tests/webui/routers/test_settings_api.py` — `test_put_negative_board_edge_margin_returns_400`
- `tests/e2e/test_webui_e2e.py` — pad_align キー集合に `board_edge_margin` を追加

### 5. 全緑の確認結果

- `make format` — 通過（ruff-format / docformatter が初回に整形、2 回目クリーン）
- `make type` — pyright エラー 0 / 警告 0
- `make test-no-hardware` — **1680 passed, 87 deselected**
- `make test-e2e` — **51 passed**
- `grep -rn '</content>' tests` — ヒット 0

`tests/pcbasm/posctrl/test_correction.py` は 1 行も変更していない（実機検証済み符号規約 A2/A4 の安全網）。
既存の重要ピン（斜め一方向で `match is None` / サブピクセル復元 0.15px / `rms_distance_px` / 純並進
`machine_transform` の det<0 parametrize / `CopperEdgeDetector` の 1px 細線 / `_StubSession` 手書き）は
いずれも触っていない。

### 6. 実装側への申し送り（テスト側では直さない）

`_candidate_grid` の `math.ceil((hi - lo) / step)` が丸め誤差に敏感。`board_transform` に回転があると
`region_mm` が `10.0` ではなく `9.999999999999991` になり、`ceil(20 / 4.9999...)` が 4 ではなく 5 を返して
**格子間隔が 5.0mm から 4.0mm へ跳ぶ**（実測、`Rotation(30°)`）。領域選定の結果が回転角の
丸め誤差で変わるので、`step` を `region_mm / 2` そのままではなく相対誤差を吸収した形にするか、
格子点数を `round` ベースにする改善余地がある。実機挙動を壊す不具合ではない（候補が少し粗く／細かく
なるだけ）のでテスト側は 0.9 倍半径の safe_area で回避しており、修正要求ではなく観測の共有。
