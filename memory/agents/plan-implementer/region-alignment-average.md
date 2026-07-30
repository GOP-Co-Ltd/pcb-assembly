# 銅箔照合を「数領域の平均補正」へ作り直す（src 側実装）

計画書: `memory/agents/implementation-planner/region-alignment-average.md`（正典）。
担当は `src/` と `data/` の設定テンプレートのみ。`tests/` は一切触っていない。

## 計画外の判断ログ

1. **`copper.py` の `import math` は残した。** 計画書「実装ステップ 1」は
   「`roi_of` と `import math` 削除」と書いているが、確定アルゴリズム側が
   `math.hypot`（二次形式の λ_min）と `math.sqrt`（sharpness / rms）を使うので
   削除できない。`roi_of` だけ削除した。計画書内の記述矛盾。

2. **`match` の二次形式当てはめとサブピクセル補間をモジュール private 関数へ切り出した**
   （`_fit_sharpness` / `_parabolic_subpixel`）。`match` 本体が 1 メソッドに
   80 行超になるのを避けるため。公開 IF は変わらない。

3. **`plan_alignment_regions` の内部分割**: `_projected_segments` / `_clipped_lengths` /
   `_candidate_grid` の 3 つの private 関数へ分けた。計画書の擬似コードと 1:1 で対応する。

4. **`PadAlign.__attrs_post_init__` を 3 キーのループで書いた**（`getattr` で回す）。
   同じ「1 以上の整数」検証を 3 回並べるより短い。エラーメッセージは
   `f"{name}は1以上の整数である必要があります: {value}"`。

5. **`posctrl/__init__.py` の `__all__` をアルファベット順に直した。** 元は
   `BoardTransformMeasurer` が `ComponentPads` の後ろにあって順序が崩れていた。
   計画書が「アルファベット順を維持」と書いているので、崩れを直すのが正しいと判断した。

6. **`webui/jobs/pasting.py` から `ResolvedInitialPurge` の import を削除した。**
   `_initial_purge_point` を消したことで唯一の利用が消えた orphan。計画書に明記は
   ないが「自分の変更で生じた orphan は消す」に従った。
   なお `Machine` / `Pad` の unused import は**元から** dead なので触っていない
   （ruff は F401 を ignore 設定にしているので検出されない。`uvx ruff check --select F401`
   で base と比較して確認済み）。

7. **`board_tour` の `min_regions=1` 固定** と **`pad.py` → `aligner.py` の `git mv`** は
   orchestrator の裁定どおり。

## code-reviewer への対応（2 巡目・verdict approve / must-fix なし）

orchestrator が採用した should-fix 3 件に対応した。却下された S3（外れ値除去）/
S7（README）/ nit 12 件は**触っていない**。`spread` のログ出力も現状維持。

### S4 — 到達不能なクランプを削除

`copper.py` の `_parabolic_subpixel` から `min(max(..., -0.5), 0.5)` を外した。
`c0` は `minMaxLoc` が返す探索格子全域の最小値なので `cxm - c0 >= 0` かつ
`cxp - c0 >= 0`、よって `|ds| <= 0.5` が恒真という根拠を docstring に明記した。
`denx > 0` / `deny > 0` のガードは残置（凸でない軸は実際に起こり得る）。

**削除前後で数値が完全一致することを確認済み**（`verify_impl.py` の sharpness 5 ケース・
サブピクセル 21 通り・最大誤差 0.1256px が全て bit 一致）。クランプが実際に
到達不能だったことの実測裏付け。

### S5 — board_tour の overlay に rms / sharpness を出す

上位計画 §6 の未達項目を解消した。`render_edge_match`（照合**前**に作られるので
`EdgeMatch` を持てない）ではなく、既存の `PadResultRenderer.render(image, lines)`
経路に載せた。**renderer のシグネチャは変更していない。**

- `measure_regions` に `on_success: Callable[[RegionAlignment], None] | None = None`
  を追加（`on_failure` と対称。**既定 None の追加キーワード引数なので既存呼び出しは無影響**）
- `posctrl.py` に `_region_overlay` を切り出し、`render_measured` / `render_failed`
  の両方から使う。成功時の行は
  `["Region N/M", "dx=... dy=... mm", "rms=... px", "sharpness=..."]`
- 呼ばれる時点でステージは領域アンカーに留まっているので、overlay は
  その領域を映す（`on_failure` と同じ前提）
- `paste_solder` 側は `on_success` を渡していない（塗布中に 1 領域 1 秒の
  overlay 配信を挟みたくないため）

### S8 — 中止メッセージに原因と対処を書く

`board_ops.py` の 2 つの `ValueError` を書き換えた。**アルゴリズムは変えていない。**

- 計画領域数不足: 「領域は互いに領域サイズ以上離して選ぶため、pad の分布が
  region_size_px の数倍に収まる小さい基板では必要数を確保できません」＋
  対処「region_size_px を小さくするか、region_count と min_regions を下げてください」
- 成功領域数不足: 照明・Canny 閾値（キー名を明記）に加え「ログの sharpness が
  min_sharpness を下回っているなら min_sharpness を下げるか region_size_px を
  大きくし、それでも足りなければ min_regions を下げてください」
- 既存テストがピンしているトークン（`"1 個"` / `"必要 3"` / `"成功 1"` /
  `"必要 2"` / `"計画 2"`）は**すべて保持**した

## 他 implementer への IF 変更通知（並列時）

計画書「公開インターフェース」節からの逸脱は 2 巡目の S5 対応で入れた
`measure_regions(..., on_success=None)` の**追加キーワード引数 1 件のみ**
（既定 None・既存呼び出しは無影響）。他のシグネチャは全て計画どおり。
`spec-test-author` が使う想定の追加情報:

- `CopperEdgeMatcher.match` は `roi` が第 3 位置引数（キーワードでも可、既定値なし）
- `plan_alignment_regions` の `ValueError` メッセージ:
  `"region_size_pxは1以上である必要があります: {n}"` /
  `"countは1以上である必要があります: {n}"` / `"pad_centersが空です"` /
  `"region_size_px {n} が画像サイズ {size} を超えています"`
- `centered_roi` の `ValueError`: `"size_pxは1以上である必要があります: {n}"`
- `BoardAlignment(results=())` の `ValueError`: `"BoardAlignmentには1件以上の領域計測が必要です"`
- `RegionAligner.measure` の照合失敗 `RuntimeError`:
  `"領域 {index} の銅箔エッジ照合に失敗しました（拘束不足または観測エッジなし）"`

## 計画書の実測値との突き合わせ（確認済み）

自作の合成フィクスチャ（`region_size_px=400` / `window_px=42` / `search_window=1.4mm` /
`pixel_per_mm=30.225`）で実装を走らせた結果、計画書の表と **4 桁一致**した:

| ケース | 計画書 | 実装 |
|---|---|---|
| 正方リング 100×100 | 0.7059 | **0.7059** |
| pad 群 6×6 = 24×12 | 0.5693 | **0.5693** |
| 細長リング 240×8 | 0.1758 | **0.1758** |
| 水平線 2 本 + 20px 縦線 | 0.1921 | **0.1921** |
| 水平線 1 本（端点が視野内） | 0.0576 | **0.0576** |
| 水平線 1 本（観測が視野を縦断） | 棄却 | **`None`**（`min_sharpness=0.0` でも窓端ガードで `None`） |

いずれも並進は真値 (+2, +3) を誤差 0 で復元。`min_sharpness=0.15` は
採択側最小 0.1758 と棄却側最大 0.0576 の間に正しく入っている。

**領域計画**も実 PCB `data/TJ-56-67/TJ-56-67.kicad_pcb` で完全一致:
選ばれた 4 領域の board 座標 (16.0, 8.9) (54.8, 8.9) (67.8, 22.0) (80.7, 8.9)、
λmin = 546 / 265 / 265 / 594、予測 sharpness 0.633〜0.682、採点全体 **0.038 秒**。
（計画書は λmin を降順、実装は巡回順で並べているだけで集合は同一）

**サブピクセル**: 円 4 個を −2.0〜+2.0px の 0.2px 刻み 21 通りでずらし、
`(dx, dy) = (d, −d/2)` を復元。**最大誤差 0.1256 px**、整数ずれで誤差 0。
`rms_distance_px` は整数一致で厳密に 0.0（負値にならない）、サブピクセルずれで
0.361〜0.535。計画書の記載（最大誤差 0.110 / rms 0.485〜0.520）とは円の半径・
配置が違うため数値がずれるが、**テスト許容 `abs=0.15` px は満たす**（余裕は
計画書想定より小さく 0.024px しかない）。→ 下記「残課題」参照。

検証スクリプトは
`/tmp/claude-1000/-home-gop-pcb-assembly/b495f7b4-e830-4365-8865-c0a78c6d65d3/scratchpad/verify_impl.py`
と `verify_region.py`。

## 既知の制約・残課題

- **サブピクセルの pixel-locking バイアスはフィクスチャ依存**。私の円配置では
  最大 0.126px 出た。`spec-test-author` が `abs=0.15` で書く場合、フィクスチャ次第で
  余裕が 0.02px 程度しかないケースがある。閾値を厳しくしない（0.1px は達成不可）。
- **8 本水平ストライプの棄却経路が計画書と違う**。計画書は「`min_loc` が窓端 →
  `None`」としているが、私のストライプ（端点が ROI 内）では `min_loc` が窓端に来ず
  sharpness = 0.0526 で棄却された。どちらも既定 `min_sharpness=0.15` で `None` に
  なるので実害はないが、テストで「窓端ガード」をピンするなら
  **観測が視野を縦断する**形状（真に平坦）を使う必要がある。
- **`centered_roi` は `(w - size)` が奇数のとき ROI 中心が画像中心から 0.5px ずれる**。
  `plan_alignment_regions` の採点は厳密な中心 ±half で切るので、両者に 0.5px の
  食い違いが出る。銅箔の切り出しは実質同一で、既定値（1280×800 / 400px）では
  そもそも偶奇が揃うため発生しない。
- **`config/machine.toml`（gitignore・実機設定）は未更新**。削除キーは cattrs が
  無視するが、`region_*` / `min_sharpness` は既定値になる。ユーザーが WebUI
  設定画面で調整する必要がある（MR 説明に書くこと）。
- `src/pcbasm/posctrl/README.md` は未同期（`code-simplifier` の担当）。

## 検証結果

code-reviewer 対応（S4 / S5 / S8）を入れた後の最終状態。`spec-test-author` の
テストも着地済みで、**3 コマンドすべてグリーン**。

- `make format`: **pass**
- `make type`: **pass**（0 errors / 0 warnings。`src/` も `tests/` も 0）
- `make test-no-hardware`: **pass — 1660 passed / 0 failed / 87 deselected**（76 秒）
- `grep -rn '</content>' src`: ヒットなし
- `pad_align` 由来の残骸 grep: なし（board キャリブの `tolerance`、カメラの
  `crop_size`、チェッカーボードの `mean_distance_px` のみ＝別物）
- `make test-e2e` は未実行（orchestrator の合流検証に委ねる）
- **実機テスト（`make test` / `@mark_hardware`）は一度も実行していない**
- `grep -rn '</content>' src tests`: ヒットなし。
- `grep -rn 'tolerance|roi_margin|min_roi|max_failures|mean_distance|roi_of|crop_size' src/`:
  `pad_align` 由来の残骸なし（board キャリブレーションの `tolerance`、カメラの
  `crop_size`、チェッカーボードの `mean_distance_px` のみ残る＝別物）。

---

# 追補（MR !151 に対するユーザー追加要求 2 件）

`src/` と `data/` の設定テンプレートのみ担当。`tests/` は一切触っていない（後続 agent が修正）。

## 実装した 3 変更

1. **基板外周の除外** — `PadAlign.board_edge_margin: float = 2.0`（0 以上を検証）を追加。
   `plan_alignment_regions` は「ROI の 4 隅を board 座標へ写した多角形が `safe_area` に
   `within` で収まる」候補だけを採点する。ROI ごと内側に入れる理由は、削れた銅箔を避ける
   だけでなく、**基板外形そのものの強いエッジを視野に入れないため**（外形線は
   `CopperProjector` の想定エッジに含まれないので、片方向 chamfer では一切ペナルティを
   受けない偽エッジとして働く）。docstring / README に明記した。
2. **`_clipped_lengths`（Liang-Barsky）を削除**し、`_projected_segments` を
   `_projected_edge_points(projector, matrix, shift) -> (P, N)` に置換。線分を
   `max(1, round(length))` 個の約 1px 区間に割り、その中点へ点を置く。点は候補に依存しない
   ので 1 回だけ作り、候補ごとは矩形マスク → `N[inside].T @ N[inside]` の 2 行。
   `AlignmentRegion.edge_length_px: float` → `edge_point_count: int`。
3. **`pad_centers` を廃止し `safe_area: Polygon` に統合** — 候補格子は `safe_area` の bbox に
   張り、ROI 収容判定も同じ図形。`RegionAlignmentSession.plan_regions` が
   `pcb.outline.polygon.buffer(-board_edge_margin)` を作って渡す。空なら領域 0 個
   （例外は投げない。中止判定は呼び出し側 `min_regions` の責務）。**縮めた結果が分裂して
   MultiPolygon になっても、使うのは bbox と `within` だけなので同じに扱える**
   （pyright は `Polygon.buffer` の戻りを `Polygon` と推論するので型注釈は `Polygon`）。

## 実測 1: TJ-56-67 の領域数（既定値は変更不要）

`data/TJ-56-67/TJ-56-67.kicad_pcb`（外形 89.5 × 58.0 mm）/ `pixel_per_mm = 30.225091`
（`config/ov9281_20260729_113115.json`）/ crop 600×600 / `region_size_px = 400`（= 13.234mm）/
`region_count = 4`。候補格子 14 × 10 = 140 点のうち **ROI が safe_area に収まるのは 60 点**、
そのうち銅箔があって `λmin > 0` は 17 点。

| `board_edge_margin` | 取れた領域数 | 予測 sharpness |
|---|---|---|
| 0.0 | 4 | 0.648 / 0.656 / 0.659 / 0.661 |
| 1.0 | 4 | 0.638 / 0.661 / 0.659 / 0.690 |
| **2.0（既定）** | **4** | **0.681 / 0.649 / 0.663 / 0.659** |
| 3.0 | 4 | 0.655 / **0.385** / 0.663 / 0.675 |

**`region_size_px = 400` のまま 4 個取れるので既定値は一切変えていない。** margin 2.0 の
4 領域は board 座標 (15.15, 14.00) / (41.46, 14.00) / (61.19, 14.00) / (67.77, 26.00)、
λmin = 450.0 / 48.1 / 363.4 / 302.3、点数 970 / 114 / 827 / 696。予測 sharpness の最小は
**0.649**、`min_sharpness = 0.15` に対して 4.3 倍の余裕がある。採点全体は **14 ms**。

（margin 3.0 にすると 2 番目の領域が λmin 4.3 / 点数 29 の貧弱な領域に落ちて予測 0.385 まで
下がる。2.0 はまだ健全側。）

## 実測 2: 点サンプリングは「単純化」だが「精度改善」ではなかった

**orchestrator の指示にあった「点で数えれば斜め支配の +22% が直る」は成立しない。**
`sharpness = sqrt(λmin(A) / count)` は `A` と `count` が同じ点集合の和なので、
**サンプリング密度に対して不変**。密度を変えても形状ごとの比は動かない。

同じ 4 領域で、旧（クリップ線分長）と新（点数）の予測を実測 sharpness（想定マスクを
(+2,+3)px ずらして `match`）と比べた:

| 領域中心 | 旧予測 | 新予測 | 実測 | 旧/実測 | 新/実測 |
|---|---|---|---|---|---|
| (15.15, 14.00) | 0.6725 | 0.6811 | 0.6523 | 1.031 | 1.044 |
| (61.19, 14.00) | 0.6500 | 0.6629 | 0.5920 | 1.098 | 1.120 |
| (67.77, 26.00) | 0.6425 | 0.6591 | 0.6082 | 1.056 | 1.084 |
| (41.46, 14.00) | 0.6457 | 0.6493 | 0.6303 | 1.025 | 1.030 |

合成形状（300px ROI）でも:

| 形状 | 旧予測 | 新予測 | 実測 | 旧/実測 | 新/実測 |
|---|---|---|---|---|---|
| 軸平行 正方リング 100×100 | 0.7071 | 0.7071 | 0.7059 | 1.002 | 1.002 |
| 45° ダイヤ（斜めのみ） | 0.7071 | 0.7071 | 0.5774 | **1.225** | **1.225** |

→ **斜め支配の +22.5% は 1px 点サンプリングでは変わらない。** 原因は正規化の定義ではなく、
`distanceTransform` が測るのが「Bresenham の階段」への距離で、理想線分への `|s·n|` から
ずれること。Chebyshev 間隔（`max(|dx|,|dy|)` 個 = ラスタライズ画素数と厳密一致、ダイヤで
n = |T| = 280 になる）も試したが**予測値は 0.7071 で不変**だったので、指示どおり
Euclid 約 1px 間隔にした。この乖離を消すにはラスタライズをモデル化する必要があり、
「単純化する」という今回の主旨に反するので手を付けていない。

`predicted_sharpness` の docstring には「数 % 〜 十数 % 楽観側、斜め支配で最大 +22%」と
実測値ベースで書いた。**削除した Liang-Barsky の特殊ケース（境界平行かつ外側の線分の
丸ごと棄却など）が消えたことが、この変更の実質的な利得。**

参考: 実 PCB では点数 `n` は template 画素数 `|T|` より 1.35〜1.42 倍多い（970 vs 684 等）。
隣接ポリゴンが共有する線分・重複頂点がラスタライズでは 1 画素に潰れるため。よって
「`|T|` と厳密に同じ定義」ではなく「`|T|` と同種の量」と書いてある。

## 落ちる既存テスト（41 件、すべて `tests/` 配下。修正は後続 agent）

`make format` pass / `pyright src` **0 errors** / `make test-e2e` **51 passed**。
`make type` は `tests/` の 8 error で赤（内容は下表と同じ原因）。
`grep -rn '</content>' src data` ヒットなし。

| ファイル | 件数 | 原因 | 直し方 |
|---|---|---|---|
| `tests/pcbasm/posctrl/test_region.py` | 18 | `plan_alignment_regions(proj, pad_centers, board_transform, ...)` の 3 引数呼び出し | 第 2 位置引数を削り `safe_area=` を渡す。`test_empty_pad_centers_raises` は**削除**し、代わりに「空 `safe_area` で空リスト」「ROI が収まらなければ空リスト」を書く |
| `tests/pcbasm/posctrl/test_aligner.py` | 10 | `AlignmentRegion(..., edge_length_px=...)` | `edge_point_count=<int>` |
| `tests/pcbasm/posctrl/test_alignment.py` | 8 | 4 件は `edge_length_px`、4 件は `pcb` fixture に `outline` が無く `Mock.buffer()` の戻りが Mock → `is_empty` が truthy → 領域 0 個（`assert 0 == 1` / `IndexError`） | fixture に `pcb.outline = Outline(_square(0, 0, 35))` を足す。**`half=35` は検証済みで anchor (0,0) / 予測 sharpness 0.707 の 1 領域が出る**。`half<=30` だと 0 個になる（`region_size_px=400` / `PPM=10` → ROI 40mm、格子 step 20mm が bbox 中心に乗るには span ∈ (60, 80] が必要） |
| `tests/webui/jobs/test_board_ops.py` | 5 | `edge_length_px` | `edge_point_count=<int>` |

新設定キーのテストは未追加: `PadAlign.board_edge_margin` の既定 2.0 / 負値 ValueError
（メッセージ `board_edge_marginは0以上である必要があります: {value}`）/ `config_store` の
`paste_dispenser.pad_align.board_edge_margin` FieldSpec（`float` / `mm` / 負値で 400）。

## 計画外の判断

- `PadAlign.__attrs_post_init__` の `min_sharpness` 単独チェックを
  `("board_edge_margin", "min_sharpness")` のループに畳んだ。**既存の例外メッセージは
  1 文字も変わらない**（`{name}は0以上である必要があります: {value}`）。
- `src/webui/jobs/posctrl.py` は `edge_length_px` を参照していなかったので無変更。
- `src/pcbasm/posctrl/README.md` の銅箔照合節を 1 項目 → 2 項目に更新（点サンプリングと
  `safe_area`）。
- `config/machine.toml`（gitignore・実機設定）は指示どおり未更新。`board_edge_margin` は
  既定 2.0 で動く。ユーザーが WebUI で調整可。
- リポジトリ直下に元から untracked のゴミファイル `"\0014\253\006@W@8"` がある。私の生成物では
  ないので消していない。

---

# 追補: `_candidate_grid` の丸め誤差不感化（spec-test-author 指摘）

## 症状（実測で再現）

`_candidate_grid` の `math.ceil((hi - lo) / step)` が丸め誤差に跳ぶ。`step` は
`region_mm / 2` で、`region_mm = region_size_px / hypot(matrix[0,0], matrix[1,0])`
なので `board_transform` に回転があると最終桁が振れる（`Rotation(30°)` で
`region_mm = 9.999999999999995`）。`span / step` がちょうど整数になる配置では、
その振れだけで分割数が 1 段増え、格子間隔が不連続に変わる。

## 修正

`ratio = (hi - lo) / step` を出し、`math.ceil(ratio - _GRID_RATIO_TOLERANCE * max(ratio, 1.0))`
で分割数を決める。`_GRID_RATIO_TOLERANCE = 1e-9`（相対）。倍精度の相対誤差
（~1e-16）より十分大きく、実寸の差より十分小さい。数式・他ロジックは無変更で、
変更は `_candidate_grid` と新定数のみ。

- 絶対許容ではなく相対にしたのは、`span / step` が大きい（分割数が多い）場合でも
  同じ強さで効かせるため。
- `round` ベースには**しなかった**。`ratio = 2.4` で `round` は 2 分割（間隔 1.2·step）
  を返し、「格子間隔は step 以下」という既存の性質を壊す。`ceil` + 相対許容なら
  整数近傍の跳びだけを吸収し、それ以外の分割数は 1 つも変わらない。

## 実測（read-only、`CopperProjector.board_to_pixel_affine` の実コードパス）

`safe_area` bbox span 40.0mm / `region_size_px=100` / `PPM=10`（公称 `region_mm` 10.0mm、
`step` 5.0mm、`span/step = 8`）。修正前は 15 角度中 7 角度で分割数 8 → 9 に跳び、
格子間隔 5.000000mm → 4.444444mm。修正後は全角度で 8 分割・5.000000mm。

| angle | region_mm | span/step | 修正前 n / 間隔 | 修正後 n / 間隔 |
|---|---|---|---|---|
| 0 | 10.0 | 8.0 | 8 / 5.000000 | 8 / 5.000000 |
| 5 | 9.999999999999996 | 8.000000000000004 | **9 / 4.444444** | 8 / 5.000000 |
| 10 | 10.000000000000005 | 7.999999999999996 | 8 / 5.000000 | 8 / 5.000000 |
| 15 | 10.000000000000002 | 7.999999999999998 | 8 / 5.000000 | 8 / 5.000000 |
| 20 | 10.000000000000002 | 7.999999999999998 | 8 / 5.000000 | 8 / 5.000000 |
| **30** | **9.999999999999995** | 8.000000000000004 | **9 / 4.444444** | 8 / 5.000000 |
| 33 | 10.0 | 8.0 | 8 / 5.000000 | 8 / 5.000000 |
| 37 | 10.000000000000014 | 7.9999999999999885 | 8 / 5.000000 | 8 / 5.000000 |
| 40 | 9.99999999999999 | 8.000000000000009 | **9 / 4.444444** | 8 / 5.000000 |
| 45 | 10.000000000000009 | 7.999999999999993 | 8 / 5.000000 | 8 / 5.000000 |
| 60 | 10.000000000000012 | 7.99999999999999 | 8 / 5.000000 | 8 / 5.000000 |
| 75 | 9.99999999999999 | 8.000000000000009 | **9 / 4.444444** | 8 / 5.000000 |
| 90 | 9.999999999999996 | 8.000000000000004 | **9 / 4.444444** | 8 / 5.000000 |
| 120 | 9.999999999999996 | 8.000000000000004 | **9 / 4.444444** | 8 / 5.000000 |
| 210 | 9.999999999999998 | 8.000000000000002 | **9 / 4.444444** | 8 / 5.000000 |

整数でない `span/step` では分割数が 1 つも変わらないことも確認（`span=40/step=3` → 14、
`span=37.5/step=5` → 8、`span=0` → 0 分割 = 1 点）。

## 挙動が変わる境界（意図的）

span が step の 1e-9 倍未満（例 span=1e-12mm, step=5mm）だと 1 分割 → 0 分割になり、
その軸の候補が 2 点から 1 点に減る。潰れた bbox の縮退ケースで、実寸では起こらない。

## 検証

`make format` / `make type` (0 errors) / `make test-no-hardware` **1680 passed** /
`make test-e2e` **51 passed**。`tests/` は未変更。実機テストは未実行。
