# region-affine-correction 仕上げ（簡素化 + ドキュメント同期）

対象: `feature/20260729/region-alignment-average` の未コミット差分
前段: `memory/agents/plan-implementer/region-affine-correction.md` /
`memory/agents/code-reviewer/region-affine-correction.md`（verdict: approve、must-fix なし）

公開 IF は無変更。振る舞いの変更も無い（ログ文言 1 箇所を除く）。

## ドキュメント同期

### `src/pcbasm/posctrl/README.md`（should-fix S3）

- 概要リスト: 「領域単位の 1 ショット計測」「基板全体の平均並進補正」→
  「領域単位の反復計測」「区ごとの変位から当てはめる基板全体のアフィン補正」。
  公開名も `fit_displacement` / `DisplacementFit` を追記
- 「銅箔照合の設計」を現状の 8 項目へ書き直した
    - 「推定するのは並進のみ」→「**1 区の照合で**推定するのは並進のみ。回転・
      スケール・スキューは区をまたいだ当てはめ側で拾う」（旧記述は板全体の
      モデルと読めて実装と矛盾していた）
    - 領域選定を「貪欲選択・互いに領域サイズ以上離す・数個」から
      「重なりなしのタイル格子・位相 = pad 重心・3 条件・上限なし」へ
    - 予測 sharpness の正規化根拠を独立した項に分離
    - 反復計測（`max_passes` / `converge_tolerance` / 二重計上を防ぐ投影補正）の項を追加
    - 平均並進 → アフィン最小二乗 `d(p) = L (p − c) + t`、準共線での並進縮退、
      残差ログの目的（3 点法の残りか非線形か）を追記
- README の他の記述（`observe() -> Transform` 契約、補正の適用位置）は現状どおりで
  古くなっていない。旧 `PadAligner` / `PadAlignmentSession` の残骸は既に無い

### docstring

- `aligner.py` `RegionAligner`: 反復の説明文を整形。ただし docformatter が
  段落を再フロー（日本語を幅 1 で数える）するため、改行位置は hook 出力に従う。
  内容の陳腐化は無かった
- `region.py` / `alignment.py` / `copper.py` のモジュール・クラス docstring は
  実装と一致していた（タイル格子・3 条件・アフィン当てはめ・`with_correction`）。
  変更なし

## 簡素化（code-reviewer の nit から採用）

- **N1 採用** `region.py`: `math.sqrt(constraint / edge_point_count)` の重複を
  private `_sharpness()` に集約。`AlignmentRegion.predicted_sharpness` と
  計画時のフィルタが同じ 1 箇所を参照するようにした（片方だけ正規化を変えて
  閾値が黙って別物になる事故を防ぐ）
- **N2 採用** `alignment.py` `fit_displacement`: `if count >= 2 else 0.0` と
  `count < 3 or` を削除。centered の最小特異値は 2 点以下で必ず 0 になるので
  どちらも結果が変わらない分岐だった（起こり得ない場合の分岐＝CLAUDE.md 原則 2）。
  `spread < _MIN_ANCHOR_SPREAD_MM` の 1 条件に集約し、docstring も追従
- **N3 採用** `board_ops._log_alignment`: 並進縮退の警告に区数を出し、
  n < 3（原因が区数）と n >= 3（原因が配置）で対処の案内を出し分けた。
  唯一の振る舞い変更（ログ文言）。既存テストの `"広がり"` / `"translation"` の
  アサートは維持されている
- **N5 採用** `test_region.py`: `test_region_count_is_stable_under_board_rotation`
  → `test_tile_count_is_stable_under_board_rotation`（`region_count` は撤去済みの
  設定キー名で紛らわしい）。`degrees=0.0` の parametrize は Identity と
  Rotation(0) の自明比較なので落とし、θ=2° の 1 件にした。**このため
  test-no-hardware は 1717 → 1716 passed**（テスト件数の減はこの 1 件のみ）

## 却下した nit

- **N4 却下** 計画不足メッセージのキー名表記（`board_edge_margin`）。
  `measure_regions` は `min_regions` しか受け取らず `pad_align` を持たないので、
  mm 値を出すには引数を増やすことになる。reviewer 自身も「実用上は十分」と
  評価しており、引数追加の複雑さに見合わない
- **N6 却下** `residuals` の再計算（`residual_rms` / `residual_max` /
  board_ops のループで 3〜4 回）。キャッシュするには `attrs.field(init=False)` を
  もう 1 本増やすことになり、区数十数件の再計算より複雑になる。
  reviewer も「実害なし」

## 触っていない箇所（制約どおり）

`posctrl/correction.py` / `tests/pcbasm/posctrl/test_correction.py` /
`copper.py` の `_fit_sharpness` `_parabolic_subpixel` / `XYPositionAdjustor` /
untracked のゴミファイル。厳しく作られた 3 件のテスト（`with_correction(Shift(cumulative))`
の二重計上、タイル位相 = pad 重心、`max_passes >= 3` の 3 パス目）は無変更。

## 検証

- `make format`: pass（docformatter が aligner.py の docstring を再フロー、以後は no-op）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: **1716 passed** / 87 deselected（N5 で -1）
- `make test-e2e`: 51 passed
- `grep -rn '</content>' src tests`: 0 件
- 実機テスト（`make test` / `@mark_hardware`）は未実行
