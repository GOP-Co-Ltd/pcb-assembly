# コア ML 基盤 MR3: orchestrator の裁定と進行

計画書: `memory/agents/implementation-planner/ml-core-3-model-evaluation.md`

## 確認事項への裁定

| # | 論点 | 裁定 | 理由 |
| --- | --- | --- | --- |
| 1 | doc の「正規化誤差 e」を `relative_error_*` へ改名するか | **改名する** | MR185 は `normalized_error`（相対誤差）と `normalized`（z-score）を別ファイルで同名運用しており取り違えの元。ユーザーは「明確な単語を利用したい」と明示済み。primary gate の意味は変えない。`docs/image-based-dispense-calibration-ml-plan.md` §3 の表記も MR3 のドキュメント段階で揃える |
| 2 | `GaussianImageRegressor` を ml 側に置くか | **ml 側に置く** | 合成は 30 行程度。MR3 単体で過学習・parity・inspection を検証でき、MR6 の ONNX export wrapper が単一 module を前提にできる |
| 3 | compile parity の既定 backend | **eager を既定、Inductor は capability probe 付き 1 本** | CI は Raspberry Pi の 30 分 timeout。比較機構そのものの検証が目的なので Inductor 可用性に依存させない。probe は `skip_if_no_mdns` と同じ「実物があるときだけ実物で検証する」構造 |

## 進行方針

`spec-test-author` は挟まない。計画書が公開 IF をシグネチャレベルで確定し、テスト観点も
正常系・異常系・エッジケースまで列挙しているため、契約は既に固定されている。
テストを実装へ寄せる drift は `code-reviewer` で検出する。

並列の分け方（計画書の 3 グループ案を、テストの依存を見て 2 波に組み替えた）:

| 波 | agent | 担当 | 理由 |
| --- | --- | --- | --- |
| 1 | A | `src/ml/model/` 4 file + `tests/ml/model/` | `ml/evaluation/` と disjoint |
| 1 | B | `src/ml/evaluation/{regression,slices}.py` + テスト | model に依存しない |
| 2 | C | `src/ml/evaluation/compile_parity.py` + テスト + `tests/ml/test_architecture.py` の `RUNTIME_MODULES` 更新 | parity テストが A の model を使う。`RUNTIME_MODULES` は列挙した module を実際に import するので、7 module が全部揃ってから 1 回で更新する |

## 実装報告の裁定

### mr3-evaluation（`ml/evaluation/{regression,slices}.py`、42 passed / type clean）

計画書との差分 4 点。裁定:

| # | 差分 | 裁定 | 備考 |
| --- | --- | --- | --- |
| 1 | `GaussianPredictions.valid_sample_mask() -> Tensor` を public 追加 | **受け入れ** | `slices` 側が同じ有効判定（非有限 / `mean <= 0` / `target <= 0` / `weight < 0`）を要る。private を跨ぐと `reportPrivateUsage`、写すと drift する。契約として公開するのが正しい |
| 2 | `ReliabilityBin` の集計だけ重みなし（件数ベース） | **要レビュー** | bin を件数で切るので統計も件数ベース、という理屈は通る。ただし overall / slice が重み付きなので、同じ report 内で重みの扱いが 2 通りになる。code-reviewer に是非を判定させる |
| 3 | `NumericDimension.validate()` に `bucket_count >= 1` と値の有限性を追加 | **受け入れ** | `torch.quantile` の前提。計画の抜け |
| 4 | `reliability_bin_count < 1` は `ValueError` にせず bin 0 個 | **受け入れ** | 計画に規定が無く、例外にする根拠が無い |

`docformatter` は summary 先頭を大文字化するため、docstring を小文字の識別子で始めない
（`"""log 分散へ…"""` → `"""Log 分散へ…"""` に書き換わる）。MR2 で見つけた折り返し問題に続く
2 つ目の docformatter の癖。

### mr3-model（`ml/model/` 4 module、100 テスト、`make test-no-hardware` 3082 passed）

計画書との食い違い 6 点。裁定:

| # | 差分 | 裁定 |
| --- | --- | --- |
| 1 | `ImageEncoder.padding_pixel` 読み取り専用 property を追加 | **受け入れ**。学習可能 padding pixel は private な `nn.Parameter`。計画書のテスト観点「padding pixel の grad が非ゼロ」を private に触れずに検証する手段が他に無い。MR4 の部分 fine-tuning が更新対象を選ぶ経路も兼ねる |
| 2 | `GaussianRegressionHead.conditioning_features` property を追加 | **受け入れ**。`input_features` と対になる |
| 3 | 「mean は常に正」→「常に非負」に読み替え | **受け入れ**。Softplus は数学的には正だが、pre-activation が極端に負だと float32 で厳密に 0 へ丸まる。epsilon を足して計画書の `mean = softplus(raw)` を崩すより、契約を実態に合わせる方がよい |
| 4 | `build_group_norm_convolution` の `kernel_size` を検証しない | **受け入れ**。既定 3 以外の利用者が居ない（AGENTS.md 開発原則 2） |
| 5 | `measure_model_size` の hook 除去と training mode 復元に `try` / `finally` | **受け入れ**。例外処理ではなく後始末なので `feedback_no_try_catch` に抵触しない |
| 6 | `replace_invalid_pixels` は検証なしの純関数 | **受け入れ**。shape / dtype 検査は `ImageEncoder.forward` 側に 1 本化されている |

#### 差分 3 の下流整合を orchestrator が実測確認

`ml/model/heads.py` は `nn.Softplus()`（非負）。
`ml/evaluation/regression.py:76` の `valid_sample_mask()` は `mean > 0` を有効条件にしており、
0 の mean は無効 sample として除外される。相対誤差は `(mean - target) / target` で
target（> 0 を要求）で割るため 0 除算も起きない。**矛盾なし。**

## code-reviewer の指摘への裁定（verdict: request-changes）

レビュー本体: `memory/agents/code-reviewer/ml-core-3-model-evaluation.md`

| # | 指摘 | 裁定 |
| --- | --- | --- |
| M1 | reliability bin だけ重みなし | **修正する。** 実測で `weight=[1,1,1,0]` の寄与ゼロ sample が単独 bin を作り RMSE 98.0 を出した。`mean_predicted_standard_deviation` が同一 report 内で同名別定義になるのも不可。bin の境界は等件数のまま、中の 3 統計を重み付きに。`weight > 0` で先に絞れば 0 除算分岐は不要 |
| M2 | 過学習テストの閾値が実測 0.4621 に対し 0.5、かつ outer seed が無効 | **修正する。** `spec-test-author` を挟まなかったことで想定していたリスクがそのまま出た形 |
| S1 | 重み付き percentile が複製等価でない | **定義は変えない。** 複製等価な定義（`p_i = (C_i - w_i/2)/W` 系）にすると一様重みで `torch.quantile` と一致しなくなり、計画書が固定した契約を壊す。両立しないトレードオフなので、`torch.quantile` 一致の側を採る。ただし **docstring に複製等価でないことを明記し、非一様重みの定義をテストで固定する**（未固定なのが実際の欠陥） |
| S2 | `GaussianPredictions` の等価性が壊れている | **修正する。** 全フィールド `eq=False` で別データが `==` になる。class 側 `@attrs.frozen(eq=False)` で identity 比較にする。**MR2 の `PaddedBatch` と `PreprocessedSample` も同じ欠陥なので同時に直す**（同一の欠陥クラス、いずれも未 push、3 行の変更。AGENTS.md 原則 3 の例外として理由を残す） |
| S3 | 空 bucket が randomized 3,000 中 440 件 | **修正する。** 空 bucket は落とす |
| S4 | `.4g` で bucket ラベルが衝突 | **修正する。** MR6 で evaluation.json のキー相当になるため一意性は要件 |
| S5 | 有効判定と weight 合計チェックの重複 | **修正する** |
| S6 | 計画外に足した挙動がテスト未固定 | **修正する。** 裁定で受け入れた挙動は契約としてテストに落とす |
| S7 | compile 失敗の理由文字列が warm-up 失敗まで飲む | **修正する**（文言のみ） |
| S8 | doc §3 の「正規化誤差 e」が未更新 | **修正する。** 裁定 1 で「MR3 のドキュメント段階で揃える」と決めた分 |
| S9 | 限界価値テスト 2 本 | **修正する** |
| 論点 2 | `_weighted_percentile` の IndexError | **ガードを足さない。** 到達不能でありガードは死にコード（原則 2）。docstring に前提条件を 1 行 |
| 論点 3 | `padding_pixel` は書き換え可能 | **受容。** `.grad` を見せる以上 `detach()` は返せず代替がない。docstring の根拠を「MR4 の fine-tuning」から「勾配の観測」に直す |
| nit | `inplace=True` 不揃い / `_as_output_tuple` の語法 / `span <= 0` の到達不能分岐 / `CompileOptions.validate()` 欠如 / `padding_pixel` docstring / 空 slice の reason 文言 | **すべて修正する** |
| nit | head の既定値 `hidden_features=128`、log 分散範囲 (-14, 5) が doc §2 の v1 値 | **変えない。** 計画書が明示的に既定と定めた。encoder と方針が不揃いなのは事実なので、MR5 でドメイン config を書くときに再考する |

### MR5 へ持ち越す申し送り

`state_dict()` のキーが `_encoder._padding_pixel` のように private 名を含む。
`_` prefix 規約の帰結だが、**MR5 の checkpoint 再開と MR6 の ONNX export で
内部属性のリネームが黙って互換性を壊す**。MR5 の checkpoint 設計で
「state_dict のキーは公開契約である」ことを明示するか、key の正規化層を置くかを決める。

## 2 巡目レビューの裁定（verdict: approve）

レビュー本体: `memory/agents/code-reviewer/ml-core-3-model-evaluation-round2.md`

先行指摘 19 件はすべて解消を確認。新規 must-fix なし。残りの裁定:

| # | 指摘 | 裁定 |
| --- | --- | --- |
| N1 | `regression.py:284` の `positions = exclusive / (total - sorted_weight)` が、weight 比が float64 の分解能を超えると 0 除算になり `median/p95 = nan` を `reason = None` で返す（`weight=[1.0, 1e-20]` で実測） | **ガードを足さない。docstring に前提条件を書く。** 実 weight は `1/(session 内 pad 数) × 1/(pad 内 view 数)` で比は高々 1e4 程度。1 巡目で `span <= 0` の到達不能分岐を削除した判断と同じ原則（AGENTS.md 原則 2）を適用する。ただし silent NaN は例外より悪いので、前提条件を明記して MR5 / MR6 へ申し送る |
| N2 | `_reliability_bin` の重み付き平均再実装。**第 3 の選択肢**として私有 module `_aggregation.py` に public `weighted_mean` を置けば pyright は警告を出さない（レビュアーが実機 pyright で検証済み）。公開 API も増えない | **採用する。** 修正者が「private の cross-module import か public 化か」の 2 択と誤認していた。重複を消せてかつ公開面が増えないなら、そちらが正しい |
| N3 | `PaddedBatch` / `PreprocessedSample` の identity 比較がテスト未固定 | **採用する。** 特に `PreprocessedSample` は `scale: float` を持つため、壊れた形へ戻しても気付けない |
| nit | S9 の時間計測コメント「実測 86 倍」が条件を書き分けていない ほか 3 件 | **採用する** |

### M2 の判定（history 中の最良値）

レビュアーが修正者の主張を独立に再現し、**修正者が挙げなかった代替 2 種も実測した**うえで
「契約の弱体化には当たらない」と判定。cosine decay / lr 0.005 / 末尾 30 step 平均のいずれも
`last/first` は 0.1 を割れず、best より弱い主張しか書けない。`best/first` は 8 seed で
最悪 0.0286（余裕 3.5 倍）。失われる「収束の安定性」の検証は MR3 の計画書に無い観点であり、
MR4 の学習ループ側で扱う。**この判定を受け入れる。**

### MR5 / MR6 への申し送り（追加）

N1 の前提条件（weight 比が float64 の分解能内であること）。dataset の weight 設計を
変える MR では再確認する。
