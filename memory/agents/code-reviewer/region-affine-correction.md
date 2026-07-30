# region-affine-correction レビュー

計画書: `memory/agents/implementation-planner/region-affine-correction.md`
対象: `feature/20260729/region-alignment-average` の未コミット差分（src 10 file / tests 9 file /
設定テンプレート 2 file / 未追跡の agent ノート 3 file）

## verdict: approve

数式・共役・適用位置・仕様準拠はすべて独立検算で通った。must-fix は無い。
should-fix 3 件はテストの空振り 2 件と README の未同期で、いずれも実装の振る舞いは正しい。

## must-fix

なし。

## should-fix

### S1. `test_tile_phase_is_aligned_to_the_pad_centroid` が空振り（確信度: 高）

- `tests/pcbasm/posctrl/test_region.py:166`
- `region.py:186` の `base = pad_px.mean(axis=0)` を `min(axis=0)` に変えても
  `tests/pcbasm/posctrl` 175 件が全通過する（実測）。pad 1 個では mean == min、
  `test_regions_are_ordered_from_the_tour_start` も pad 間隔が `2*region_mm` なので
  どちらの位相でも同じタイルが出る。
- 「位相 = pad 重心」は `_GRID_RATIO_TOLERANCE` 撤去と回転安定性の根拠になっている設計判断
  （計画書「設計判断」1）。pad を非対称に置く（例: x = 0, 10, 10 → 重心 6.67 / min 0）と
  anchor が変わるので 1 件で弁別できる。spec-test-author が自己申告した
  `test_single_pad_yields_only_the_tile_containing_it` と同種の穴。

### S2. `max_passes >= 3` の経路が未テスト（確信度: 高）

- `src/pcbasm/posctrl/aligner.py:136`
- `with_correction(Shift.from_point(cumulative))` を `Shift.from_point(increment)` に
  変えても 175 件が全通過する（実測）。3 パス目で「累積」ではなく「直前の増分」で投影を
  補正するのは二重計上の再発そのもので、本タスクの最重要不変条件が 3 パス目では
  無防備。
- `max_passes` は `PadAlign` / `config_store` とも「1 以上」しか検証しないので、
  WebUI から 3 以上が設定できる = config から到達可能な経路。既存の
  `test_converge_tolerance_decides_the_early_exit` は `max_passes=3` だが 2 パスで
  収束するため 3 パス目に入らない。

### S3. `posctrl/README.md` が実装と矛盾（確信度: 高）

- `src/pcbasm/posctrl/README.md:9` が「領域単位の 1 ショット計測」「基板全体の平均並進補正」。
  反復計測とアフィン補正に変わったので記述が逆。前ラウンドのレビュー S7（旧 `PadAligner` /
  `PadAlignmentSession` の残骸）も未着手のまま。
- 計画書は README 同期を `code-simplifier` の担当としているので、合流前に回せば足りる。

## nit

- **N1** `region.py:211` のフィルタ `math.sqrt(constraint / edge_point_count)` が
  `AlignmentRegion.predicted_sharpness`（`region.py:64`）と同じ式の重複。片方の正規化だけ
  変えると閾値が黙って別物になる（`TestPredictedSharpnessMatchesMeasured` は後者しか見ない）。確信度: 高
- **N2** `alignment.py:96` の `if count >= 2 else 0.0` は結果が変わらない分岐（count==1 では
  centered が零行列で σ_min = 0）。`count < 3` も spread 判定に包含される（2 点は必ず
  spread = 0）。起こり得ない場合の分岐（CLAUDE.md 原則 2）。確信度: 高／影響なし
- **N3** `board_ops._log_alignment` の縮退警告が n < 3（board_tour の `min_regions=1`）でも
  「アンカーの広がりが不足…region_size_px を小さくして」と出る。原因は区数なので案内がずれる。確信度: 中
- **N4** 計画不足メッセージが計画書案の「基板外形の内側 {margin} mm」ではなくキー名表記
  「（board_edge_margin）」。`measure_regions` が `pad_align` を持たないためで実用上は十分。確信度: 高／影響小
- **N5** `test_region_count_is_stable_under_board_rotation` の `degrees=0.0` は
  Identity vs Rotation(0) の自明比較。テスト名の `region_count` は撤去した設定キーと同名で紛らわしい。確信度: 高／影響小
- **N6** `residuals` が `residual_rms` / `residual_max` / ログループで 3〜4 回再計算される
  （区数は十数なので実害なし）。確信度: 高／影響なし

## 独立検算の結果（コード変更不要・実機評価の材料）

- **O1 二重計上なし。** 手描きフィクスチャを使わず「真の銅箔位置を投影して合成する カメラ」で
  1〜2 パスを通した（`scratchpad/verify_two_pass.py`, `passes.py`）。`with_correction` を潰した
  版は正確に 2 倍（真 0.6mm → 1.29mm）になり、実装版はならない。
- **O2 最小二乗は正しい。** `fit_displacement` の L / t を scipy `least_squares` の素の 6 変数
  最適化と突き合わせて max 差 2e-14。非対称 L（転置ミスなら 7e-4 ずれる）でも 8e-16 で復元。
  `Compose([Shift(-c), Matrix2d(I+L), Shift(c+t)])` の適用順も任意点で `p + L(p−c) + t` に一致。
- **O3 実基板で縮退しない。** TJ-56-67 / ppm 30.225 / margin 2.0 / 300px で 9 区・
  spread 6.26mm・最小中心間 9.93mm（= region_mm、重なりなし）・予測 sharpness 0.64〜0.71。
  θ = 0 / 0.5 / 2° で 9 区固定（400px は 6 / 6 / 5 区、spread 4.70〜6.67mm）。
- **O4 ただし 9 区のうち 6 区が同一行**（y = 99.58 に 6 / 109.51 に 2 / 119.43 に 1）。
  少数行の 3 区が全滅すると spread = 0 → 並進へ縮退（警告あり）。`min_regions=4` は
  「4 区すべてが 1 行」を許すので、縮退警告の読み取りが実機での要注意点。
  外れ値 1 区が少数行に居る場合、レバー腕がその 1 区に乗る（30um の誤マッチ → 遠方 pad で
  約 40um）。棄却器は入れない設計判断（計画書「想定リスク」）なので残差ログが唯一の検出手段。
- **O5 plan-implementer ノートのバイアス説明は原因の言い換えが必要。** 合成観測は 1px 格子へ
  丸められるためサブピクセルの補正移動が画に出ず、同じバイアスを 2 回測る。実機の観測は
  連続に動くので 2 パス目で相殺する（`offset₂ = ppm(cum − D) + b = −b + b = 0`）。
  ノートの「反復計測はパスごとにこのバイアスを足す」はアルゴリズムの性質のように読めるので、
  実機で「毎区 ※収束せず」が出たときの誤診の元になる。
- **O6 パス 2 の実効利得は step 量子化で下限が付く。** `observed_at` は
  `gcode_move.gcode_position`（指令位置）なので、2 パス目の微小移動が物理的に出ない分
  （半 step ≈ 2.5um 級）はそのまま増分に乗る。planner の理想利得 1.8 → 0.7um と同オーダー。
  `max_passes=1` の逃げ道が設計に入っているのは妥当。
- **O7 反復は固定点反復で `R∘Q ≈ I`（符号規約 A2/A4）に依存する。** `offset_transform` が
  90° 回転だと発散する（独立検算: 真 D=(0.6,−0.4) に対し −3D 相当）。実機は約 180°
  = involution なので `Q² ≈ I` で問題なく、むしろ 1 ショット版が持っていた共役誤差
  `2 sin δ |D|` を反復が消す。テストで大きさをピンしているのは Identity のみ。

## 確認した観点（問題なし）

- **変更禁止箇所**: `posctrl/correction.py` / `tests/pcbasm/posctrl/test_correction.py` /
  `copper.py` の `_fit_sharpness` `_parabolic_subpixel` / `XYPositionAdjustor` はいずれも無変更。
  `copper.py` の差分は `with_correction` 追加と `Compose` import だけ。
- **補正の適用位置**: `Compose([board_transform, correction, toolhead_offset, height_plane])`
  のまま機械座標へ出る 1 回だけ。board 座標のポリゴンへの共役適用は無い。吐出量は
  `polygon.area`（board 座標）で決まり `_draw_polyline` が総量を保つので、スケール成分
  −2435ppm でも総量は不変（経路長だけ 0.24% 変わる）。
- **公開 IF**: 計画書の IF 表と一致（`increment` は orchestrator 裁定由来の追加）。
  `region_count` / `corrected_projector` / `.spread` は src から消滅。`__all__` はアルファベット順。
- **テスト方針**: `mocker.Mock` を手書き stub（`_StubKlipper` / `_StubStage` / `_StubPcb`）へ
  置き換えており前より skill 準拠。3rd-party（cv2 / numpy）は実物。private への直接アサートなし。
- **自前 mutation 15 件**: 転置落ち・Compose 順序入替・spread 正規化落ち・共役 anchor 取り違え・
  累積の欠落・収束判定の対象取り違え・max_correction を増分判定・pad 包含の緩和・
  ROI 包含を中心だけに・逆行列の転置落ち・sharpness 閾値無効化・早期打ち切り撤去・
  with_correction の合成順逆転 の 13 件は検出。すり抜けは S1 / S2 の 2 件のみ。
- **単純化**: src 正味 +226 行（region.py は −6 行。貪欲選択・`_candidate_grid` ・
  `_GRID_RATIO_TOLERANCE` 撤去とタイル走査の追加が相殺）。増分は新しい推定器
  （alignment.py +108）と判定材料ログ（board_ops +39）で、いずれも要求由来。
  投機的な柔軟性・起こり得ないシナリオのエラーハンドリングは N2 のみ。

## 検証結果

- `make format`: pass（全 hook Passed、書き換えなし）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（1716 passed / 87 deselected）
- `make test-e2e`: pass（51 passed）
- `grep -rn '</content>' src tests data`: 0 件（memory ノートの散文中の言及のみ）
- 実機テスト（`make test` / `@mark_hardware`）は未実行（方針どおり）
