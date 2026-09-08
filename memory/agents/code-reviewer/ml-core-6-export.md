# MR6（`ml.export`）レビュー

対象: `feature/2026-09-08/ml-core-6-export`（staged diff vs `main` = 56f6fab、31 file / +7383）

## verdict: request-changes

must-fix は 1 件（promotion の精度 gate 3 分岐がテストで固定されていない）。
実装の設計・層分離・docs 同期は概ね計画どおりで、実測に基づく判断はすべて再現できた。
残りは should-fix / nit で、orchestrator が「今回は見送る」と裁定してもマージ可能な性質のもの。

## 再現できた主張（レビュアー側で独立に確認した）

- `int()` の 3 通りの表（torch 2.12.1+cu130、コンテナ内で再現）

    | 宣言 | `int()` あり | `int()` なし |
    | --- | --- | --- |
    | `Dim.AUTO` + `strict=False` | `(2, 4)` へ特殊化 | `(s52, 4)` |
    | 名前付き `Dim` + `strict=False` | `UserError: Constraints violated (batch)` | `(s52, 4)` |
    | `strict=True` | `(s27, 4)` | `(s27, 4)` |

    `torch.onnx.export(..., dynamo=True)` は `int()` の有無に関わらず `dim_param='batch'` を出す
    → 「ONNX だけ見ても気付けない」も再現。docs §6 / 計画書 §2.2・§4.10 / `blocks.py`・`heads.py`
    のコメントは、いずれもこの最終理解と一致している。

- docs §6 の operator 集合は実測と完全一致（創作なし）。
    実測: `Add, Clip, Concat, Conv, Gemm, Greater, InstanceNormalization, Mul, ReduceMean,
    Relu, Reshape, Shape, Softplus, Where` / `node_domains == ('',)` / opset 20 / ir 10。

- 層分離: `TestInferenceOnlyLayer` は素の interpreter で `sys.modules` を見る既存機構を再利用しており、
    `ml.export.runtime` / `benchmark` が `onnx` / `onnxscript` / `torch` / `torchvision` を
    1 つも読まないことを機械検証できている。`ml-runtime` group に `onnxruntime` が入っているので
    `ml.export.parity` を `RUNTIME_MODULES` へ置いた判断も整合。

- `pyproject.toml` / `uv.lock` / `scripts/ml_smoke.py` / `src/ml/config` / `src/ml/tuning` は未変更（MR5 競合回避）。
- モックゼロ、`tests/ml` に hardware マーカーゼロ、`</content>` 等の混入なし、docformatter 由来の文字化けなし。
- §4 の公開シグネチャは引数名・keyword-only・戻り値型ともに計画どおり。追加された公開名は
    `PRECISIONS` / `QUANTIZED_PRECISION` / `DEFAULT_DOMAIN` の 3 つのみ。

______________________________________________________________________

## must-fix

### M1. `promotion._rejection_reason` の精度 gate 3 分岐がテストで固定されていない（#11 と同型の confound）

- 対象: `src/ml/export/promotion.py:322-354`、`tests/ml/export/test_promotion.py:204-221`
- 確信度: **高**（コンテナ内で実際に理由文字列を出して確認した）
- 深刻度: 中〜高（論点 3 の gate 本体。計画 §6.2 が明示的に要求したテストが空洞化している）

`_rejection_reason` の判定順は
`export_parity → 絶対 primary_score → 絶対 coverage → baseline 比 regression → baseline 比 coverage差 → …`。
先の分岐が後の分岐を隠していて、次の 3 つがどれも単独では固定されていない。

1. **絶対 `maximum_primary_score` 分岐（:322）が masked。**
    `test_rejects_a_candidate_whose_primary_score_exceeds_the_gate` は
    `_int8(primary_score=0.11)`（baseline は 0.05）。この分岐を削っても
    baseline 比 regression（0.06 > 0.01）が同じ候補を落とすので、
    `promoted == FLOAT32` / `rejected == (INT8,)` は変わらず**テストが通ってしまう**。
    再現: `_rejection_reason` の `if accuracy.primary_score > accuracy_gate.maximum_primary_score:` を削除 → 当該テストは緑のまま。
    さらに **baseline 自身が絶対 gate を超えるケースのテストが無い**（baseline には regression 検査が掛からないので、
    絶対 gate だけが唯一の防壁になる経路）。

2. **絶対 coverage 分岐（:330）が masked。**
    `test_rejects_a_candidate_whose_coverage_is_too_far_from_the_reference` は `_int8(coverage=0.60)`、
    baseline は 0.683。この分岐を削っても baseline 比 coverage 差（0.083 > 0.03）が落とすので通る。

3. **baseline 比 coverage 差の分岐（:345-354）を狙ったテストが 1 本も無い。**
    計画 §4.2 判定順 3 の「baseline 以外で coverage 差が `maximum_coverage_difference` を超える」に対応する
    却下条件だが、上の 2 と相互にマスクし合っていて単独では観測されない。
    観測できる入力は実在する（確認済み）:
    `float32.coverage=0.660` / `int8.coverage=0.706`（どちらも基準 0.683 から 0.03 以内、差は 0.046）
    → 現状は `baseline に対する coverage の差が大きすぎます: 0.046（許容差 0.03）` で INT8 が落ちる。

**要求**: 上記 3 分岐それぞれを単独で観測するケースを足す。少なくとも
(a) baseline が絶対 primary_score gate を超える単独候補、
(b) baseline 比 regression に掛からない範囲で絶対 coverage gate を超える候補、
(c) 絶対 coverage gate に掛からない範囲で baseline 比 coverage 差を超える候補、の 3 本。
併せて `RejectedCandidate.reason` の文面まで見る（`_rejected_ids` だけでは分岐を区別できない）。

______________________________________________________________________

## should-fix

### S1. `StaticQuantizationResult.quantize` が `validate()` を通らない `QuantizationRecord` を返しうる

- 対象: `src/ml/export/quantization.py:86-105`（`StaticQuantizationOptions.validate` が `()` を通す）と
    `:171-181`（`record.quantized_operator_types=options.quantized_operator_types`）、
    `src/ml/export/manifest.py:117-118`（`QuantizationRecord.validate` は空を拒否）
- 確信度: **高**（コンテナ内で再現。Linear だけの tiny model を `quantized_operator_types=()` で量子化 →
    `quantize` は成功し `record.validate()` が `quantized_operator_types は 1 個以上が必要です` を返す）
- 深刻度: 低〜中

成功した量子化成果物が、次段の `InferenceManifest.validate()` / `PromotionCandidate.validate()` で
必ず落ちる。共有 initializer を持つ model では `quantize_static` 側が先に失敗するので実害は出ていないが、
「成功したのに使えない record」を返す状態は契約として破綻している。
`quantize` 側で `()` を弾くか、実際に量子化された operator 種を record へ書くか、どちらかに寄せたい。
（`()` を `validate()` で弾かないのは §6.5 の異常系テストを成立させるための意図的判断と承知している。
その場合は `quantize` の入口で弾けば両立する。）

### S2. `OnnxExportOptions.validate_for` / `as_dynamic_shapes` が `KeyError` を送出する

- 対象: `src/ml/export/onnx_export.py:119-121`（`by_name[dimension.input_name]`）、`:143-145`
- 確信度: **高**（`DynamicDimension(input_name="absent", ...)` で `KeyError: 'absent'` を再現）
- 深刻度: 低（`export()` 経由では `validate()` が先に走るので到達しない）

どちらも計画 §4.7 の公開メソッドで、`validate_for` は `str | None` を返す契約。
計画 §8 の「検証は例外ではなく `validate() -> str | None`」「呼び出し側の不変条件違反以外に例外を増やさない」
に反する。単独で呼ばれた場合に理由文字列で返すか、`validate()` 済みを前提とする旨を docstring に明記したい。

### S3. `promotion._p95_seconds` の到達不能な `raise ValueError`

- 対象: `src/ml/export/promotion.py:398-403`
- 確信度: 高（到達不能であること自体はコメントが認めている）
- 深刻度: 低

`src/ml/export/` 唯一の `raise`。AGENTS.md「起こり得ないシナリオ向けの処理を増やさない」と
計画 §8「例外は新規に増やさない」に当たる。型を通すためだけなら
`_best_candidate` 側で `latency is not None` の候補だけを扱う形（内包表記で絞る）に寄せられる。

### S4. `QuantizationRecord.validate()` の全分岐と `InferenceManifest.validate()` の複数分岐が未テスト

- 対象: `src/ml/export/manifest.py:110-134`（`QuantizationRecord.validate` 6 分岐すべて）、
    `:167-187`（`onnx_ir_version`、`exporter_versions` の 3 分岐、`training_run_id` /
    `dataset_fingerprint` / `split_fingerprint`、`_validate_contracts` の名前重複、未知 `precision`）、
    `:78-81`（`TensorContract` の「10 進数か識別子か」分岐）
- 確信度: 高（`tests/ml/export/test_manifest.py` に該当 parametrize ケースが無い）
- 深刻度: 低〜中

計画 §7 #6 は「`validate()` の該当分岐を 1 つずつ削る（parametrize の該当ケースだけ落ちる）」を
変異操作として挙げているので、ケースの無い分岐はそのまま survivor になる。
とくに `calibration_sample_ids` の重複・空検査は「どの sample で校正したか」の監査証跡そのもので、
`PromotionDecision` が `calibration_split` しか見ない以上、ここが唯一の防壁になる。

### S5. `payload_filenames()` の重複除去は valid な manifest では到達不能で、pin するテストが invalid manifest を作っている

- 対象: `src/ml/export/manifest.py:202-206`、`tests/ml/export/test_manifest.py:105-119`
- 確信度: 高
- 深刻度: 低

`validate()` は extras 内の重複も model_filename との重複も予約名も拒否するので、
valid な manifest では `{MANIFEST_FILENAME, model_filename, *extras}` の要素は常に相異なる。
一方 §7 #2 を pin するテストは `extra_payload_filenames=("preprocess.json","evaluation.json","preprocess.json")`
という **`validate()` が拒否する manifest** で `payload_filenames()` を呼んでいる。
「不正な入力に対する挙動」を契約として固定している形なので、dedup をやめて整列だけにするか、
テストを valid な入力へ寄せるか、どちらかに揃えたい。

### S6. `DeviceBenchmark.load` が `validate()` を呼ばない（`InferenceManifest.load` は呼ぶ）

- 対象: `src/ml/export/benchmark.py:341-345` vs `src/ml/export/manifest.py:239-250`
- 確信度: 中（計画 §4.4 は `load` の検証を明記していない）
- 深刻度: 低

同じ「envelope 付き document」型で `load` の厳しさが揃っていない。
`DeviceBenchmark.validate()` は happy path（`is None`）でしか呼ばれておらず、
全エラー分岐が未テストのまま残る要因にもなっている。

### S7. `parity.OnnxParityResult.measure` の 3 分岐が未テスト

- 対象: `src/ml/export/parity.py:121-132`（case の入力数不一致、model の出力数不一致）、
    `:182-183`（`positive_output_names` が `output_names` に無い）
- 確信度: 高
- 深刻度: 低

計画 §6.6 の一覧には無いが、いずれも呼び出し側の間違いを理由文字列で返す経路なので、
削っても誰も気付かない。

### S8. 記録側に訂正前の記述が残っている（`spec-test-author` のノート）

- 対象: `memory/agents/spec-test-author/ml-core-6-export.md`
- 確信度: 高
- 深刻度: 低（成果物ではないが、この MR の staged diff に含まれる）

冒頭の blockquote で「訂正」と断ってはいるが、本文が古いまま残っている箇所が 4 つある。

- 「§7 の太字 18 行のうち 15 行はテスト化した。残り 3 行（#26 の一部・#29・#56/#57 の変異）は
    環境の実測により**変異が成立しない**」→ #56 / #57 は成立する（実測で 4 本中 3 本が落ちる）
- 対応表の #56 / #57 行「**現環境では `int()` を戻しても落ちない**」→ 同上
- 「§7 でテスト化できなかった行」の 1 番「commit 2（`int()` 除去）は現環境では機能的に no-op」→ 誤り
- **「## ユーザーへの質問」の「上記 1（`int()` 除去 = commit 2）を残すか。現環境では機能的に no-op と
    実測された」→ 訂正の印が付いていない。** orchestrator がユーザーへ中継する節なので、ここが最も紛らわしい。

### S9. 裁定 3（compile 済み判定の `hasattr(_orig_mod)` 化）が裁定記録に無く、計画書も旧記述のまま

- 対象: `memory/agents/orchestrator/ml-core-6-export.md:72`、
    `memory/agents/implementation-planner/ml-core-6-export.md:159-161` および `:843`
- 確信度: 高
- 深刻度: 低

実装は `_COMPILED_WRAPPER_ATTRIBUTE = "_orig_mod"` の `hasattr` 判定（`onnx_export.py:226`、`parity.py:168`）だが、
裁定記録も計画書も `any(key.startswith("_orig_mod.") for key in model.state_dict())` のまま。
`plan-implementer` のノートにしか裁定 3 が残っていない。裁定記録に 3 件（`int()` 根拠の訂正 /
manifest の precision ↔ quantization / compile 判定）を追記して、計画書 §1 論点 4・§4.7 手順 1 を同期したい。
なお `hasattr` への変更そのものは妥当（parameter を持たない compile 済み module も拒否できる。テストで固定済み）。

______________________________________________________________________

## nit

- `memory/agents/implementation-planner/ml-core-6-export.md` §7 末尾「太字の 15 行（…）」の後に **18 個**の番号が並ぶ（数が合わない）。確信度: 高
- 同 §1 論点 4 の「実測（§2.4）で分かったこと」— §2.4 は出力名衝突の節で、compile 済み wrapper の実測ではない。参照ずれ。確信度: 中
- `DEFAULT_MODEL_FILENAME`（`manifest.py:35`、`__all__` 入り）と `DEFAULT_DOMAIN`（`graph.py:28`）が
    src / tests のどこからも使われていない。`tests/ml/export/support.py:65` は `MODEL_FILENAME = "model.onnx"` を
    別に定義しており、定数を import すれば済む。確信度: 高
- `graph._walk_nodes` の subgraph 再帰（`If` / `Loop` 内の node を拾う）は計画 §4.6 に無い追加機構で、テストも無い。
    docstring の意図（検査逃れの防止）は妥当だが、動くことが固定されていない。確信度: 高 / 深刻度: 低
- `support.shared_fp32_model_path()` の `tempfile.mkdtemp` を後片付けしない（process ごとに 1 ディレクトリ残る）。確信度: 高
- `support.TinyRegressor.__init__` が `torch.manual_seed()` でグローバル RNG を触る。
    入力側は `torch.Generator` で分離できているので、model 側も `Generator` か
    `torch.random.fork_rng()` に寄せられる。確信度: 中
- `ColdStartMeasurement` の docstring「Process 起動から初回予測までの所要」に対し、実測開始点は
    子プロセスの `-c` プログラム冒頭（interpreter 起動と `import json/sys/time` の後）。
    onnxruntime / numpy の import は含まれるので実用上は足りるが、文言が少し強い。確信度: 高 / 深刻度: 低
- `_COLD_START_PROGRAM` が `OnnxInferenceModel.load(sys.argv[1])` と `str` を渡している
    （注釈は `Path`。`ImmutablePackage.verify` が `Path()` で包むので動く）。確信度: 高 / 深刻度: 低
- `tests/ml/export/test_graph.py:194` の `com.microsoft.nchwc` 期待は x86 の ORT 実装に依存する。
    aarch64 では nchwc 変換が掛からず、`assert "com.microsoft.nchwc" in summary.node_domains` が落ちうる。
    学習機（x86）でしか回さない前提なら問題ないが、前提をテスト側にコメントで残したい。確信度: 中
- `tests/ml/export/test_quantization.py:100-121` `test_stays_close_to_the_float32_model` は片側境界のみで、
    INT8 が FP32 の複製でも通る。QDQ node の存在は別テストで固定されているので穴ではないが、
    このテスト単独の検出力はほぼ無い。確信度: 高 / 深刻度: 低
- `OnnxInferenceModel.__init__` が public なので、`load` の検証（改竄検出・manifest 突き合わせ・版数）を
    飛ばして構築できる。`attrs.frozen` でない事情は理解できるが、`_` 付きの classmethod 経由に寄せる余地はある。確信度: 中 / 深刻度: 低
- `_best_candidate` の tie 判定が `< negligible_difference_ratio` の strict 比較で、
    ちょうど境界の候補は tie に入らない。境界の扱いを固定するテストが無い。確信度: 高 / 深刻度: 低
- `promotion.PromotionDecision.summary` の文面を見るテストが無い（計画 §9-9 は「実測値を summary に書き残す」を
    INT8 が大きくなりうる件の緩和策としていた）。確信度: 高 / 深刻度: 低
- 計画 §5 の 12 commit 境界がまだ commit として存在しない（HEAD == main、全部 staged）。
    AGENTS.md「1 commit 1 関心事」を満たすには分割が要る。確信度: 高（進行上の指摘）

______________________________________________________________________

## 蒸し返さなかった既知の判断（妥当と判断した）

- `external_data=False` の明示。既定 `True` は `model.onnx.data` を作り、manifest の `model_filename` 1 個設計と
    両立しない。2GB 超が export できない代償は Pi 向け model では現実的でなく、失敗は理由文字列で返る。妥当。
- ORT optimized FP32 を artifact にしない判断。`write_ort_optimized_model` で実物を作り、
    `verify_standard_operators` が実際に拒否することまでテストで固定してあり、判断そのものの回帰検出器になっている。良い。
- `TensorDifference.between` の公開化。`parity.py` は `RUNTIME` 層で `onnx_export` を import できず、
    private 関数は他 module から使えないので、再実装を避けるための最小の公開化。
    本体は移動のみで振る舞いは不変（`_maximum` も含めて差分を確認した）。既存テストは無変更で緑。妥当。
- `int()` 除去 2 箇所。残っている `int()` は channel 軸（静的）だけで、dynamic 軸には掛かっていない。
    `blocks.py` は `torch.Size != tuple` の比較になっており、計画 §4.10 の「`tuple(...)` を挟まない」も守られている。
- survivor #29 / #35 / #48 / #54 を補強しない判断。#48 / #54 は裁定済み。
    #29 は「守っているのが `torch.onnx.export` の例外捕捉」であることが正しく、機構自体は #23 / #24 が
    `test_graph` で単独に押さえている。#35 は観測点が作れないという説明に同意。

## 検証結果（コンテナ内で再実行した）

- `pre-commit run -a`: **pass**（全 hook Passed、書き換えゼロ。実行後の `git diff` も空）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings。情報レベルの指摘も 0 件）
- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（**783 passed / 1 skipped / 17 warnings**、42.07s）
- `make test-no-hardware` はこの機体に pcbnew / picamera2 が無いため未実行（`make test` / `pytest -m hardware` も未実行）

## ユーザーへの質問（orchestrator 経由）

- M1 の 3 分岐を今回の MR で埋めるか、Phase 5 の gate 実運用に入る前でよいか。
    現状でも「実機実測なしの INT8 は promote できない」という論点 3 の核は守られている（#7 / #8 は実測済み）。
    埋めていないのは精度側 gate の内訳で、`ml.evaluation` の metric が揃う MR 以降でも遅くはない。
- S1 の `quantized_operator_types=()` を `quantize` の入口で弾いてよいか。
    弾くと §6.5 の異常系テスト（全 op 指定で共有 initializer が失敗することの pin）が別の書き方になる。

______________________________________________________________________

# 2 巡目レビュー（差し戻し対応後）

## verdict: approve

must-fix M1 は解消。should-fix 9 件と nit 4 件もすべて対応されている。
新たに **should-fix 2 件（どちらも「残る survivor 7 件」の判断根拠の誤り）と nit 6 件**を出す。
いずれもマージを止める性質ではないので、orchestrator の裁量で次 MR へ送ってよい。

## 1 巡目指摘の解消確認

| 件 | 状態 | 確認方法 |
| --- | --- | --- |
| **M1** 精度 gate 3 分岐の空洞化 | **解消** | 下記のとおり 4 分岐すべて単独発火する入力になっている |
| S1 `quantize` が invalid な record を返す | 解消 | `StaticQuantizationOptions.validate()` が `()` を弾く。規則が 1 か所に集約されており、`quantize` の入口が `options.validate()` なので二重定義にならない |
| S2 公開 API の `KeyError` | 解消 | `validate_for` は `by_name.get()` + 理由文字列、`as_dynamic_shapes` は `setdefault`。回帰テストあり |
| S3 到達不能な `raise` | 解消 | `_p95_seconds` ごと削除。`_best_candidate` の内包表記で narrowing され `cast` も不要になっている。#7 / #8 の検出力は維持（削ると `rejected` が空になり両テストが落ちる） |
| S4 `validate()` の未テスト分岐 | 解消 | `TestQuantizationRecord` 10 ケース、`TestValidate` 24 ケース。masking も確認した（`duplicate-input-name` は名前重複分岐だけが発火、`model-filename-with-a-directory` はディレクトリ分岐だけ、など） |
| S5 到達不能な dedup | 解消 | dedup を削り、docstring に「`validate()` が相互重複を拒否するので不要」と根拠を書いている。テストも valid な入力に直っている |
| S6 `DeviceBenchmark.load` が validate しない | **判断で解決** | `load` は構造だけ見る旨を `TestDeviceBenchmarkValidation` の docstring に明記し、`validate()` の全 8 分岐を parametrize で埋めた。`InferenceManifest.load` との非対称は残るが、`PromotionCandidate.validate()` が下流で `LatencyEvidence` を検証するので危険は封じ込められている。妥当 |
| S7 parity の未テスト分岐 | 解消 | 引数検証 8 本すべてが理由文まで見ている。`positive_output_names` の綴り間違いを入口で弾く件はコメントの動機付けも良い |
| S8 テストノートの訂正前記述 | 解消 | 4 箇所とも訂正。「ユーザーへの質問」は取り消し線 + 訂正文 |
| S9 裁定記録の追記漏れ | 解消 | 追加裁定 3 件・変異フェーズの裁定・1 巡目の裁定を追記。計画書 §1 論点 4 / §4.7 も `hasattr` へ同期 |
| nit 1 太字 15 行 vs 18 個 | 解消 | |
| nit 2 未使用の公開定数 | 解消。**ただし私の指摘の半分は誤りだった** | `DEFAULT_DOMAIN` は `graph.py:122` / `:171` で使われている。私の見落とし。`DEFAULT_MODEL_FILENAME` だけが未使用で、`support.py` が import する形になった |
| nit 3 `_walk_nodes` の subgraph 再帰 | 解消 | `If` の then / else へ `Neg` / `Abs` を隠した graph で `operator_types == ("Abs", "If", "Neg")` を固定 |
| nit 4 `com.microsoft.nchwc` の決め打ち | 解消 | 手組みの独自 domain graph で機構を決定的に固定し、実 ORT 側は「独自 domain が出なければ skip」に変更。良い分離 |

### M1 の解消を分岐ごとに確認した

新しい 5 本は、いずれも**狙った分岐だけが発火する**入力になっている（算術を確認した）。

- 絶対 `primary_score`（非 baseline）: `_int8(primary_score=0.11)` + 理由文 assert。
    絶対 gate を消すと理由が「baseline に対する…悪化」に変わって落ちる
- 絶対 `primary_score`（baseline）: `_decide([_float32(primary_score=0.11)])`。
    baseline には baseline 比検査が掛からないので、この分岐だけが防壁
- 絶対 coverage（非 baseline）: `float32=0.710` / `int8=0.720`。
    baseline 比の差は 0.010（許容内）、基準からの 0.037 だけが超える
- 絶対 coverage（baseline）: `_decide([_float32(coverage=0.600)])`
- baseline 比 coverage 差: `float32=0.660` / `int8=0.706`。
    どちらも基準から 0.023 で絶対 gate 内、差 0.046 だけが超える

`_reason_for()` の導入が決め手というのはそのとおり。
`TestGateValidation` / `TestEvidenceValidation` / `TestCandidateValidation` の
parametrize も「1 ケースにつき 1 分岐だけ発火」になっていることを確認した
（`reference_coverage=1.5` は loop の有限・非負を通って範囲検査だけが発火、など）。

______________________________________________________________________

## 2 巡目の should-fix

### R1. `benchmark.py:388` は survivor ではなく、到達可能で未テスト

- 対象: `src/ml/export/benchmark.py:387-389`、
    `memory/agents/plan-implementer/ml-core-6-export.md` の survivor 表
- 確信度: **高**（コンテナ内で実際に到達させた）
- 深刻度: 低

ノートの根拠「壊れた入力ファイルは cold start が先に落ちる」は **`cases[0]` に限った話**。
`ColdStartMeasurement.measure` は `cases[0]` しか使わず、`_validate_measure_arguments` は
`is_file()` しか見ないので、**2 件目以降の壊れた `.npz`** は `_measure_case` まで届く。

再現（実行して確認した）:

```
cases = (正常, 中身を b"not an npz archive" に差し替えた 2 件目)
DeviceBenchmark.measure(...)
-> (None, 'case の入力を読めません: .../case-1.npz（This file contains pickled (object) data. ...）')
```

この理由文は他のどの経路からも出ない。既存の
`TestDeviceBenchmark::test_reports_a_later_case_whose_input_file_is_missing` が
まったく同じ 2-case 構成なので、`unlink()` を `write_bytes(...)` に変えた 1 本で塞げる。

### R2. `onnx_export.py:187` の survivor 根拠が誤り。`validate_for` の docstring も ONNX 経路には当てはまらない

- 対象: `src/ml/export/onnx_export.py:106-133`（`validate_for` の docstring と `size <= 1` 規則）、
    survivor 表の「消しても `torch.export` 自身が同じ入力を拒否する」
- 確信度: **高**（3 通り実測した）
- 深刻度: 低（成果物の正しさには影響しない。根拠の記述とテスト戦略の前提の問題）

実測 1 — `torch.export.export` は確かに size-1 を特殊化する（docstring は**この経路では正しい**）:

```
batch=1 Dim.AUTO/strict=False -> (1, 4)
  W torch/_export/non_strict_utils.py: dimension 0/1 specialized; Dim.AUTO was
    specified along with a sample input with hint = 1
batch=1 namedDim/strict=False -> UserError: Constraints violated (batch)!
batch=2 Dim.AUTO/strict=False -> (s97, 4)
```

実測 2 — しかし `export()` が実際に使う `torch.onnx.export(dynamo=True)` 経路では、
size-1 の軸を dynamic 宣言しても**例外にならず、正しく動く ONNX が出る**:

```
example batch=1、dynamic_shapes={images:{0:batch,2:height,3:width}, conditioning:{0:batch}}
-> inspect: images ('batch','6','height','width') / conditioning ('batch','2')
-> verify_dynamic_dimensions(...) -> None（合格してしまう）
-> ORT 実行: batch=1/2/3 いずれも成功、eager との max|差| = 0.000e+00
```

つまり `:187` が survivor なのは「torch が先に拒否するから」ではなく、
**消しても何も壊れないから**。`:212` の `verify_dynamic_dimensions` も
`dim_param` が宣言どおりなので素通りする（この 2 つは相互に補完しない）。

規則そのものは「silent fallback に依存しない」保守的な方針として残す価値があると思う。
ただし残すなら理由を実態に合わせたい。現状の
「大きさ 1 以下の軸は `torch.export` が固定値へ特殊化する。宣言しても無視されるので、
export を試す前に拒否する」は、`OnnxExportResult.export` の利用者から見ると事実と違う。
「`torch.export` を直接使う経路（compile parity / 将来の ExecuTorch）では特殊化されるので、
入口で揃えて拒否する」といった書き方なら実態と一致する。

______________________________________________________________________

## 2 巡目の nit

- **`onnx_export.py:216`（書き出した opset が指定と違う）を現状維持にした判断は、
    ノート結論部が採用した規則「到達不能な防御分岐は足さない。足してしまったら削る」
    （S3 / `benchmark:392` / MR5 裁定 5）と矛盾する。**
    他の 3 件（`quantization:170` / `benchmark:273` / `benchmark:402`）は
    `tuple[T | None, str | None]` の伝播 guard で、消すと型が壊れるので別枠と理解できる。
    `:216` だけは純粋な「torch が契約を破ったら」検査で伝播 guard ではない。
    残すなら「torch の回帰を検出する意図的な assertion」と明記したい。確信度: 高 / 深刻度: 低
- **理由文の部分一致 assert は、テストを日本語メッセージの文面へ結び付ける。**
    今回はそれ以外に前段・後段を区別する観測点が無いので正しい選択だが、
    文面を直すとテストが落ちる負債が 30 箇所ほど増えた。次の MR 以降の検討候補として、
    「識別子としての理由コード（`Literal`）＋人間向け文面」に分ける案を挙げておく。
    `tuple[T | None, str | None]` 規約の変更を伴うのでこの MR の範囲外。確信度: 中
- `LatencyGate.validate` の `not math.isfinite(self.negligible_difference_ratio)` は
    後続の `not (0.0 <= x < 1.0)` に完全に吸収される（nan も ±inf も後者が True になる）。
    到達不能な条件。`maximum_p95_seconds` 側は `inf <= 0.0` が False なので必要で、
    こちらは残す。確信度: 高 / 深刻度: 極小
- `memory/agents/spec-test-author/ml-core-6-export.md` の対応表 #6 行が
    「（10 ケース）/（4 ケース）」のまま。実際は 24 / 5 ケース。確信度: 高
- `TestDeviceBenchmarkValidation::test_reports_a_malformed_benchmark` は 8 param それぞれで
    `_measure(tmp_path)` を回す（子プロセス起動 + 2 case × 7 推論 × 8 回）。
    `attrs.evolve` の対象は 1 個あれば足りるので、module scope fixture へ寄せられる。
    総時間 43.53s で計画 §9-10 の 60 秒枠内なので急ぎではない。確信度: 中
- 手順上の事故（sweep をバックグラウンドに残し、変異が当たったままの状態が 2 度観測された）が
    ノートに記録されているのは良い。**成果物側に残骸が無いことを独立に確認した**
    （`if False` / `</content>` / 文字化け / worktree と index の差分、すべてゼロ）。
    `parity.py` の `_validate_arguments` / `_verify_session_names` も 1 巡目と同一。確信度: 高
- 計画 §5 の 12 commit がまだ切られていない（HEAD == main = 56f6fab、全部 staged）。1 巡目から未変化。

______________________________________________________________________

## 分析（「初回 survivor 36% はリポジトリの設計方針に由来する」）の評価

**3 点とも妥当**だと考える。とくに 2 の「3rd-party を最後の砦にしている経路では
入口検査を消しても同じ理由が返る」は、`parity` の引数検証 10 分岐が丸ごと空洞だった
という具体で裏付けられていて説得力がある。1 の「理由文字列を返す方針が検証を直列
チェーンにする」も、M1 と S4 と #11 が同じ形だったという事実と整合する。

対策 4 点も適切。補足を 2 つ:

- 対策 2（理由文まで見る）は、上の nit のとおり文面への結合という代償がある。
    今回の規模なら許容範囲だが、ドメイン層で同じことをやると負債が効いてくる
- **分析に 4 つ目の原因を足せる。「到達不能な伝播 guard が分岐として数えられる」。**
    残る 7 件のうち 4 件（`quantization:170`、`benchmark:273`、`benchmark:402`、
    および `onnx_export:216`）はこれ。`ast` の `If` を数える sweep では、
    `X, error = f(); if X is None: return None, error` という定型が
    「テストできない分岐」として必ず残る。次の sweep では最初にこの形を除外して数えると、
    survivor 率の実質的な意味がはっきりする（今回なら 64/176 のうち何件がこれかで
    「本当の穴」の数が変わる）

## 2 巡目の検証結果（コンテナ内で再実行した）

- `pre-commit run -a`: **pass**（chain 全体が exit 0、実行後の `git diff` は空）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings。情報レベルの指摘も 0 件）
- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（**883 passed / 1 skipped / 18 warnings**、43.53s）
- 変異残骸・文字化け・成果物汚染: なし
- `make test` / `make run` / `pytest -m hardware` は未実行

## ユーザーへの質問（orchestrator 経由）

- R1（`benchmark:388` の 1 本追加）と R2（`validate_for` の docstring 訂正）を
    この MR で入れるか、次の MR へ送るか。どちらもマージを止める性質ではない。
- R2 に関連して、`validate_for` の `size <= 1` 規則を**残す**という理解でよいか。
    `torch.onnx.export` 経路では size-1 の軸も正しく dynamic 化されると実測できたので、
    「batch を dynamic にしたい呼び出し側は batch 2 以上の example を用意すること」という
    API 制約を我々が課している状態になる。保守的方針として妥当だと思うが、判断は残しておきたい。
