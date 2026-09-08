# MR6（`ml.export` — ONNX export / parity / static INT8 / model package）の仕様テスト

計画: `memory/agents/implementation-planner/ml-core-6-export.md`

`plan-implementer` と並列で書いた。`src/**` `docs/**` `pyproject.toml` は一切触っていない。

## 書いたテスト一覧（collect 済みの件数）

| ファイル | 件数 | 計画の対応 |
| --- | --- | --- |
| `tests/ml/export/support.py` | —（共有 helper） | §3 / §6 |
| `tests/ml/export/test_manifest.py` | 35 | §4.1 / §6.1 |
| `tests/ml/export/test_promotion.py` | 22 | §4.2 / §6.2 / 論点 3 |
| `tests/ml/export/test_graph.py` | 16 | §4.6 / §6.3 |
| `tests/ml/export/test_onnx_export.py` | 32 | §4.7 / §6.4 / 論点 4 |
| `tests/ml/export/test_quantization.py` | 17 | §4.8 / §6.5 |
| `tests/ml/export/test_parity.py` | 12 | §4.5 / §6.6 |
| `tests/ml/export/test_runtime.py` | 12 | §4.3 / §6.7 |
| `tests/ml/export/test_benchmark.py` | 24 | §4.4 / §6.8 |
| `tests/ml/export/test_integration.py` | 7 | §6.9 + 単一 payload の pin |
| `tests/ml/test_architecture.py` | 5（+1 新設層） | §6.10 |
| `tests/ml/model/test_blocks.py` | +2 | §4.10 / §7 #57 |
| `tests/ml/model/test_heads.py` | +2 | §4.10 / §7 #56 |

hardware マーカーは 1 件も置いていない（§1 論点 2 の判断どおり）。

## 検証結果（記録時点）

- `tests/ml` = **774 passed / 1 skipped**（ベースライン 592 passed / 1 skipped、+182）
- pyright `src/ml tests/ml scripts/ml_smoke.py` = 0 errors
- ruff 0.8.4 `format --check` / `check`、docformatter 1.7.5 `--check` いずれも書き換えゼロ
    （pre-commit 本体は `plan-implementer` と `.git` で競合するので、pre-commit が管理する
    env の binary を read-only モードで直接呼んだ。version は `.pre-commit-config.yaml` の
    pin と一致）

**期待される失敗は無い。** 実装が並列で先に着地したため、書いた時点で全て緑。
唯一 `CalibrationSample.validate()` に float64 拒否を期待したテストが落ちたが、計画 §4.8 に
その要求が無いので**テスト側を差し替えた**（下記「実装へ差し戻さなかった 1 件」）。

## テスト ↔ §7 の対応行 ↔ 潰す機構

合流後の変異実験はこの表をそのまま使える。

**（変異実験フェーズで訂正）** 執筆時点では「太字 18 行のうち 15 行をテスト化。残り 3 行
（#26 の一部・#29・#56/#57）は変異が成立しない」と書いたが、**実際に成立しなかったのは
#29 だけ**だった。#56 / #57 は既存の `TestExportedDynamicShapes` が検出し（4 本中 3 本が
落ちる）、#26 も検出する。**太字 18 行はすべて実測済み。**

| §7 | テスト | 潰す機構 |
| --- | --- | --- |
| 1 | `test_manifest::TestPayloadFilenames::test_includes_the_manifest_and_the_model` | `payload_filenames` から `MANIFEST_FILENAME` を落とす（完全一致で比較している） |
| 2 | `TestPayloadFilenames::test_sorts_and_removes_duplicates` | `sorted(set(...))` を `sorted(...)` にする（extra に同名を 2 個渡している） |
| 3 | `TestVerifyRuntime::test_reports_a_runtime_below_the_minimum`（4 ケース） | `verify_runtime` を常に `None` にする |
| 4 | `TestVerifyRuntime::test_accepts_a_runtime_that_meets_the_minimum[1.9 / 1.10.0]` と `[1.20.1 / 1.20.10]` | 版比較を文字列比較にする（文字列では `1.9 > 1.10`） |
| 5 | `TestSaveAndLoad::test_reports_an_unsupported_schema_version` / `test_reports_a_document_of_another_kind` | `DocumentKind.load` の envelope 照合を削る。**MR5 の実測どおり、`INFERENCE_MANIFEST_DOCUMENT.schema_version` を上げるだけでは save/load が同じ定数を共有するので落ちない**。テストは JSON を直接書き換えている |
| 6 | `TestValidate::test_reports_a_malformed_manifest`（**24 ケース**）/ `TestTensorContract::test_reports_a_malformed_contract`（**5 ケース**）/ `TestQuantizationRecord::test_reports_a_malformed_record`（**11 ケース**。レビュー対応で新設） | `validate()` の該当分岐を 1 つずつ削る（ケース単位で落ちる）。**執筆時点の 10 / 4 から増えている**（S4 と網羅 sweep の補強分） |
| **7** | `test_promotion::TestDecideRejectsCandidates::test_rejects_an_int8_candidate_without_latency_evidence` | `decide` の `latency is None` 分岐を削る（**論点 3 の核**。削ると INT8 が p95 不明のまま promote される） |
| **8** | 同 `::test_promotes_nothing_when_no_candidate_has_latency_evidence` | 同上。`promoted_candidate_id is None` と却下理由の両方を見る |
| 9 | `TestDecideSelectsAWinner::test_promotes_the_int8_candidate_that_is_clearly_faster` | 選択を `candidates[0]` 固定にする |
| 10 | 同 `::test_prefers_the_smaller_artifact_when_the_latency_gap_is_negligible`（対照 `::test_prefers_the_larger_float32_over_a_negligibly_faster_int8`） | `negligible_difference_ratio` の分岐を削り常に p95 最小を採る。対照側が独立に落ちる |
| 11 | 同 `::test_prefers_float32_when_latency_and_size_both_tie` | tie-break から `precision == "float32"` を落とす |
| 12 | `TestDecideRejectsCandidates::test_rejects_a_candidate_that_regresses_against_the_baseline` | baseline 比較の分岐を削る（gate 内 0.07 なので絶対値 gate では拾えない） |
| **13** | 同 `::test_rejects_a_candidate_calibrated_on_a_split_other_than_train` | `CALIBRATION_SPLIT` 比較を削る |
| 14 | `TestDecideCannotJudge::test_reports_why_the_comparison_does_not_hold[mismatched-evaluated-split]` | 一致検査を削る |
| 15 | 同 `[no-float32-baseline]` | baseline 探索の失敗分岐を削る |
| 16 | `TestDecideRejectsCandidates::test_rejects_a_candidate_slower_than_the_latency_gate` | `maximum_p95_seconds` の分岐を削る |
| 17 | 同 `::test_rejects_a_candidate_that_failed_export_parity` | `export_parity_passed` の分岐を削る |
| **18** | `test_graph::TestInspect::test_reports_an_output_name_that_collides_with_a_graph_value` | `inspect` の `check_model(..., full_check=True)` を外す。**実 export で SSA 違反を再現済み**（output_names を `mean` / `log_variance` にする） |
| 19 | `TestInspect::test_reports_a_file_that_is_not_a_protocol_buffer` / `::test_reports_a_json_document` / `::test_reports_a_path_that_does_not_exist` | `onnx.load` の例外捕捉を外す |
| 20 | `TestVerifyStandardOperators::test_reports_operators_outside_the_allowlist` | 許可リスト比較を削る（`Gemm` が理由に出ることまで見る） |
| **21** | 同 `::test_reports_a_graph_that_uses_a_vendor_domain` | `node_domains` の検査を削る。**実物の ORT 最適化済み graph**（`com.microsoft.nchwc` を含み、しかも `onnx.checker` は通る）を使うので、§2.6 の判断そのものの回帰検出器になる |
| 22 | （なし） | local function を持つ実 graph を作れなかった。下記「テスト化できなかった行」 |
| **23** | `TestVerifyDynamicDimensions::test_reports_an_axis_that_became_a_fixed_value` | `dim_param` が空のときも通すようにする（batch は出荷形で固定値 `1`） |
| 24 | 同 `::test_reports_a_symbol_that_does_not_match_the_declaration` | 名前一致の比較を削り存在確認だけにする |
| **25** | `test_onnx_export::TestExport::test_rejects_a_compiled_wrapper` | `_orig_mod.` 前置きの検査を削る（torch.onnx は黙って通すので、ファイルが作られてしまい `list(tmp_path.iterdir()) == []` が落ちる） |
| **26** | `TestExport::test_does_not_change_the_callers_model` | `copy.deepcopy` を外して `model.eval()` を直接呼ぶ（`model.training` が False になる） |
| 27 | `TestExport::test_writes_the_graph_in_evaluation_mode` | `.eval()` を削る。**GroupNorm では観測できないので Dropout を 1 段持つ tiny model を使う**（学習 mode のまま export すると graph に `Dropout` node が残ることを実測済み） |
| **28** | `TestOptionsValidateFor::test_reports_an_axis_whose_example_size_is_one`（対照 `::test_accepts_a_batch_axis_whose_example_size_is_two`） | `size <= 1` の検査を削る |
| **29** | `TestExport::test_reports_an_axis_that_cannot_be_made_dynamic` | **§7 の想定と機構が違う**（下記参照）。現環境では torch が例外を投げるので、守っているのは `torch.onnx.export` の例外捕捉。そこを外すと `TorchExportError` が漏れて落ちる |
| 30 | `TestExport::test_honours_the_requested_opset_version` | `torch.onnx.export` へ `opset_version` を渡すのを止める |
| 31 | `TestExport::test_rejects_a_compiled_wrapper` / `::test_reports_malformed_options_without_writing_a_file` / `::test_reports_an_axis_that_cannot_be_made_dynamic` | 失敗経路で `model_path.unlink(missing_ok=True)` を止める（3 本とも `tmp_path` が空であることを見る） |
| 32 | `TestOptionsValidate::test_reports_malformed_options`（10 ケース） | `validate()` の該当分岐を 1 つずつ削る |
| **33** | `test_quantization::TestQuantize::test_quantizes_a_model_whose_initializers_are_shared` | `op_types_to_quantize` に `options.quantized_operator_types` を渡すのを止める。**tiny model を 2 stage 4 block・`group_norm_groups=8` にしてある**（これが §2.5 の共有 initializer 失敗を再現する最小構成。1 stage では失敗が再現せずテストが空洞化する） |
| **34** | 同 `::test_reports_a_failure_when_no_operator_type_is_restricted` | `quantize_static` の例外捕捉を外す（`op_types_to_quantize=[]` は「全 op」扱いになり `ValueError` が出ることを実測済み）。※ 実装が `validate()` で `()` を先に弾く設計なら、この行は例外捕捉の検出器にならない |
| 35 | （なし） | `quant_pre_process` の有無を出力から観測できなかった |
| 36 | `TestQuantize::test_records_how_the_model_was_quantized` | `sorted(...)` を外す（降順の sample_id を渡している） |
| 37 | 同 `::test_reports_too_few_calibration_samples` | `minimum_calibration_samples` の検査を削る |
| 38 | 同 `::test_quantizes_a_model_whose_initializers_are_shared` / `::test_keeps_the_dynamic_axes_of_the_source_model` | `OnnxGraphSummary.inspect` を呼ばず `summary` を捏造する |
| 39 | `test_parity::TestMeasure::test_measures_every_case_that_was_given` | `cases` の走査を `cases[:1]` にする（case_id の並びを完全一致で見る） |
| 40 | 同 `::test_reports_a_mismatch_when_the_weights_differ` | `passed` を常に `True` にする |
| **41** | 同 `::test_lists_a_non_finite_output` | `non_finite_output_names` の収集を削る（0 除算で必ず `inf` になる tiny model を使う） |
| **42** | 同 `::test_lists_an_output_that_is_required_to_be_positive_but_is_not`（対照 `::test_lists_no_violation_when_the_positive_output_is_positive`） | `positive_output_names` の判定を削る（符号が確定する tiny model を使うので学習状態に依存しない） |
| 43 | 同 `::test_rejects_a_compiled_wrapper` | 検査を削る |
| 44 | `test_runtime::TestLoad::test_reports_a_tampered_payload` | `ImmutablePackage.verify` の呼び出しを削る |
| **45** | 同 `::test_reports_a_manifest_that_declares_a_payload_the_package_lacks` | `payload_filenames()` と `verified.checksums` のキー集合比較を削る |
| 46 | 同 `::test_reports_a_runtime_below_the_minimum` | `verify_runtime` の呼び出しを削る |
| 47 | `TestPredict::test_reports_a_missing_input` / `::test_reports_an_input_of_the_wrong_element_type` / `::test_reports_an_input_whose_fixed_axis_does_not_match` | ORT の例外捕捉を外す |
| 48 | `TestPredict::test_returns_the_same_values_as_the_eager_model` | session の作り方を変えても一致は崩れない（§7 の注記どおり検出力は弱い） |
| 49 | `test_benchmark::TestLatencyStatistics::test_summarizes_a_known_series` | percentile の index 計算を 1 ずらす。**許容幅は nearest-rank と線形補間の差だけを吸収する幅にしてある**（20 点・1 ms 刻み。1 点ずらすと外れる） |
| 50 | 同 `::test_reports_an_empty_series` / `::test_reports_a_duration_that_cannot_be_measured`（3 ケース） | 空判定・非有限判定・負値判定をそれぞれ削る |
| **51** | `TestColdStartMeasurement::test_measures_a_child_process_that_never_loads_torch` と `TestDeviceBenchmark::test_measures_the_cold_start_in_a_child_process` | `VmHWM` の読み取りを `resource.getrusage(RUSAGE_SELF).ru_maxrss` に置き換える（親は torch を読み込み済みなので、継承すると親の値と等しくなり `<` が落ちる） |
| 52 | 同上 | 子プロセスを起こさず親プロセスで測る（同じ assert が落ちる） |
| 53 | `TestDeviceBenchmark::test_reports_the_worst_p95_across_the_cases` | `max` を `min` にする（解像度の違う 2 case なので p95 が一致しない） |
| 54 | （なし） | warm-up の有無を安定に観測できない。下記参照 |
| 55 | `TestDeviceBenchmark::test_counts_every_payload_in_the_artifact_size` | payload 合計を `model.onnx` 単体にする |
| 56 | `test_heads::TestExportedDynamicShapes`（2 本） | `heads.py` の batch 照合へ `int()` を戻す。**実測で 2 本とも落ちる**（訂正済み。`torch.export.export` の既定 `strict=False` では `int()` が SymInt を example の値へ落とす） |
| 57 | `test_blocks::TestExportedDynamicShapes`（2 本） | `blocks.py` の mask 形状照合へ `int()` を戻す。**mask 経路の 1 本が落ちる**（mask を使わない経路は落ちなくて正しい） |
| **58** | `test_architecture::TestInferenceOnlyLayer::test_importing_them_does_not_load_onnx_or_torch` | `ml/export/runtime.py` に `import torch`（または `import onnx`）を足す |
| 59 | `test_architecture::TestDependencyFreeLayer` | `ml/export/manifest.py` に `import onnxruntime` を足す |
| 60 | `test_architecture::TestRuntimeLayer` | `ml/export/parity.py` に `import onnx` を足す |
| 61 | `test_architecture::TestDomainIndependence` | `ml/export/promotion.py` に `from pcbasm.config import Machine` を足す |
| 62 | `test_integration::TestPromotionOverTheRealArtifacts::test_rejects_the_int8_candidate_without_latency_evidence` | #7 と同じ。通し経路なので広く落ちる |

### §7 に無い追加行（実測で分かった機構）

| # | テスト | 潰す機構 |
| --- | --- | --- |
| A | `test_onnx_export::TestExport::test_writes_a_single_self_contained_file` と `test_integration::TestExportedPackage::test_the_package_holds_the_weights_in_a_single_payload` | `torch.onnx.export` の `external_data=False` を外す。**既定は `True` で、重みが `model.onnx.data` へ切り出される**。manifest は `model_filename` を 1 個しか持たないので、package から重みが丸ごと落ち、checksum は通るのに `load` が外部データを見つけられない。export 直後（ファイル構成）と package 経由（`load` → `predict`）の 2 段で pin してある |
| B | `test_onnx_export::TestAsDynamicShapes::test_names_every_input_even_without_a_dynamic_axis` | `as_dynamic_shapes()` から「dynamic 軸を持たない入力の空 dict」を落とす。**dict 形式の `dynamic_shapes` は全引数名を要求し、欠けると `torch._dynamo.exc.UserError` になる**（実測で踏んだ） |
| C | `test_onnx_export::TestExport::test_runs_at_other_resolutions_through_onnxruntime`（3 解像度）/ `::test_declares_the_dynamic_axes_to_onnxruntime` | dynamic 宣言を落とす。ONNX の `dim_param` だけでなく、**実 ORT session が別解像度を実際に受け取れること**まで見る |
| D | `test_quantization::TestQuantize::test_reports_calibration_samples_of_the_wrong_element_type` | ORT quantization の例外捕捉を外す（float64 の calibration 入力で 3rd-party 例外が漏れる） |

## §7 でテスト化できなかった行と理由（**要 orchestrator 判断**）

> **変異実験フェーズによる訂正（下記 1 について）。**
> `int()` を実際に `src/ml/model/` へ戻す変異を当てたところ、
> `TestExportedDynamicShapes` は **4 本中 3 本が落ちた**（heads 2/2、blocks 1/2。
> blocks の残り 1 本は mask を使わない経路なので落ちなくて正しい）。
> `torch.export.export` の既定は torch 2.12 で `strict=False` であり、
> 非 strict 経路では `int()` が SymInt を example の値へ落とす。
> 「no-op」の結論は `torch.onnx.export` の出力次元だけを見たことによる誤り
> （`torch.onnx.export` は非 strict の失敗を黙って `strict=True` へフォールバックする）。
> #56 / #57 は変異対象として成立する。詳細は計画書 §2.2 の訂正済みの表。
>
> 3（#22 local function）も成立した。`onnx.helper` で「既定 domain の node だけを持ち、
> 呼ばれない local function を抱えた model」を組むと `full_check=True` を通り、
> `verify_standard_operators` の function 検査だけを単独で観測できる。

すべて**コンテナ内（torch 2.12.1 / onnx 1.22.0 / onnxruntime 1.29.0）での実測**に基づく。

1. ~~**#56 / #57（`int()` を戻すと dynamic 次元が固定される）が再現しない。**~~
   **この節は誤り（変異実験フェーズで訂正）。** 観測したのは
   `torch.onnx.export` が出す ONNX の入力 shape だけで、そこは確かに `int()` の有無で
   変わらない。しかし `torch.onnx.export` は非 strict export の失敗を黙って
   `strict=True` へフォールバックしており、`torch.export.export`（既定 `strict=False`）
   を直接見ると `int()` は SymInt を example の値へ落とす。
   **commit 2 は no-op ではなく、現に存在する制約違反の除去である。**
   `TestExportedDynamicShapes` は `strict=False` を明示して契約を固定し直した。
2. **#29（特殊化された軸を `verify_dynamic_dimensions` が捕まえる）が成立しない。**
   現環境の `torch.export` は、宣言した軸を dynamic にできない場合**黙って特殊化せず例外を
   投げる**（`Constraint violation` / `UserError`）。したがって `export` の中で
   `verify_dynamic_dimensions` を呼ばなくても、テストは torch の例外捕捉側で落ちる。
   `verify_dynamic_dimensions` 単体の検出力は #23 / #24（`test_graph`）が持っている。
3. **#22（local function を拒否）**。`onnx.checker(full_check=True)` を通り、かつ
   `FunctionProto` を持つ実 model を作れなかった。手で組んだ graph は checker で落ちるので
   `inspect` の段階で止まり、`verify_standard_operators` まで到達しない。
   同じ検査の隣接分岐（node domain）は #21 が実物で押さえている。
4. **#35（`quant_pre_process` を通す）**。呼んでも呼ばなくても、この tiny model では
   出力 graph の観測可能な差が出なかった。
5. **#54（warm-up を回す）**。学習機の CPU では初回実行と 2 回目の差が計測ノイズに埋もれ、
   安定した判定にならない。`measured_count` の反映は
   `test_measures_every_case`（`statistics.measured_count == 5`）で押さえている。
6. **`DeviceBenchmark.measure` の既定値 100 / 10**。skill `testing-strategy` の「定数 literal の
   追試を書かない」に当たるので書いていない（計画 §6.8 は「既定値であることだけ確認」と
   していたが、`inspect.signature` を見るテストは実質 literal の追試なので落とした）。

## 実装へ差し戻さなかった 1 件

`CalibrationSample.validate()` が float64 の `values` を拒否することを期待したテストを書いたが、
計画 §4.8 に dtype 要求が無いのでテスト側を差し替えた。代わりに
`StaticQuantizationResult.quantize` が float64 の calibration sample で**例外を漏らさず理由を
返す**ことを見ている（上表 D）。`validate()` 側で早く弾く設計にするかは実装の判断。

## 設計上の申し送り（orchestrator へ）

1. **`external_data=False` を明示すること**（上表 A）。既定 `True` は `model.onnx.data` を
   作るので、`InferenceManifest.model_filename` 1 個の設計と両立しない。テストは
   「export 先ディレクトリに `model.onnx` しか無い」ことを固定した。
2. **`as_dynamic_shapes()` は全入力名を含める必要がある**（上表 B）。
   §4.7 のシグネチャは変えていないが、返り値の内容に条件が付く。
3. **`torch.compile` した module に parameter が 1 つも無い場合、`state_dict()` が空になり
   `_orig_mod.` 判定をすり抜ける**（実測）。
   → **裁定 3 で対応済み。** 判定を `hasattr(model, "_orig_mod")` へ寄せ、parameter を
   持たない compile 済み module の拒否をテストで固定した。
4. §4.x のシグネチャは**すべてそのまま使えた**。変更要求は無い。
5. **`InferenceManifest.validate()` が precision と quantization を結び付けていない件**（実装側
   からの問い）。**manifest 側は結び付けるべき**だと考える。`precision="static-int8"` かつ
   `quantization=None`、あるいは `precision="float32"` かつ `quantization` 有りの manifest は、
   存在し得ない artifact を記述している。manifest は Pi へ出荷される単一の記述なので、
   その内部整合は `validate()` の担当範囲そのもの。一方 `PromotionCandidate` 側は
   §4.2 の却下条件が独立に書かれており、`decide()` が `calibration_split` を見ているので
   現状のままでよい。計画 §6.1 の異常系一覧に無いので**テストは足していない**。
   → **裁定 2 で採用。** `_validate_quantization` を足し、`test_manifest::TestValidate` に
   2 ケース追加済み。`PromotionCandidate` 側は指摘どおり変更なし。

## ユーザーへの質問（**解決済み。裁定 1 で決着した**）

- ~~上記 1（`int()` 除去 = commit 2）を残すか。現環境では機能的に no-op と実測された。~~
    **前提が誤りだった。** `int()` は非 strict export の制約に現に違反しており、
    `torch.onnx.export` の silent fallback で ONNX の次元だけが同じに見えていた。
    commit 2 は残す。根拠の記述は docs §6 / 計画書 §2.2・§4.10 /
    `blocks.py`・`heads.py` のコメントで訂正済み。
