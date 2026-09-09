# paste_volume train / evaluate（Phase 2 + Phase 3）

## 実データの実測（2026-09-09 時点、orchestrator が測定）

`data/paste-volume-datasets/` に session 5 本。**`.gitignore` が `*` なので git 管理外＝CI に無い。**

| session directory | samples | blanks | k = measured/commanded | rotations_per_ul |
| --- | ---: | ---: | ---: | ---: |
| plate-47.5x20-20260908T144137.001+0900 | 160 | 4 | 0.870058168195 | 14.970664 |
| plate-47.5x20-20260908T153829.606+0900 | 164 | 3 | 1.035754369088 | 14.970664 |
| plate-47.5x20-20260909T120737.733+0900 | 164 | 3 | 0.862594195928 | 13.346488 |
| plate-47.5x20-20260909T130756.484+0900 | 164 | 3 | 0.648548981882 | 13.346488 |
| plate-47.5x20-20260909T145923.452+0900 | 164 | 3 | 0.713484046817 | 15.765539 |

- 計 832 cell（816 sample + 16 blank）、PNG 8320 枚。**全て 53x53 RGB 8bit**
- `crop_size_px=53`、`camera.pixel_per_mm=28.677782176153425` は 5 session すべて同一
- 各 sample は 5 view（offset 0 / +x / +y / -x / -y、各 1.0 mm）
- **k は session 内で厳密に 1 定数**（float の ULP 差だけ）。`measured = commanded x k`
- k の広がりは **1.60 倍**（0.6485 〜 1.0358）。2 session 時代（0.8701 / 1.0358、1.19 倍）より大きい
- 体積域は session により 0.032 〜 0.363 uL
- paste / nozzle は全 session 同一（S3X70-E150DN、density 3.78 mg/uL、nozzle 0.3 mm）

## ユーザーが確定させた決定（2026-09-09）

- **評価は session 単位の leave-one-session-out 5-fold**。cell 単位 70/15/15 は棄却。
    理由: k が session 固有定数なので、cell 単位では model が session を識別して k を
    憶えるだけで見かけの精度が出る
- **スコープは Phase 2 + Phase 3 全部**（model / task / train / evaluate / conf /
    packaged config group / Optuna search space / MLflow）

## orchestrator の測定と裁定

- **`_cell_key` は座標由来なので session をまたいで同一値になる。** 5 session とも同じ
    47.5x20 銅板・同じ cell 配置なので、cell group は 166 個・各 5 sample（全 session 由来）。
    どの split にも 5 session 全部が入る。**cell group と session は入れ子ではなく直交する**
- したがって session LOSO では **split group を session そのもの**に切り替える。
    `LeaveOneGroupOutPlan.build({session_group: session_value})` は各 group が 1 値なので
    held-out 1 / remaining 4、`validation_ratio=0.15` で val 1 / train 3 になる
- **順序制約: `AugmentationRange.parameters_for` の材料へ役割ラベルを足すのは Trainer を
    初めて回す前**。`{global_seed}:{epoch}:{sample_id}` がラベル無しで `_derived_seed` を
    占有している。checkpoint を 1 つでも作ると augmentation 系列の変更が過去 run との
    比較を壊す

## 環境

- GPU workstation、コンテナ常駐。RTX 4090 / torch 2.12.1+cu130
- mlflow / optuna / onnx / onnxruntime / torchvision すべて導入済み
- 検証は `make ml-docker-check`。**`make test` / `make run` / `pytest -m hardware` は実行しない**

## orchestrator の裁定（実装の進め方）

- **計画書の「ユーザーへの確認事項」3 件は orchestrator 裁定で決着させた。**
    1. `ml.paste_volume.release` は作らない。Phase 4 相当であり、ユーザーが選んだスコープ
        （Phase 2 + Phase 3）の外
    2. 成功条件は必須 1〜3（機能・健全性・定数予測超え）、4（輝度差ベースライン超え）は目標。
        未達なら report に考察を残して MR は成立させる
    3. 5 fold は shell の for ループ
- **src を触る実装 agent は並列にしない。** ML コンテナは main repo だけを mount している
    （`docker/ml/compose.yaml` の `../../:/workspace`）ので、worktree に分離した agent は
    `make ml-docker-check` を実行できない。同じ作業ツリーで 2 体に書かせると片方の検証と
    commit にもう片方の未検証の変更が混入する。計画書の A/B 2 レーンはこの制約で採らない。
    並列は測定（ベースライン再測定）とレビューに寄せる

## ベースライン再測定の結果（step 7、2026-09-09 実測）

特徴量はいずれも 5 view を gray 化して平均したもの。`abs_diff_mean = mean(|post - pre|)`。

- `const` = train 平均の定数予測
- `lin1` = `brightness_diff`（= `mean(post) - mean(pre)`）1 変数の線形回帰
- `lin3` = `brightness_diff` + `abs_diff_mean` + `area_abs_12` の線形回帰

### A: session LOSO 5-fold（本命、blank 込み 832 cell）

| model | 5-fold mean R^2 (sd) | 5-fold mean MAE [uL] (sd) |
| --- | ---: | ---: |
| const | -0.205501 (0.193371) | 0.069755 (0.011138) |
| lin1 | +0.522382 (0.067169) | 0.043280 (0.006654) |
| lin3 | +0.696693 (0.148318) | 0.033966 (0.010461) |

fold ごとの振れが大きい（`const` の R^2 は -0.014 〜 -0.528、`lin3` は +0.493 〜 +0.890）。
**5 fold 平均だけで判断せず fold ごとの値も report に残すこと。**

### B: cell 単位 70/15/15（比較用、seed 0-4 平均）

| model | R^2 | MAE [uL] |
| --- | ---: | ---: |
| const | -0.008094 | 0.068198 |
| lin1 | +0.653748 | 0.039875 |
| lin3 | +0.797963 | 0.029065 |

**どの seed でも test に 5 session 全部が入る。** cell group と session が直交する実測証拠。

### leak の直接測定（session one-hot を足したときの利得）

| split | lin3 | lin3 + session one-hot | 差 |
| --- | ---: | ---: | ---: |
| A (LOSO) | R^2 +0.696693 | +0.560434 | **-0.136260**（純粋に害） |
| B (cell) | R^2 +0.797963 | +0.878698 | **+0.080735**（利得） |

**同じ「session を憶える」能力が cell split では R^2 +0.081 の利得になり、LOSO では害になる。**
画像から session を読める CNN は cell split でこの +0.08 を取りに行ける。
**ユーザーが LOSO を選んだ判断が正しかったことの実測証拠。**

held-out session 上で affine を後付け fit する oracle（k が完璧に読めた場合の天井）:
R^2 +0.866593 / MAE 0.021856 uL。**LOSO 残差の大半（R^2 0.697 → 0.867）は session ごとの k。**

### step 8 の判定基準（これだけを使う）

- **必須（成功条件 3）**: 5 fold 平均 test MAE < **0.069755 uL**（定数予測超え）
- **目標（成功条件 4）**: 5 fold 平均 test MAE < **0.043280 uL** かつ R^2 > **+0.522382**（lin1 超え）
- 参考の上位: lin3 が MAE **0.033966** / R^2 **+0.696693**。素朴回帰がかなり強い
- 参考の天井: oracle が MAE **0.021856** / R^2 **+0.866593**

**2 session 時代の R^2 0.59〜0.62 / MAE 0.039〜0.049 は比較に使わない。** test 集合の分布が
別物（cell split → LOSO）、k の広がりが 1.19 倍 → 1.60 倍、R^2 の分母（test 分散）も違う。

### 副産物

**最も強い単一の素朴特徴は輝度差ではなく `abs_diff_mean`**（目的変数との相関 +0.890、
輝度差は -0.786）。ペーストは基板より暗いので「暗くなった画素率」だけが効き、
「明るくなった画素率」は無相関（-0.029）。

## step 0/1 レビュー（1 巡目）の裁定

verdict は request-changes。詳細は `memory/agents/code-reviewer/paste-volume-train-evaluate-step01.md`。

### must-fix として差し戻す（6 件）

- **M1 view-dropout の材料検査が退化。** `GLOBAL_SEED=7, EPOCH=3` の材料は `randint(1,5)` が
    5 を引き、`sorted(sample(range(5), 5))` は乱数列に関係なく常に `(0,1,2,3,4)`。任意材料の
    21% が同じ値を返す。**ラベルを衝突させても全緑。** `count < AVAILABLE_VIEW_COUNT` になる
    定数へ変え、count 自体も観測点に含める。**このリポジトリで 6 回目の同じ型**
- **M2 閾値 28 の根拠が誤り。** 実測は 60 epoch で **31**（32 ではない。欠けるのは
    `(band 5, offset 3)`）。120 epoch 以降で 32。衝突時は epoch 数に関わらず 20。
    **120 epoch へ上げて `== 32` の完全一致で固定する**（`>=` より強く、計画書 R1 の
    「閾値を下げず epoch を増やす」に沿う）
- **M3 session 次元で既存 split.json を再利用すると held_out の食い違いを黙って通す。**
    session-0 の split.json がある状態で `held_out_session="session-2"` を要求すると
    `reason=None` で成功し、要求した session-2 が train に入る。`SplitManifest` は次元も
    held-out も持たないので誰も気づけない。step 5 の `run_directory` 既定が fold 間で
    共有され得るので実害がある
- **M4 private の直接 import（`_derived_seed` / `_placement_seed`）。**
    `memory/feedback_no_private_test.md` の明示禁止。**加えて private を import すると
    f(x) と f(x) の比較になり検査が同語反復になる。** 契約を test 内で独立に組み直すか、
    公開経路（`collate` の `valid_pixel_mask`）で観測するほうが**強い**。M1 の直しと同じ方向
- **S1 を must-fix へ昇格。** 「5 番目の用途をラベル無しで足す」変異が全緑。4 用途の手書き表
    なので新規用途は原理的に見えない。**step 0 の目的は規則を立てることであって、
    機械検証されない規則は必ず退化する。** `_derived_seed` / `random.Random(...)` の呼び出し
    元を走査し、材料文字列が役割ラベルを含むことを機械検証する（`test_architecture.py` の手口）
- **S8 docformatter が日本語を崩した 5 箇所。** auto-memory `docformatter-corrupts-japanese`
    に該当。文中に空白が入っている（「前頭辞を 渡して」等）

### should-fix として対応（S2 / S3 / S5 / S6 / S7 / S9 / N2 / N3）

- S2 計画外の判断 3 件が無検査（削除しても全緑）
- **S3 合成 5 session の fold 構成テストを足す。** (1,1,3) を固定しているのは opt-in の
    実データテストだけで **CI では skip される**。n=3〜12 は val 1 固定、n=13 で初めて val 2
- S5 `groups` の二重計算、S6 docstring、S7 契約外 dimension が `validate()` を通る、
    S9 理由文未検証の異常系 2 件
- **N2 / N3 は docstring の内容が事実として誤り**（`batch-plan` の材料に `sample_id` は入らず、
    `view-dropout` は batch 単位）。誤った docstring は無いより悪いので直す

### nit は code-simplifier へ回す

N1 / N4 / N5 / N6 / N7。

### 副産物（step 4 へ伝達）

**`make_strict_converter()` は PEP 695 の `type SplitDimension = Literal[...]` を解決でき、
`"machine"` を `ClassValidationError` で弾く。** step 0/1 実装者の残課題 1 は解消。

## step 2/3 の実測と、step 8 への最重要の申し送り

### 確定した実測

- parameter **395,048 ちょうど**（仕様書・planner の手計算と一致）、tensor 43 本、
    `total_stride == 8`、GMAC 5 view で 0.159780（53px）/ 1.307149（159px）
- **trunk ReLU の死は 0/100。`hidden_features=128` の再検討は不要**（仕様書 §2 申し送り 2 完了）
- `apply_fine_tune_freeze` は 21 tensor / 86,368 要素を凍結、学習対象 308,680 要素。
    freeze 後も `padding_pixel.requires_grad` は True
- 過学習テスト: 初期 MAE 0.067857 → 最終 0.002133
- compile parity（inductor / fullgraph / padding あり 2 sample）: passed、**勾配 43 本すべて**突き合わせ

### GroupNorm と大域統計（2 巡目レビューで訂正済み）

**この節は 1 度誤った内容で記録した。以下が実測に基づく訂正版。**

step 2/3 実装者の当初の申し送りは「encoder 冒頭の GroupNorm が入力全体の明るさ・振幅の差を
ほぼ落とすので、step 7 の最強の素朴特徴 `abs_diff_mean` / `brightness_diff` が乗る経路が
塞がっている。成功条件 3 を満たさなければ正規化が第 1 容疑者」だった。orchestrator も
その形で裁定を記録した。**2 巡目レビューが実測で反証した。**

`PasteVolumeTask` + 200 step + cosine、8 sample、最終 MAE / 初期 MAE:

| 合成入力 | group 数 8 | group 数 1 |
| --- | ---: | ---: |
| 面積（空間構造） | 0.0314 | - |
| **6 channel 共通の振幅** ∝ 真値 | **0.8421**（定数予測の床、5 seed とも） | **0.8421** |
| **post 3 channel だけ**の輝度 ∝ 真値 | **0.0210** | 0.0180 |

1. 「6 channel 共通の振幅だけの差は学習できない」という実測自体は正しい
2. **`group_norm_groups=1` にしても直らない（0.8421 のまま）。打ち手 (c)「group 数を 1 に」は
    この現象に対して測定上まったく効かない**
3. **pre/post の contrast は GroupNorm を生き残る**（post だけ明るくした batch は 0.0210 で学習）。
    **step 7 の `brightness_diff = mean(post) - mean(pre)` と `abs_diff_mean = mean(|post - pre|)` は
    どちらも 6 channel 共通の大域 scale ではなく pre 対 post の contrast。この 2 特徴が乗る
    経路は塞がっていない**

**したがって GroupNorm は step 8 の第 1 容疑者ではない。** 面積経路（`area_neg_12` 単独で
LOSO R^2 +0.640）も contrast 経路も生きている。成功条件 3 を満たさなかった場合の原因帰属を
正規化へ短絡させないこと。残る打ち手候補は (a) conditioning に輝度差を足す /
(b) 第 1 stem の GroupNorm を外す。

### 計算量 gate をどの shape で測るか（仕様書 §2 の自己矛盾）

実測 GMAC: 53x53x1view 0.031966 / 53x53x5view 0.159780 / 159x159x5view 1.307149 /
**512x512x1view 2.677027** / 512x512x5view 13.385085。

仕様書 §2 は「512 x 512 入力で 1.5 GMAC 以下であることを実装時に計測する」と
「159 px x 5 view が計算量上限に最も近い構成」（1.31 GMAC）を**両方**書いていて自己矛盾している。
512 で測ると全 run が学習前に拒否され、159px で測ると gate は原理的に発火しない。

**裁定: gate は点塗布 crop の上限（159px x 5 view）で測る。** 512 の記述は多視点化（!212）
以前の単一 view 時代の残りで、`ImageConstraints` の `maximum_size=512` は前処理の契約上限で
あって点塗布 crop の実際の上限（159px）ではない。**model を縮めない**（仕様書 §2 の
「精度比較なしに channel や block を増やさない」の裏返しで、減らすのも比較なしには行わない）。
**仕様書 §2 の該当記述を実測値へ書き替える。**

### 運用上の落とし穴（新規、以降の全レーンへ）

- **`pre-commit run -a` は untracked file を検査しない**（`git ls-files` を見る）。
    新規 file は `git add` してから format を回すこと。step 2 でこれを踏んだ
- **codespell が `memory/agents/**` の綴りも拾って `make ml-docker-check` を落とす**

### 計画外の判断（レビュー対象）

- 過学習テストの optimizer に cosine 減衰を足した。固定学習率では 200 step で収束せず
    5 seed 中 2 seed が 1/10 に届かない（0.022〜0.624）。実 Trainer は `ReduceLROnPlateau` で
    同じ問題を扱っている
- `ZeroTargetMetrics` に `zero_target_` 接頭辞。4 field 名が `GaussianRegressionMetrics` と
    衝突し、blank 16 件の値が全体を黙って上書きするため。`reduce` は 26 本を返す
- `build_paste_volume_model` は `initial_weights` の load も freeze もしない（step 5 の
    `run_training` が build → load → freeze の順を持つ）

## step 4（packaged config）の裁定

### 計画書からの逸脱 4 件 — すべて承認

1. **`data/` と `model/` の group directory を作らない。** `manifest` を落とした後に書ける内容が
    「既定値の再掲」か「機械固有の絶対パス」しか残らない。AGENTS.md 開発原則 2・3（要求されて
    いない柔軟性を足さない）に沿う。空 option を置かない規約をテストで固定したのも良い。
    argv 規約（`group=option` か `key=value` かは name が conf root 直下の directory として
    実在するかで決める）とも矛盾しない — `data` が directory でなければ `data.roots=...` は
    `key=value` として正しく解決される
2. **`PasteVolumeDataConfig.split_dimension` を既定値なしにする。** step 1 の
    `sample_groups(dimension=...)` に既定を持たせなかったのと**同じ理由**（既定を置くと
    呼び出し側が黙って leak する split を選ぶ）。結果として `experiment=` が実質必須になるが、
    仕様書 §7 の argv 例は常に `experiment=` を渡しているので整合する
3. `SearchConfig` を `experiment.py` に置く（`search.py` だと循環 import）
4. `data.roots` / `logger.tracking_uri` / `hyperparameter_search.storage_uri` を同梱 TOML に
    書かず必須 field にする（機械固有の絶対パス。相対 tracking URI は 5 fold の store を散らす）

### step 4 実装者が見つけた既存資産の弱さ（重要）

**`tests/ml/tuning/test_integration.py:_duplicated_defaults` をそのまま流用すると、既定値を
持たない必須 field 配下の既定値二重定義が丸ごと素通りする**（`type(field.default)` で辿るため）。
annotation から辿る版に強化済み。**tuning 側の既存 helper は弱いままなので、
将来 `ml.tuning` を触るときに同じ穴が残っていることを思い出すこと。**

### step 5 への裁定

- **HPO trial の「60 epoch / patience 10」（仕様書 §3）は `experiment/search.toml` を新設して
    置く。** `hyperparameter_search/*.toml` は top-level key が group 名 1 つという規約なので
    `[trainer]` を書けない。一方 `experiment` は preset group で、既に `fine_tune.toml` が
    `run_kind` + `[data]` + `[model]` + `[trainer]` を持っている。**同じ形が自然な置き場所**
- **no-op logger を `ml.experiment` へ足さない。** `logger=` を選ばなかったら train は
    明確な理由で失敗させる。「logging が黙って無効」は最も避けたい失敗の形であり、
    ドメイン非依存とはいえ要求されていない抽象を `ml` コアへ足す理由にならない
- **`run_directory` は fold ごとに別の値を渡す。** 既定は相対 `runs` なので、共有すると
    step 0/1 レビュー M3（split.json の食い違いを黙って通す）を踏む。M3 の検査は入ったが、
    検査に頼らず run を分けること
- `manifest.seed` をそのまま run seed に流さない（64 bit、`np.random.seed` は 2^32 未満）

## step 8: LOSO 5-fold の実走結果（2026-09-09、RTX 4090）

commit `4af725b`。5 fold とも `stop_reason=early_stopping`（epochs 109 / 93 / 50 / 53 / 78）。
2 fold の `git-diff.patch` はサイズ 0（untracked file で dirty 判定されただけ）で、
**追跡下のソースは 5 fold とも同一**（全 run `git.commit = 4af725b`）。

| held-out | n | MAE [uL] | RMSE | R^2 | 1sd coverage | mean 飽和 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 08T144137 | 164 | 0.02098 | 0.02716 | 0.8900 | 0.4188 | 0.000 |
| 08T153829 | 167 | 0.02329 | 0.02887 | 0.9096 | 0.5183 | 0.000 |
| 09T120737 | 167 | 0.01932 | 0.02668 | 0.8887 | 0.8171 | 0.000 |
| 09T130756 | 167 | 0.01151 | 0.01556 | 0.9330 | 0.8780 | 0.000 |
| 09T145923 | 167 | 0.01031 | 0.01345 | 0.9586 | 0.7073 | 0.000 |
| **平均** | **832** | **0.01708** (sd 0.00521) | | **0.9160** (sd 0.0267) | 0.6679 (sd 0.175) | 0.000 |

### ベースラインとの比較（step 7 で測り直した値）

| | MAE [uL] | R^2 |
| --- | ---: | ---: |
| 定数予測 | 0.06976 | -0.2055 |
| 輝度差 1 変数（lin1） | 0.04328 | +0.5224 |
| 面積系込み（lin3） | 0.03397 | +0.6967 |
| oracle（k を完璧に読めた天井） | 0.02186 | +0.8666 |
| **CNN** | **0.01708** | **+0.9160** |

**oracle を下回った。** oracle は「lin3 + held-out session の affine を後付けで完璧に補正」。
CNN はそれを超えたので、**大域特徴 + 完全な session 較正では届かない per-sample の空間情報を
使っている**（しかも session を教えられずに）。

### 成功条件の判定

1. **必須（機能）: 達成。** 5 fold とも early_stopping、`best.pt` / `weights.pt` / `final.pt` /
    `latest.pt` / `config.json` / `split.json` / `calibration.json` が揃う。MLflow の 5 run が
    FINISHED で、dataset fingerprint / split fingerprint / split.dimension=session /
    held_out_session / model.family / git.commit を辿れる
2. **必須（健全性）: 達成。** `saturated_positive_fraction` が 5 fold とも **0.000**（閾値 0.05）。
    held-out が train/validation に現れないことは step 1 で実測済み
3. **必須（定数予測超え）: 達成。** 0.01708 << 0.06976
4. **目標（lin1 超え）: 達成。** 0.01708 < 0.04328、R^2 0.9160 > 0.5224。**さらに oracle も超えた**

### 記録（成功条件 5）: blank と log 分散下限の相互作用

仕様書 §2 申し送り 3 への回答。blank は 16/832 = 1.9%。

| fold | blank exact_zero | blank 1sd cover | 正の真値の 1sd cover | pred_sd | RMSE | pred_sd/RMSE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 08T144137 | 1.000 | 1.000 | 0.4188 | 0.01444 | 0.02716 | 0.53 |
| 08T153829 | 0.667 | 1.000 | 0.5183 | 0.02260 | 0.02887 | 0.78 |
| 09T120737 | 1.000 | 1.000 | 0.8171 | 0.03183 | 0.02668 | 1.19 |
| 09T130756 | 0.333 | 1.000 | 0.8780 | 0.02439 | 0.01556 | 1.57 |
| 09T145923 | 0.333 | 1.000 | 0.7073 | 0.01282 | 0.01345 | 0.95 |

- **blank 側は全 fold で完全に当たる**（1sd coverage 1.000）。危惧された「blank が log 分散を
    下限へ押し下げる」は、blank 比率 1.9% では**全体を壊すほどには起きていない**
- **正の真値側の coverage は 5 fold 平均 0.668 で理想（0.683）にほぼ一致**するが、
    **fold 間の振れが大きい**（0.419〜0.878、sd 0.175）。`pred_sd/RMSE` が 0.53〜1.57 と
    fold ごとに過小・過大の両方へ振れており、**平均が合っているのは相殺の結果**
- **弱い負の関係が見える**（blank を厳密に 0 と当てた fold ほど正の真値側の coverage が低い）が、
    **n=5 では確立しない。** 断定せず記録に留める。**blank の `sample_weight` 調整は行っていない**
- 精度（MAE / R^2）に blank 起因の劣化は見られない

### 記録の訂正

orchestrator が「`trainable_parameter_count` が MLflow param に None」と記録したのは**誤り**。
param 名は `model.trainable_parameter_count` で、prefix を落として問い合わせていた。
5 run とも `model.parameter_count = 395048` / `model.trainable_parameter_count = 395048` /
`model.frozen_parameter_tensor_count = 0` が正しく入っている（base training なので凍結 0 は正しい）。

## step 4/5/6 レビュー（3 巡目）の裁定

verdict は request-changes。**src の振る舞いに誤りは 1 件も無く、指摘はすべて「検査・記録が
主張を支えていない」型。** 詳細は `memory/agents/code-reviewer/paste-volume-train-evaluate-step456.md`。

### must-fix（6 件。うち 3 件は should-fix からの昇格）

- **M1 resume の検査に検出力が無い（8 回目の同じ型）。** `resume_from=None` に変える変異で
    `test_train.py` が **30 passed 全緑**。参照 run も再開 run も同じ argv・同じ seed なので、
    checkpoint を無視して最初から回しても同じ最終重みに着く。**「一致する」型の assert が、
    再開したかどうかではなく決定論であることしか測っていない。** 実装自体は正しい
    （forward hook で 通し 9 / 中断 2 / 再開 8、resume 無視の変異では再開 9）
- **M2 案内している `logger.tracking_uri=file:///abs/mlruns` は必ず例外になる。**
    `validate()` が `file://` を合格させ、`Trainer.run()` の内側で raw な `MlflowException`。
    **`logger=` を忘れた運用者が受け取る理由文が、まさにその argv を案内している**
- **M3 `config.json` が tracking URI / storage URI を平文で持ち MLflow artifact として上がる。**
    `postgresql://user:pw@...` の password がそのまま入る。同じ repo は param に
    `sanitized_tracking_uri`、study 成果物に `storage_uri_redacted` を使っており、
    **新しい成果物だけ規約から外れている**
- **S2 を昇格: 既存 experiment では `artifact_location` が黙って無効になる。**
    5 fold 全部の artifact が黙って `<cwd>/mlruns` へ落ちうる。**silent-wrong は本 MR で
    一貫して潰してきた型**。（今回の実走は experiment が新規だったため影響なし。実測で確認済み）
- **S13 を昇格: `train.main()` が signal / deadline 停止でも 0 を返す。**
    5 fold の shell ループが次へ進み、**打ち切られた fold が report に混ざる**。
    運用手順書そのものに影響する
- **S5 を昇格: calibration の失敗が 2 箇所で黙って捨てられる。** `calibration.json` の
    `log_variance_offset` は report に載る値なので、黙って落ちてはいけない

### should-fix（残り 16 件）と nit（11 件）

S1 / S3 / S4 / S6 / S7 / S8 / S9 / S10 / S11 / S12 / S14 / S15 / S16 / S17 / S18 / S19 を対応。
nit は code-simplifier へ。

- **S7**（`trainer=gpu` = compile ON の end-to-end が無検査）は、私が step 8 で 5 回実走して
    経験的には通っているが、テストは要る
- **S17（申し送りの実測値が再現しない）は 3 例目。** 実装者が報告する数値を鵜呑みにせず
    レビュー側が測り直す運用が効いている

### 追加（orchestrator の指摘は誤りだった）

「`trainable_parameter_count` が None」は prefix を落として問い合わせた私の誤り。
正しくは `model.trainable_parameter_count = 395048` が 5 run とも入っている。

### 重要な制約

**修正で学習の振る舞いを変えないこと。** step 8 の結果（MAE 0.01708 / R^2 0.9160）は
commit `4af725b` に対するもので、学習経路が変わると再実走が必要になる。
上記はすべて観測性・堅牢性・記録の問題で、学習の数式には触れないはず。
**触れる必要が出たら実装せずに報告すること。**
