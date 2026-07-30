# region-local-correction レビュー

対象: `feature/20260729/region-alignment-average` の未コミット差分
（src 10 file / tests 6 file / 設定テンプレート 2 file / 未追跡ノート 2 file）。
前ラウンド: `memory/agents/code-reviewer/region-affine-correction.md`

## verdict: approve

数式・符号・適用位置・削除の完全性は独立検算で通った。must-fix は無い。
should-fix 4 件は「per-pad 配線が無テスト」を軸にした 1 つの塊で、
コードの振る舞い自体は正しい。

## must-fix

なし。

## should-fix

### S1. per-pad 配線が完全に無テスト（確信度: 高）

- `src/webui/jobs/pasting.py:851-898` / `src/webui/jobs/posctrl.py:506,536`
- 自前 mutation 17 件のうち、以下 4 件が `test-no-hardware` 全緑のまますり抜けた:
  - `pad_transform` の引数を `routed_pads[0].center` 固定にする（＝全 pad 同一補正）
  - `applicator.apply([pad.polygon])` から `transform=` を落とす（＝無補正）
  - 初回パージから `transform=` を落とす
  - `_corrected_entries` の `correction_for(pad.center)` を固定点にする
- 本タスクの成果（pad ごとに違う補正がノズルまで届く）を守るテストが 1 件も無い。
  `BoardAlignment.correction_for` 単体は 30〜38 件で厚く守られているのに、それを
  呼ぶ側は素通り。`_run_paste_solder` / `_run_board_tour` は実機依存で直接は
  テストできないため、S2 の切り出しとセットで解消するのが素直。

### S2. per-pad 変換合成が job 層に直書き（確信度: 高）

- `src/webui/jobs/pasting.py:851`（`pad_transform` クロージャ）、
  `src/webui/jobs/posctrl.py:536`（`Compose([board_transform, correction_for(...)])`）
- skill `webui-thin-wrapper` の点検リスト「幾何計算（座標変換…）が router/job に
  直書きされている」に該当。合成順（補正は toolhead_offset の前・height_plane は
  最後尾）はドメイン規則で、それを 2 つの job file が別々に持っている。
- 置き場は既にある: `PasteSession.board_to_machine`
  （`src/pcbasm/session.py:108`）が補正なし版の同じ合成を pcbasm 側で公開している。
- 前ラウンドも同じ場所に `Compose([...])` を直書きしていたので回帰ではないが、
  per-pad 化で「毎 pad 呼ばれる関数」に育ったぶん影響が大きくなっている。

### S3. 「借用補正」の距離が判定材料に出ない（確信度: 高／数値、中／対処の要否）

- `src/webui/jobs/board_ops.py:155-160`、`src/pcbasm/config.py:53`（`min_regions`）
- 実測（`data/TJ-56-67`、ppm 30.225、`board_edge_margin=2.0`、θ=0.5°、
  `min_sharpness=0.15`）: `region_size_px=100` で 28 区が計画され、**全区成功でも
  19/48 pad が自分の区を持たず**、中央値 3.8mm・最大 5.1mm 離れた区の補正を借りる
  （ROI 全体が `outline.buffer(-2.0)` に入る条件で外周付近のタイルが落ちるため）。
  実測の局所勾配 0.36mm / 3.3mm をそのまま当てると、3.8mm の借用は 0.4mm 級の
  誤差を持ち込み得る＝補正しないより悪い領域に入る。
- ログに出るのは成功区数・平均変位・ばらつきだけで、借用が起きたか / どれだけ
  離れたかは見えない。前ラウンドは残差 RMS が同じ役目（模型が足りているかの
  判定材料）を担っていたが、その後継が無い。
- `min_regions=4` も局所補正では意味が変わっている。アフィンでは「自由度 + 余裕」
  だったが、局所補正では 28 区中 24 区が失敗しても通る＝全 pad が遠方の 4 区から
  借りる状態を許す。絶対数ではなく被覆率（借用距離）が本来の門番。
- 参考: `board_edge_margin` を 1.0 に下げると同条件で 38 区・借用 7/48・最大 3.3mm。
  実機チューニングの効く軸なので、借用距離をログに出すだけでも判断材料になる。

### S4. `transform` の渡し忘れが黙って無補正になる（確信度: 高／影響は現状なし）

- `src/webui/jobs/pasting.py:868-871`、`src/pcbasm/pasting/applicator.py:288,391`
- `base_transform`（補正なし）を applicator の既定に据えているが、`_run_paste_solder`
  の経路でこの既定が使われる箇所は 1 つも無い（`_run_loading_loop` は `load` /
  `load_rotations` しか呼ばない）。実質デッドで、かつ「`transform=` を書き忘れると
  例外にならず無補正で塗ってしまう」フェイルセーフの逆向きの既定になっている。
  S1 の mutation 2 件がすり抜ける直接の理由でもある。

## applicator の IF 追加（`transform: Transform | None = None`）の評価

**妥当。** 代替案を検討したが、いずれも劣る:

- pad ごとに `make_applicator` → `with` で入り直す: `__enter__/__exit__` が
  AirPump ON/OFF + Stepper Enable/Disable なので、pad ごとにポンプを叩くことになる
- `set_transform()` の可変セッター: 呼び出し順に依存する状態が増え、
  「今どの補正が入っているか」が呼び出し側から見えなくなる
- board 座標のポリゴンを `B⁻¹·d` だけ平行移動して既定 transform のまま塗る:
  機械座標の結果は数学的に完全に等価だが、ユーザーが明示的に却下した
  「board 空間へ補正を持ち込む」構造そのもの

`_fill` を既定値なしのキーワードにして呼び出し側に明示させている点、
`draw_line`（キャリブ専用）には足していない点は、いずれも CLAUDE.md 原則 2 に沿う。
既存呼び出し（吐出量キャリブ `pasting.py:1856` の `deposit_at`）は無変更で通る。

## nit

- **N1** 「最近傍の区中心 = 包含区」は board→pixel が**相似写像のときだけ厳密**。
  3 点法の `board_transform` は一般 2x2（`board.py:137` の `M @ B⁻¹`）でスキューを
  持ち得るので、数学的には成り立たない。実測（区中心の格子に 20 万点サンプル）で
  スキュー 0.06° なら境界から 1.4um、0.2° で 5.0um、1° でも 27.9um の帯だけが
  誤った区を引く。異方スケールだけなら 0 件（矩形格子の Voronoi は矩形のまま）。
  実害は無いが、docstring / README / テストが「一致する」と断定しており、
  テストは Identity / Shift / Rotation（全部相似）しか使っていないので無防備。確信度: 高
- **N2** `tests/pcbasm/pasting/test_applicator.py:340-341` の
  「board 座標のポリゴンへ共役適用する構造に戻ると、面積が変わって吐出量が動く」は誤り。
  純並進の共役は board 空間でも純並進なので面積も経路点数も変わらず、
  そのミューテーションはこのテストでは落ちない（実装が共役適用でないことは
  別途 diff で確認済み）。テストの価値は per-pad 差し替えのピンであって、
  共役構造の検出ではない。確信度: 高
- **N3** `src/webui/jobs/board_ops.py:150` の成功数不足メッセージ
  「region_size_px を大きくし」が、テンプレートの新コメント
  「広いと chamfer が局所変動を平均して鈍る」と方向が衝突する。確信度: 高／影響小
- **N4** `board_tour` は pad ごとに `correction_for` を 2 回引く
  （`_corrected_entries:536` と巡回ループ `posctrl.py:506`）。実害なし。確信度: 高
- **N5** 同じく巡回ループ内で pad ごとに `CopperProjector` を生成する
  （`posctrl.py:506`）。`__init__` は属性代入だけなので実測上の負荷は無い。確信度: 高
- **N6** 明示指定の初回パージ pad は disabled でもよい（`initial_purge.py:66`）ため、
  その場合 `plan_regions` に渡らず必ず借用補正になる。パージは捨て塗布なので実害なし。確信度: 高
- **N7** `displacement_spread` が軸ごとの σ を `Point2d` で返す（点ではない）。
  既存の 2 ベクトル型の流用で許容範囲。確信度: 高／影響なし
- **N8** `mean_displacement` / `displacement_spread` はログ 1 行と summary で計 4 回
  再計算される（区数は数十なので実害なし）。確信度: 高／影響なし

## 独立検算・実機で見るべき材料

- **mutation 17 件中 13 件を検出。** 検出できたもの: 平均への退化(38) /
  逆二乗補間(30) / 距離を anchor で測る(33) / min→max(37) / 符号反転(39) /
  母標準偏差→標本標準偏差(1) / `board_center` を区中心からずらす(12) /
  3 パス目に増分で投影補正(1) / 投影補正の撤去＝二重計上(11) /
  `_draw_polyline` の上書き無視(3) / `apply` が transform を捨てる(2) /
  ログの平均を先頭区に(1)。すり抜け 4 件は S1。
- **前ラウンドの S2（3 パス目が無防備）は解消済み。**
  `test_third_pass_corrects_the_projection_by_the_cumulative_not_the_increment`
  が `Shift.from_point(increment)` への改変を捕まえる。
- **補正の適用位置は要求どおり。** `Compose([board_transform, correction_for(center),
  toolhead_offset, height_plane])` で機械座標へ出る 1 回だけ。board 座標の
  ポリゴンへの共役適用は無い。吐出量は `polygon.area`（board 座標,
  `applicator.py:442`）、フィル経路生成も board 座標の polygon から。
  補正は純 `Shift` なので経路長も変わらない。
- **隣接 pad 間の不連続は無い。** `FillSequence.to_gcode` が pad（成分）ごとに
  「先頭点上空へ travel → 下降 → 吐出 → retract → lift」を 1 本に閉じるので、
  補正が pad 間で跳んでも連続パスが分断される箇所が無い。
  `toolhead_offset` は純 `Shift`（`config.py:254`）なので補正との合成順は可換で、
  順序が効くのは `height_plane` に対してだけ（補正が前で正しい）。
- **変更禁止箇所は無変更。** `posctrl/correction.py` / `tests/pcbasm/posctrl/test_correction.py`
  / `copper.py`（`_fit_sharpness` / `_parabolic_subpixel` 含め diff 無し）/
  `posctrl/position.py`（`XYPositionAdjustor`）はいずれも `git status` に出ない。
  `posctrl/orthogonality.py` も無傷で、`_run_orthogonality_test`（`posctrl.py:592`）が
  引き続き使う。
- **アフィンの残骸ゼロ。** `fit_displacement` / `DisplacementFit` / `DisplacementModel` /
  `_MIN_ANCHOR_SPREAD_MM` / `residual*` / `anchor_spread` / `.model` / `.translation` /
  `BoardAlignment.machine_transform` は src・tests・data・docs から消えている
  （`copper.py` の `machine_transform` 引数は `with_correction` のもので別物）。
  README・設定テンプレート 2 本・`config_store` の FieldSpec もすべて整合。
- **単純化。** src 正味 −105 行（`alignment.py` −97、`board_ops.py` −30 が主）。
  投機的な柔軟性・起こり得ないシナリオのエラーハンドリングは見当たらない。
- **実機で見てほしい点（cycle time）**: `region_size_px=100` は区数を大きく増やす。
  TJ-56-67（89.5x58mm・48 pad）の実測で 100px→28 区 / 200px→11 区 / 300px→9 区。
  `max_passes=5` と合わせると最悪 28x5=140 回の移動+撮像+照合になり、
  前設定（9 区 x 2 パス = 18 回）の約 8 倍。既定を決めた実測は
  GENS_Power_Section_5（47.5x20mm）なので、大きい基板では時間を確認してほしい。
- **`</content>` 混入**: `src` / `tests` / `data` に 0 件（memory ノートの散文中の
  言及のみ）。未追跡ノート 2 本の末尾も正常。

## 検証結果

- `make format`: pass（全 hook Passed、書き換えなし）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（**1754 passed** / 87 deselected）
- `make test-e2e`: pass（51 passed / 1790 deselected）
- 実機テスト（`make test` / `@mark_hardware`）は未実行（方針どおり）
- mutation 検証の一時編集は全て復元済み（`src` の md5 一致・`git diff --stat` 不変）
