# 銅箔照合を「数領域の平均補正」へ作り直す — レビュー

対象: `feature/20260729/region-alignment-average`（未コミット、`git diff HEAD` + 未追跡
`src/pcbasm/posctrl/region.py` / `tests/pcbasm/posctrl/test_region.py`）。
正典: `memory/agents/implementation-planner/region-alignment-average.md`。

## verdict: approve

**must-fix なし。** 数式・座標系・適用順序は独立に検算して全て正しかった。公開 IF は計画書と
逐一一致し、計画外の逸脱は実装者ノートに申告された 6 件のみでいずれも妥当。
以下は should-fix 8 件・nit 12 件（うちテスト網羅の穴 2 件と平均の外れ値耐性 1 件は
マージ前に裁定する価値がある）。

## 独立検算した結果（すべて合格）

| 検証項目 | 方法 | 結果 |
|---|---|---|
| `_fit_sharpness` の閉形式 | 3x3 の 6 項基底で `np.linalg.lstsq` を組み、`a3/a4/a5` を乱数 5 ケースで突き合わせ | **9 桁一致**。正規方程式 `[[9,6,6],[6,6,4],[6,4,6]]` も自分で立て直して `a3 = -S0/3 + Sx2/2` を導出 |
| `hxy = a5`（`2*a3` でない） | `c = a3x²+a4y²+a5xy` の交差偏微分は `a5`。45° 一方向拘束（真の λmin=0）で実装が `sharpness=0.0` を返すことを確認 | **正しい**。`2*a5` / `0.5*a5` はいずれも誤り（後者は 0.5 を返して採択してしまう） |
| `λ_min/(2n)` の平方根 | `H = 2A`・`n ≈ ΣL` から `sqrt(λmin(A)/ΣL)`。正方リング 100x100 の幾何予測 0.70711 vs 実装 0.70588 | **一致**（0.2% 差）。計画書の 0.7059 / 0.5693 / 0.1758 / 0.1921 / 0.0576 を全て再現 |
| `_parabolic_subpixel` の `cstar` | 頂点値 `f(t*) = c0 + b·t*/2 = c0 - 0.25(cxm-cxp)dsx` を手計算で導出、2 軸分離モデルでも成立 | **正しい**。クランプは到達不能（S4 参照）なので `cstar` は常に真の頂点値 |
| region.py の予測 sharpness と実測の同一スケール | 実 PCB `TJ-56-67` の 4 領域を投影 → 同マスクを (+2,+3)px ずらして `match` | 予測 0.633/0.641/0.637/0.682 vs **実測 0.612/0.601/0.595/0.662**（予測が 5〜7% 楽観側）。両者「同じ量」と呼べる精度 |
| `board_to_pixel_affine` のベクトル化投影 | 回転 7°・`Scale.flip(y)` 込みで `pixel_of` と比較 | **最大差 1.5e-11 px** |
| 参照フレーム採点 = アンカーフレーム採点 | 各領域を anchor フレームで再採点し `constraint` を比較 | **相対差 3e-13**（回転・鏡映込み）。「アフィンを 1 回しか取らない」設計判断は正しい |
| フレーム端で ROI がクリップされた場合の `min_loc` オフセット | `roi=(5,5,105,105)` / window 20px（`sx0` が 0 にクランプ）で既知ずれ (3,-2) を照合 | **(3.0, -2.0) を正しく復元**。`(sx0-x0)+cx` の補正は正しい |
| ROI = フレーム全体（`result` が 1x1） | `centered_roi((400,300), 300)` で `match` | `None`（窓端ガード）。`RegionAlignmentSession.__init__` の検証で到達不能 |
| `to_machine_transform(observed_at=...)` | `M(p) = p + (obs - anchor) - R(d)` を代数展開。`adjusted_position` は存在せず `stage.get_position()` を渡している | **罠を踏んでいない**。二重計上なし |
| `Compose([board_transform, correction, toolhead_offset, height_plane])` | `Compose` は list 順適用（`transform.py:415`）。`correction` は `Shift.from_point(Point2d)` → z=0 なので高さを乱さない。`height_plane` の定義域は `pasting/height.py:47` のノズル機械 XY | **順序は正しい** |
| 初回パージの補正 | `applicator.deposit_at(initial_purge.pad.center)` → `_draw_polyline` → `self._transform`（= 上記 Compose）。board 座標で渡している | **1 回だけ適用**。二重適用・適用漏れなし |
| board_tour の巡回先 | `Compose([board_transform, alignment.machine_transform])`（`toolhead_offset` を掛けない） | **正しい**（カメラを合わせる巡回） |
| `min_sharpness=0.15` のマージン | 実 PCB 400px 領域の実測 0.595〜0.662 | 閾値の **4 倍**。計画書の「17% マージン」は 240x8 細長リングという合成最悪ケースの話で、実機リスクは低い |
| サブピクセル許容 `abs=0.15` | テストの 3 シフトの実測誤差 (0.087, 0.084) / (0.087, 0.084) / (0.039, 0.039) | 余裕 0.063px。ただし同一フィクスチャの ±1px 全域では最悪 0.127px（N10） |
| `correction.py` / `position.py` / `test_correction.py` | `git status` に出ていない | **1 行も変わっていない** |
| `XYPositionAdjustor` | `setup.py:274` / `pasting.py:1876` で使用中、`__all__` 維持 | **残っている** |
| 残骸 grep | `tolerance` / `roi_margin` / `min_roi` / `max_failures` / `mean_distance` / `roi_of` / `crop_size` | `pad_align` 由来はゼロ（board キャリブの `tolerance`・カメラの `crop_size`・チェッカーボードの `mean_distance_px` のみ＝別物） |
| 未使用 import | `uvx ruff check --select F401,F811,F841` | `pasting.py:18` の `Machine` 1 件のみ。base でも同じく検出されるので**元から dead**（N11） |
| `__all__` のアルファベット順 | 手検証（`RegionAligner` < `RegionAlignment` < `RegionAlignmentSession`） | 正しい |

検算スクリプトは
`/tmp/claude-1000/-home-gop-pcb-assembly/b495f7b4-e830-4365-8865-c0a78c6d65d3/scratchpad/rv_*.py`。

## must-fix

なし。

## should-fix

### S1. `_fit_sharpness` の交差項が全テストで未検証（縮退検出の穴）

対象: `src/pcbasm/posctrl/copper.py:239` / `tests/pcbasm/posctrl/test_copper.py`

問題: 既存テストのフィクスチャは正方リング・水平線・4 円のみで、いずれも `Sxy ≈ 0`
（実測: ring 0、hline 0、circles -68 で sharpness 影響 0.005）。したがって `hxy` の係数を
壊してもテストは全緑のまま通る。

どう壊れるか: `hxy = a5` を `a5 / 2` に変えると、45° 一方向エッジしか無い領域（真の
λmin = 0）の sharpness が **0.0 → 0.5** になり、既定 `min_sharpness=0.15` を突破して
**採択される**。開口問題の棄却そのものが無効化される方向。逆向き（`2*a5`）は安全側。
`hxy = 2*a3` のような取り違えも同様に検出されない。

根拠: `scratchpad/rv_mut.py`（各フィクスチャの `Sxy` と k=0.5/1/2 の sharpness）、
`scratchpad/rv_cross.py`（解析パッチ `A = L·n nᵀ`, n=(-1,1)/√2 で k=0.5 → 0.5000 = ACCEPT）。
なお正規化（`/(2n)`）と `-S0/3` 項の欠落は `min_sharpness=0.9 → reject` と
`weak.sharpness < 0.1` の 2 テストが捕まえる（両方 1.4 以上になる）ので、穴は交差項だけ。

対策案: 45° 一方向エッジ（`cv2.line` の対角線）のみの template で `match` が `None`、
かつ `min_sharpness=0.0` で `sharpness ≈ 0` になるテストを 1 本足す。
現在の対角線フィクスチャは窓端ガードで先に落ちるので、探索窓内に真の最小が来る配置
（線の端点を ROI 内に入れる／`search_window` を小さくする）が必要。

確信度: **高**（数値実証済み）

### S2. 「予測 sharpness と実測 sharpness は同じ量」をピンするテストがない

対象: `tests/pcbasm/posctrl/test_region.py:12-13`（module docstring がそう主張）、
`src/pcbasm/posctrl/region.py:33`（`AlignmentRegion.edge_length_px` の docstring）

問題: `region.py` は `sqrt(λmin(A)/ΣL)`、`copper.py` は `sqrt(λmin(H)/(2n))` を返す。
両者が一致するのは `H = 2A` かつ `n ≈ ΣL`（ラスタ画素数 ≈ Euclid 長）という
2 段の近似に依存する。どちらかの正規化を変えると、`board_tour` のログに出る
「予測 sharpness」と実測が静かに乖離するだけで、テストは緑のまま。

根拠: 実 PCB で予測 0.633〜0.682 に対し実測 0.595〜0.662（5〜7% 楽観側）。
斜め線が支配的な形状では `n ≈ ΣL/√2` になるので最大 √2 の系統差が出る。

対策案: `test_region.py` に 1 本、等方リングを投影 → 既知量ずらした観測で `match` →
`abs(measured - predicted) < 0.1` を確認するテストを置く（`copper` と `region` の
スケール契約のピン）。

確信度: **高**

### S3. 平均に外れ値耐性がない（1 領域の誤マッチが平均を最大 0.1mm 動かす）

対象: `src/pcbasm/posctrl/alignment.py:44-51`（`BoardAlignment.translation`）、
`src/webui/jobs/board_ops.py`（`measure_regions`）

問題: 各領域は `max_correction`（実機テンプレート 0.3mm）で個別に上限を持つが、
平均には外れ値除去も `spread` に対するガードも無い。`min_regions=3` で 1 領域が
「sharpness は 0.15 をわずかに超えたが誤マッチ」だった場合、平均は最大
**0.3 / 3 = 0.1mm** 偏る。これは本タスクが潰そうとしている誤差と同じ大きさ。

根拠: 算術（上限 `max_correction` / 成功領域数）。`spread` はログに出るだけで
何の判断にも使われていない（`board_ops.py` の最終 log 行と summary のみ）。

計画書との関係: 計画書は「spread が大きければ剛体フィットを検討する — 今回は入れない」
と剛体フィットを deferred しているが、**外れ値除去や spread のガードは検討されていない**。

対策案（いずれか）: (a) `spread` が閾値超過で警告 log を明示的に出す（現状は数値のみ）、
(b) 中央値を使う、(c) 平均から `spread` の N 倍離れた領域を落として再平均。
今回入れないなら、実機確認項目に「spread を見る」を明記して残す（上位計画には既にある）。

確信度: **高**（算術は確定。対策の必要性は実機の spread 実測次第なので裁定は orchestrator）

### S4. `_parabolic_subpixel` の ±0.5px クランプは到達不能な dead branch

対象: `src/pcbasm/posctrl/copper.py:257-258`

問題: `c0` は `cv2.minMaxLoc` が返す `result` 全域の最小値なので、`u = cxm - c0 >= 0`、
`v = cxp - c0 >= 0` が恒真。`dsx = (u - v) / (2(u + v))` で `|u - v| <= u + v` だから
**`|dsx| <= 0.5` が常に成立**し、`min(max(..., -0.5), 0.5)` は決して効かない。
`result` は float32、パッチは float64 へキャストするだけなので順序は保たれる。

影響: 実害はない（`cstar` は常に真の頂点値になるので docstring
「cstar はその位置での補間コスト」も結果的に正しい）。ただし計画書が 3 つのガードを
「意図的」として表にしている根拠のうち 1 つが成立しておらず、読み手を誤らせる。

対策案: クランプを外して `dsx = (cxm - cxp) / (2 * denx) if denx > 0 else 0.0` にし、
到達不能である理由（`c0` が全域最小だから）をコメントに残す。`code-simplifier` 案件。

確信度: **高**

### S5. `rms_distance_px` / `sharpness` が overlay に出ない（上位計画の目標が未達）

対象: `src/webui/jobs/posctrl.py`（`_pad_renderer` / `render_failed`）、
`src/pcbasm/posctrl/render.py:38`（`render_edge_match` は lines を受け取らない）

問題: 上位計画 §6 は「ラベルに `rms_distance_px` と `sharpness` を出せるようにする
（現在は `render_edge_match` に `EdgeMatch` が渡らず照合前の状態しか見えない）」と
書いているが、実装後も overlay に出る文字列は `["Region N/M", "FAILED"]` と
pad designator だけ。`RegionAligner` が流す `render_edge_match` フレームは照合**前**に
作られるので、そもそも `EdgeMatch` を持てない。

なお実装は計画書（`implementation-planner`）の擬似コードに逐語一致しており、
計画書側が上位計画の項目を落としている。指標自体は `ctx.log` に毎領域出る
（実機チューニングはログで足りる）ので影響は小さい。

対策案: 今回やらないなら計画書に「overlay 表示は入れない、ログで代替」と明記して
上位計画との差分を閉じる。やるなら `RegionAligner` が `match` 後に 2 枚目の
フレームを流す設計になる。

確信度: **高**（事実確認）

### S6. `mocker.Mock(spec=RegionAlignmentSession)` は skill `testing-strategy` の「モック対象は自前 HAL ABC のみ」に反する

対象: `tests/webui/jobs/test_board_ops.py:151`

問題: skill `testing-strategy`「モック（使用する場合のルール）」は
「**対象は自前 HAL ABC のみ**。3rd-party 表面と内部関数は対象外」と書いている。
`RegionAlignmentSession` は HAL ABC ではない自前の配線クラスなので、文言上は逸脱。
3rd-party 表面（OpenCV / Klipper RPC / `time.sleep`）のモックは**混入していない**
（grep 確認済み。`cv2` は全テストで実物を通している）。

裁定材料: 同 skill の参考文献にある GOOS 原則「自分が所有しているもののみ mock する」
には適合する。`spec-test-author` の判断（領域ごとの成功/失敗を実画像で作り分けると
`measure_regions` のループ契約から焦点がずれる）にも一定の理がある。
`session` の HAL 結合は `test_alignment.py` が実 projector / 実 Canny / `FakeCamera` で
別に押さえているので、カバレッジの穴は空いていない。

対策案（採るなら）: `Sequence[RegionAlignment | None]` を返す小さな stub クラスを
テストローカルに置く（`Mock` を使わない）か、skill 側に「自前の非 HAL 協調オブジェクトも
可」と例外を明記する。

確信度: **中**（規約の文言違反は確定。実質的な害はないので裁定余地あり）

### S7. `src/pcbasm/posctrl/README.md` が旧 API を記述したまま

対象: `src/pcbasm/posctrl/README.md:8-9`

```
- 部品単位の銅箔照合による自動位置合わせ（PadAligner、部品の pad 群の実銅箔を覆う ROI で照合）
- 位置合わせの配線と補正結果の lookup（PadAlignmentSession / ComponentAlignments / sorted_top_component_pads）
```

4 シンボルすべて削除済み。実装者ノートは「`code-simplifier` の担当」としているので
未着手は想定内だが、マージ前に同期が必要。

確信度: **高**

### S8. 小さい基板では既定値だけで必ず中止する

対象: `src/pcbasm/posctrl/region.py:191`（貪欲の最小分離 `>= region_mm`）、
`src/pcbasm/config.py`（`region_count=4` / `min_regions=3`）

問題: 貪欲選択の最小分離が `region_mm = region_size_px / ppm`（実機設定で 13.2mm）
なので、TOP pad の bbox が 13.2mm の 2 倍程度しかない基板では領域が 2 個以下しか
選べず、既定 `min_regions=3` で `measure_regions` が**必ず** `ValueError` を投げる。
候補格子は bbox にしか張らないので bbox 自体が小さいと逃げ場がない。

エラーメッセージは「region_size_px を小さくするか region_count を見直してください」と
正しい誘導をしているので致命的ではないが、既定値のまま動かない基板クラスが存在する。

根拠: `TJ-56-67`（89.5x58mm）では 4 領域・最小分離 18.39mm で成立。bbox が
30mm 角程度の基板だと 2〜3 個が上限。

対策案: 今回は据え置きでよい（MR 説明に「小基板では `region_size_px` を下げる」と
書く）。裁定は orchestrator。

確信度: **中**（境界サイズの実測はしていない。算術からの推定）

## nit

### N1. `sharpness` の単位表記 `[px]` は無次元量

`src/pcbasm/posctrl/copper.py:62`、`src/pcbasm/posctrl/region.py:31`、
`data/config-templates/kurousagi.paste/machine.toml`、
`src/webui/config_store.py`（`FieldSpec(..., "float", "px")` → 設定 UI に「px」と出る）。
「1px ずらしたときの RMS 距離の増分 [px]」は px/px = 無次元。実害はないが設定画面に
単位が出るので、`unit` を落とすか「px/px」にするのが正確。確信度: 高

### N2. `cstar` は理論上 `c0 > 0` でも負になり得る（`rms_distance_px` が 0 を返す）

`src/pcbasm/posctrl/copper.py:259`。`cstar = c0 - (u-v)²/(8(u+v)) - (u'-v')²/(8(u'+v'))`
なので、片側の隣接コストが `c0` と同値（tie, v=0）かつ他方が大きいと
`(u/8) > c0` になり `max(cstar, 0) = 0` → 真の残差があるのに rms 0。
手計算例: `c0=0.1, cxm=10.1, cxp=0.1` → `cstar = -1.15`。
実フィクスチャ（円 4 個の 0〜2.0px スイープ）では `cstar` は常に `c0` の 0.8〜1.0 倍で
正のままだったので、実機で出る配置ではない。確信度: **低**

### N3. 窓端ガードが sharpness 棄却より前で、診断情報が失われる

`src/pcbasm/posctrl/copper.py:349` と `src/pcbasm/posctrl/aligner.py:129-132`。
`min_loc` が窓端 → `None` → `RuntimeError("拘束不足または観測エッジなし")` になるため、
「基板が `search_window`(1.4mm) 以上ずれている」ケースが「拘束不足」として報告される。
`max_correction`(0.3mm) < `search_window` なので正常照合はここに来ず、どちらの経路でも
中止するのは同じだが、実機で基板を大きく置き間違えたときの原因究明が 1 段遠くなる。
計画書どおりの挙動。確信度: 高

### N4. `measure_regions` が progress / log に `region.index` を使う

`src/webui/jobs/board_ops.py`。`ctx.progress("銅箔照合", 100.0 * region.index / len(regions))`
と `f"領域 {region.index + 1}/{len(regions)}"` は「渡された `regions` が
`plan_alignment_regions` の出力そのままで index が 0..n-1」に暗黙依存する。
現在の呼び出し 2 箇所はどちらも全計画リストを渡すので問題ない。`enumerate` を使えば
依存が消える。確信度: 高

### N5. `_candidate_grid` の `count == 1` 分岐は冗長

`src/pcbasm/posctrl/region.py:109`。`count = max(1, ceil((hi-lo)/step) + 1)` が 1 になるのは
`hi == lo` のときだけで、その場合 `(lo + hi) / 2 == lo` なので `np.linspace(lo, hi, 1)`
（= `[lo]`）と同値。`code-simplifier` 案件。確信度: 高

### N6. `tests/webui/test_config_store.py:416` の docstring が削除済み `max_failures` を参照

`TestCameraCropFields` の docstring「max_failures と同様の per-key 検証を追加する」。
参照先キーが本 MR で消えたので stale。確信度: 高

### N7. 設定セクション名が「パッド位置合わせ」のまま

`src/webui/routers/common.py:144`。キーが `pad_align` のままなので計画書は意図的に
据え置きと決めているが、UI 上は領域単位照合の設定に「パッド位置合わせ」と出る。
確信度: 高（計画書の明示的判断なので指摘のみ）

### N8. `plan_alignment_regions` が `projector` と `board_transform` を二重に受け取る

`src/pcbasm/posctrl/region.py:114-123`。`projector` は内部に同じ `board_transform` を
持っているので、不整合な組を渡すと `anchor` だけが別変換で計算される。
`RegionAlignmentSession.plan_regions` は同一のものを渡しているので実害なし。
計画書のトレードオフ節で検討済みの設計。確信度: 高

### N9. `centered_roi` の中心が 0.5px ずれ得る

`src/pcbasm/posctrl/copper.py:37-39`。`(width - size)` が奇数のとき ROI 中心が画像中心から
0.5px ずれるが、`plan_alignment_regions` の採点は厳密な中心 ±half で切る。
既定値（1280x720 / 400px）では偶奇が揃うため発生しない。実装者ノートで既知。確信度: 高

### N10. サブピクセルテストの `abs=0.15` はシフト値依存で脆い

`tests/pcbasm/posctrl/test_copper.py:364-380`。parametrize の 3 シフトでの実測誤差は
最大 0.087px（余裕 0.063px）だが、**同一フィクスチャ**を ±1px の 0.1px 刻みで振ると
最悪 0.127px（余裕 0.023px、`(-0.8, -0.2)`）。シフト値を「もっと網羅的に」と
parametrize に足すと落ちる。docstring に「この 3 値は選んである」と書いておくと安全。
確信度: 高

### N11. `src/webui/jobs/pasting.py:18` の `Machine` 未使用 import

**元から dead**（`git show HEAD:` 版でも ruff F401 が同じ 1 件を検出）。
規約どおり指摘のみで削除は要求しない。実装者が消した `ResolvedInitialPurge` /
`transform_polygon` は本 MR で orphan になったものなので削除が正しい
（`transform_polygon` 自体は `visualization/height_render.py` で現役）。確信度: 高

### N12. 高さ計測は補正前 XY で probe する

`src/webui/jobs/pasting.py:839-843`（`board_to_machine=session.board_to_machine`、
補正なし）。`probe.min_radius = 0.3mm` と `max_correction = 0.3mm` が同値なので、
基板が上限までずれていると銅箔端近傍の probe 点が FR4 に落ち得る（銅箔厚 ~35um の
バイアス）。**変更前と同一挙動なので回帰ではない**。
一方 `height_plane` の定義域は機械 XY なので、補正後 XY で評価するのは正しく、
非対称そのものに問題はない（平面は機械 XY 上のフィットで、ノズルが行く先の
機械 XY での高さを返すのが求める挙動）。確信度: 高

## 検証結果

orchestrator 実行分（再実行せず）:
`make format` pass / `make type` 0 errors / `make test-no-hardware` 1655 passed /
`make test-e2e` 51 passed / `grep '</content>' src tests` ヒットなし。

本レビューで追加実行:
- `pytest tests/pcbasm/posctrl/ tests/webui/jobs/test_board_ops.py tests/pcbasm/vision/test_copper.py`
  → **136 passed**（0.99s）
- `uvx ruff check --select F401,F811,F841 src/pcbasm/posctrl/ src/webui/jobs/ src/pcbasm/config.py
  src/webui/config_store.py` → 1 件（`Machine`、元から dead）
- 独立数値検証 7 本（上表）

実機テスト（`make test` / `@mark_hardware`）は**実行していない**。
