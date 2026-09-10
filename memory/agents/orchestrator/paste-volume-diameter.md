# 点塗布の直径ベース塗布量推定と、収集のパージ廃止

計画書: `/home/gop/.claude/plans/docs-image-based-dispense-calibration-m-fluffy-llama.md`
ブランチ: `feature/2026-09-10/paste-volume-diameter`
進め方: skill `solo-dev-cycle`（自分で実装）＋ 各 MR で `code-reviewer` に必ず通す（ユーザー指示）

## 実測で確定した前提（再実験不要）

6 session / 831 サンプル / 4,155 画像（既存 v2 データ）で検証済み。

- 採用パイプライン: pre−post を clip → 99 パーセンタイルが `min_contrast` 未満なら直径 0
  （blank ガード）→ Otsu 閾値（下限 `min_contrast/2`）→ 3×3 MORPH_OPEN → 最大連結成分
  → 面積等価直径 → view 中央値
  - blank 誤検出 0/18、検出失敗 0/831、session 内残差 0.122
- 多項式は切片 0 固定の 3 次 `V = a·d³ + b·d² + c·d`。純 3 次は 0.124 で不足
- **差分のさらなる活用は効かない**（実測）
  - 差分強度の積分 S のみ: 0.380 / 直径3次+S: 0.114（点ごと微改善だが総体積誤差は悪化）
  - 半値幅・強度重み付き面積・2次モーメント直径: 0.132〜0.139。いずれも Otsu 面積等価直径に及ばない
- 残差 0.080 の分解: 実吐出ばらつき 0.054（分散の45%）／塗布順ドリフト（分散の23%）／
  **view 間ばらつき＝画像測定ノイズは体積換算 1.2%**
  - → 画像測定は既に十分正確で、残差の主因はラベル構造（rotation_allocated は点ごと真値を持たない）

## 確定した方針（ユーザー確認済み）

1. 抽象化を作らない。Protocol も ABC も作らず具象クラス 1 つ（直径以外の手法が来たら置き換わる）
2. マルチセッション非対応。1 session で近似。LOSO 不要
3. schema v3 のみ対応。既存 v2 は使わない
4. `src/ml/` は触らない（`tests/ml/paste_volume/helpers.py` は schema 変更で更新が必要）
5. 校正ファイルは 1 校正 = 1 ファイル、Git 管理、WebUI で明示選択
6. 新規ジョブは校正生成の 1 つだけ。検証は収集ジョブの拡張
7. パージ廃止 → 塗布パス先頭でインタラクティブローディング。ローディング分は銅板外へ廃棄

## MR 構成

- MR1: 収集のパージ廃止と schema v3 ← いま
- MR2: 下ごしらえ（atomic 移設・reader）と校正コア
- MR3: 校正生成ジョブ
- MR4: 検証の統合

## 段階ログ

### MR1 / 段階 1（計画）

影響範囲の確認結果:
- `src/ml/` は purge を一切参照していない（grep 済み）。src 側の変更は不要
- `tests/ml/paste_volume/helpers.py:306,313,330` が v2 metadata を組むので更新が必要
- スコープ外: `paste_solder` の初回パージ（`pasting/initial_purge.py`）、`testboard` の purge pad。
  これらは通常の塗布ジョブとテスト基板生成の機能で、dataset 収集とは別物

### MR1 / 段階 2-3（テスト → コア実装）

コア層完了。`tests/pcbasm/pasting/dataset/` 258 passed。

計画外の判断:
- **`PasteDatasetLoading` を metadata に新設**。`LoadingTotals` は `web.api.jobs.pasting.common`
  にあり web 層なので pcbasm から import できない。metadata 側に DTO を置き web 側で変換する
- **`config.initial_purge_ul` も削除**。パージしないので装置設定のスナップショットとして意味を失う。
  `PasteDispenser.initial_purge_ul` 自体は通常の塗布ジョブが使うので残す
- **`validate_dataset_run` から `initial_purge_ul` 引数を削除**。検証対象が消えたため
- **`_GridGeometry.available` を削除**し `grid` に統合。除外領域が無く両者が常に一致するため
- **seed 依存テストの期待値を再確定**。パージ除外がなくなり capacity が 24→25 / 143→144 に変わり、
  `rng.sample` の結果も変わる。`test_blank_cells_are_not_dispense_targets` は
  「塗布点だけが可動域に入る」意図を保つため seed 4 → 10（cell=(6,3), blank=(12,12)）へ変更。
  期待値を緩めたのではなく、決定論的配置の再計算
- **schema ピンを v2 → v3 へ作り替え**（`data/testing/schemas/paste_dataset_metadata_v3.json`）。
  v2 ファイルは削除。`paste_dataset_metadata_v1.json` は「移行せず拒否する」テストが使うので残す
- `pending.json` は schema v1 のまま（root から `purge` が消え `loading` が入るが、
  pending 自身の版は metadata の版に追従しない設計なので据え置き）

### MR1 / WebUI・ドキュメント

- 収集ジョブ: 塗布パス先頭で `run_loading_loop` を回す。`accepts_commands=True` は既に立っていた。
  `loading_param="loading_amount"` を足して操作 UI を有効化し、専用テンプレートへ
  `partials/loading_controls.html` を include
- ローディング ParamSpec 5 つを追加（既定値は `common.py` の `LOADING_DEFAULT_*` が出典）。
  レイアウトに影響しないので router の `DatasetLayoutRequest` には入れず、
  `tests/.../test_paste_dataset.py` の `_NON_LAYOUT_PARAMS` へ登録
- `tests/ml/paste_volume/helpers.py` の `skip_if_no_real_sessions` を
  「`metadata.json` があるか」→「**現版 schema の session があるか**」に変更。
  旧版データが残っていると実データ経路のテストが「読めない」ことで落ちるため
- docformatter が日本語 docstring を 1 行へ潰した（memory の既知問題）。
  `finalize_pending` の docstring を 1 文 1 段落へ直した

計画外の判断:
- **要件書は収集節だけ更新**。運転時キャリブレーションの「パージ対象パッド」（`paste_solder` の
  初回パージ）と、テスト塗布基板の purge pad はスコープ外なので触らない
- ml-plan.md の schema v2 参照と「purge と全 samples の合計」検証条件も v3 へ同期

### MR1 / 段階 3 ゲート

`make format && make type && make test-no-hardware` green（4533 passed / 1 skipped）。
skip は `tests/ml/paste_volume/test_task.py` の実データ経路で、既存 session が全部 v2 のため想定どおり。

### MR1 / 段階 4（自己レビュー）

- **冗長な `ctx.progress(LOADING_STAGE, 45.0)` を削除**。`manager` は percent をそのまま代入
  （`_progress_percent = percent`）するので、直後の `run_loading_loop` 内 `ctx.progress(LOADING_STAGE)`
  （percent 省略 = None）に打ち消されていた。既存 `loading` ジョブと同じく percent なしで揃える
- 残した懸念（既存パターンと同じなので許容）: ローディング待ちの間 applicator が開いたままなので
  AirPump が入り続ける。既存 `loading` ジョブも同じ挙動で、ローディング中は運転者が装置の前に
  いる前提。パージ時代は待ち時間ゼロだったのが変わる点として実機確認で見てもらう
- `loading_amount` に `minimum` を付けない（吸引で負値を送るため）。既存 `loading` ジョブと同じ

### MR1 / レビュー対応

`code-reviewer` の verdict は request-changes。must-fix 3 / should-fix 5 / nit 4 のうち、
N4 のみ却下し他はすべて対応した。裁定の詳細は `memory/agents/code-reviewer/paste-volume-diameter.md`。

特筆すべき指摘（自分の自己レビューで拾えていなかったもの）:
- **M2**: 回転ローディングの ParamSpec 4 つがフォームには出るが UI へ届かず完全に死んでいた。
  `partials/loading_controls.html` の回転セクションが `loading_rotation_defaults` で gate されており、
  ParamSpec を足しただけでは効かない。レビュアーは実際にページをレンダリングして実証した。
  → **教訓: ParamSpec を足しただけで「UI に出る」と思い込まない。描画を実測する。**
  再発防止として UI 実描画のテストを追加した
- **M1**: 自分も「AirPump が入りっぱなし」に気づいていたが「既存 loading ジョブと同じだから許容」と
  判断していた。レビュアーは「直前の pre 撮影パスがヘッドを板の上に残す」ため既定の姿勢が
  誤りになると指摘。既存 loading ジョブは `position_x/y/z` で移動してからループへ入るので同じではない。
  → **教訓: 「既存と同じパターン」で流す前に、前提条件まで同じか確かめる。**
- **M3**: 自分が入れた skip ゲートの修正が、条件を先送りしただけで問題を解消していなかった。
  「現版が 1 つでもあるか」では v3 を 1 本収集した瞬間に旧版で `from_roots` が失敗する。
  → **教訓: 「検査を足したが、その検査が働くかを測っていない」型（前 MR で 4 回踏んだもの）の再来。
  今回は tmp に旧版混在を作って実証確認した。**

## MR2: 下ごしらえと校正コア

`atomic.py` 移設 / `dataset/reader.py` / `paste_volume/{detect,aggregate,model,calibration,estimator}.py`
＋ `data/testing/paste-volume/`（実素材 16 枚）＋ `data/testing/schemas/paste_volume_calibration_v1.json`。

### 実装が実測ベースラインを再現することの確認

実データ 1 session（164 sample）へ detect → aggregate → fit を通した結果:

- 検出失敗 0 / blank 誤検出 0/3 / 被覆域内で単調
- `|mean(e)|+std(e) = 0.0852`（計画時の手実測 0.087 と一致）
- 総体積 推定 23.401 vs 実測 23.402 µL

パイプラインの写し間違いが無いことをこれで確かめた。

### 計画外の判断

- **`strict_bool` を metadata の共有 converter へ追加**。`bool` は `int` のサブクラスなので
  hook を分けないと `strict_int` へ流れ、`monotonic_in_range: bool` が弾かれた。
  dataset schema には bool フィールドが無いので既存への影響は無い
- **`detection` / `model` の `kind` を DTO へ持たせない**。DTO の `Literal` にすると方式追加の
  たびに DTO が増えるので、document のキーとして持ち parse 時に検証して剥がす
- **`aggregate.py` を独立モジュールに**。計画では detect に含める想定だったが、
  「1 view の計測」と「view を畳む」は関心が別で、テストも独立に書ける
- **`is_positive_in_range()` を公開メソッド化**。自己レビューで `fit_cubic_through_origin` が
  `model._range_samples()` という private を外から呼んでいたのを解消
- 校正ファイルの `source` は単数（マルチセッション非対応の方針どおり）

### docformatter の日本語 docstring 破壊（再発）

3 箇所で「文の途中の改行が半角スペースになる」破壊が起きた。MR1 に続き 2 回目。
**日本語 docstring は最初から 1 文 1 段落で書く**（`memory/docformatter-japanese-docstrings.md`）。
