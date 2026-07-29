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
